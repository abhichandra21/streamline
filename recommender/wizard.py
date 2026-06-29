"""Concierge wizard: adaptive guided-recommendation question loop.

One role=reason LLM call per turn. The model sees the taste profile as a prior
and the answers so far, and returns either the next question or a finish signal
carrying a synthesized QueryIntent plus a free-text ranking note. The question
cap is enforced here, independent of what the model returns.
"""

from __future__ import annotations

import json
import logging
import re
from collections import Counter
from dataclasses import dataclass, field

import config
from recommender.query_engine import _parse_json_response, _safe_query_intent

log = logging.getLogger(__name__)


# Defensive caps on hidden-form wizard state. This is not a security boundary
# for a single-user app, just protection against malformed or runaway payloads.
_MAX_STATE_BYTES = 64_000
_MAX_TURNS = 50

# Valid wizard step ids. Deterministic intake steps come first, then the
# review surface and the optional single adaptive (LLM) question.
_VALID_STEPS = frozenset({
    "content_type", "time_window", "energy", "tone", "review", "adaptive",
})
_DEFAULT_STEP = "content_type"


@dataclass
class WizardState:
    turns: list[dict] = field(default_factory=list)
    turn_count: int = 0
    step: str = _DEFAULT_STEP
    answers: dict = field(default_factory=dict)
    adaptive_turns: list[dict] = field(default_factory=list)
    review_seen: bool = False

    @classmethod
    def from_json(cls, raw: str) -> "WizardState":
        try:
            if raw and len(raw) > _MAX_STATE_BYTES:
                raise ValueError("wizard state payload too large")
            data = json.loads(raw) if raw else {}
            turns = data.get("turns", [])
            if not isinstance(turns, list):
                raise ValueError("turns must be a list")
            if len(turns) > _MAX_TURNS:
                raise ValueError("too many wizard turns")
            answers = data.get("answers", {})
            if not isinstance(answers, dict):
                answers = {}
            adaptive_turns = data.get("adaptive_turns", [])
            if not isinstance(adaptive_turns, list):
                adaptive_turns = []
            if len(adaptive_turns) > _MAX_TURNS:
                raise ValueError("too many adaptive turns")
            step = data.get("step", _DEFAULT_STEP)
            if step not in _VALID_STEPS:
                step = _DEFAULT_STEP
            return cls(
                turns=turns,
                turn_count=int(data.get("turn_count", len(turns))),
                step=step,
                answers=answers,
                adaptive_turns=adaptive_turns,
                review_seen=bool(data.get("review_seen", False)),
            )
        except (json.JSONDecodeError, ValueError, TypeError) as exc:
            log.warning("Resetting wizard state, could not parse: %s", exc)
            return cls()

    def to_json(self) -> str:
        return json.dumps({
            "turns": self.turns,
            "turn_count": self.turn_count,
            "step": self.step,
            "answers": self.answers,
            "adaptive_turns": self.adaptive_turns,
            "review_seen": self.review_seen,
        })


def _qa_so_far(state: WizardState) -> str:
    if not state.turns:
        return "(no answers yet)"
    lines = []
    for t in state.turns:
        picks = ", ".join(t.get("selected", []))
        free = (t.get("free_text") or "").strip()
        answer = " / ".join(p for p in (picks, free) if p) or "(skipped)"
        lines.append(f'Q: {t.get("prompt", "")}\nA: {answer}')
    return "\n".join(lines)


def _structured_preferences_block(state: WizardState) -> str:
    """Render the deterministic answers as a block the adaptive LLM must honor.

    Returns "" when no structured intake has happened yet, so the legacy
    LLM-only prompt is unchanged.
    """
    if not state.answers:
        return ""
    # Lazy import avoids a circular dependency (wizard_flow imports WizardState).
    from recommender import wizard_flow
    intent_dict, context_note, _ = wizard_flow.build_recommendation_seed(state)
    lines = ["STRUCTURED PREFERENCES (already decided — do not re-ask):",
             f"content_type: {intent_dict['content_type']}"]
    if intent_dict.get("max_runtime_minutes"):
        lines.append(f"max_runtime_minutes: {intent_dict['max_runtime_minutes']}")
    energy = state.answers.get("energy")
    if energy:
        lines.append(f"energy: {energy}")
    tone = state.answers.get("tone") or []
    if tone:
        lines.append(f"tone: {', '.join(tone)}")
    if context_note:
        lines.append(f"notes: {context_note}")
    lines.append(
        "Do not ask content type, time available, or energy again. Ask at most "
        "ONE question that would materially improve mood, novelty, or theme fit. "
        "If you already have enough signal, return the recommend action now."
    )
    return "\n".join(lines) + "\n\n"


# TMDB keywords that are production/technical noise, not watchable themes.
_KEYWORD_STOPLIST = frozenset({
    "aftercreditsstinger", "duringcreditsstinger", "based on novel or book",
    "woman director", "sequel", "based on comic", "based on young adult novel",
})

# Memoized per (cache_dir, history size): the user's real genres/keywords change
# only on a rebuild, so aggregating the TMDB cache once per process is enough.
_TASTE_TAGS_CACHE: dict = {}


def _taste_tags(ctx, top_genres: int = 6, top_keywords: int = 10) -> str:
    """Build a chip vocabulary from the user's real TMDB genres + keywords.

    Reads the cached TMDB metadata for every watched title and returns the most
    common genres and keywords so the wizard offers concrete, recognizable
    options instead of invented vibes. Returns "" when no data is available.
    """
    wi = getattr(ctx, "watch_index", None)
    tmdb = getattr(ctx, "tmdb_client", None)
    if wi is None or tmdb is None:
        return ""
    entries = getattr(wi, "entries", []) or []
    key = (getattr(ctx, "cache_dir", ""), len(entries))
    if key in _TASTE_TAGS_CACHE:
        return _TASTE_TAGS_CACHE[key]

    genres, keywords = Counter(), Counter()
    for e in entries:
        tmdb_id = e.get("tmdb_id")
        if not tmdb_id:
            continue
        try:
            meta = tmdb.get_cached_by_id(tmdb_id, e.get("content_type", "movie"))
        except Exception:
            meta = None
        if not meta:
            continue
        genres.update(g for g in (meta.genres or []) if g)
        keywords.update(k for k in (meta.keywords or [])
                        if k and k.lower() not in _KEYWORD_STOPLIST)

    top_g = [g for g, _ in genres.most_common(top_genres)]
    top_k = [k for k, _ in keywords.most_common(top_keywords)]
    if not top_g and not top_k:
        block = ""
    else:
        block = (
            "THE USER'S REAL CATEGORIES (pulled from their watch history — use THESE as the "
            "chip options for any genre or theme question; never invent vague vibes):\n"
            + (f"Genres they watch most: {', '.join(top_g)}\n" if top_g else "")
            + (f"Themes/tags they watch most: {', '.join(top_k)}\n" if top_k else "")
            + "Pick the 3-4 that fit the moment; you may add a \"Surprise me\" or \"Branch "
              "out\" chip. Keep chip labels short and plain.\n\n"
        )
    _TASTE_TAGS_CACHE[key] = block
    return block


def _prompt(state: WizardState, profile: str, force_finish: bool, tags: str = "") -> str:
    cap = config.WIZARD_MAX_QUESTIONS
    finish_clause = (
        "You MUST finish now: return the recommend action. Do not ask another question.\n"
        if force_finish else
        f"You may ask at most {cap} questions total ({state.turn_count} asked so far). "
        "Ask another only if it would materially change the recommendation; otherwise finish.\n"
    )
    return (
        "You help the user pick something to watch tonight, and you know their whole viewing "
        "history (the taste profile below). Be warm and engaging — talk like a friend who "
        "knows their taste — but stay clear and easy to read.\n\n"
        "Each turn, ask ONE question. A sentence or two of friendly, specific setup is good; "
        "keep it tight and conversational, never a paragraph of flowery prose.\n"
        "RULES:\n"
        "- Plain, everyday words. No markdown, no asterisks or styling characters, no purple "
        "metaphors (never say things like 'your heaviest gear').\n"
        "- You may mention ONE show or movie they have watched if it sharpens the question; "
        "do not list several titles.\n"
        "- Build on their last answer and what their taste shows.\n\n"
        f"{tags}"
        "ANSWER CHIPS: 3-4 options, each 1-4 plain words a normal person instantly understands "
        "— concrete, not vague. GOOD: \"Crime\", \"Easy watch\", \"Something gripping\", "
        "\"Make me laugh\". BAD (never): \"Smart but easy\", \"Grip me\", \"Warm and safe\".\n\n"
        "CONTENT TYPE is already chosen (see structured preferences) — never re-ask it. Ask "
        "about tonight: mood, energy, how much time, genre or theme, comfort vs something new.\n\n"
        f"TASTE PROFILE:\n{profile}\n\n"
        f"{_structured_preferences_block(state)}"
        f"ANSWERS SO FAR:\n{_qa_so_far(state)}\n\n"
        f"{finish_clause}"
        "Return ONLY valid JSON, exactly one of:\n"
        '1) {"action":"ask","prompt":str,"subtext":str,'
        '"chips":[{"label":str,"value":str}],"multi":bool,"allow_free_text":bool}\n'
        "   3-4 chips as above; subtext is optional and, if present, ONE short plain sentence. "
        "Use SINGLE-SELECT (multi=false) for either/or questions; MULTI-SELECT (multi=true) "
        "when several can apply. Always set allow_free_text=true.\n"
        '2) {"action":"recommend","summary":str,'
        '"intent":{"genres":[],"origin_countries":[],"languages":[],"mood_descriptors":[],'
        '"similar_to":[],"max_runtime_minutes":null,"year_from":null,"year_to":null,'
        '"unwatched_only":true,"special_intent":null,"content_type":"both","top_n":5,'
        '"platforms":[]},"context_note":str}\n'
        '   "summary" is a short plain recap ("something light and funny, around an hour"). '
        '"context_note" carries soft signals (runtime/energy/company) and which watched titles '
        "this should feel like, for the ranker.\n"
        "   When the user gave a time window, set intent.max_runtime_minutes (an integer): "
        "\"under an hour\" -> 60, \"around 90 minutes\" -> 90, \"a couple of hours\" -> 120. "
        "Carry time in max_runtime_minutes, not only in context_note.\n"
    )


def _finalize(state: WizardState, profile: str, llm, tags: str = "") -> dict:
    """Force a recommend turn. Used on cap hit or early-exit."""
    raw = llm.generate(_prompt(state, profile, force_finish=True, tags=tags),
                        role="reason", max_tokens=config.WIZARD_MAX_TOKENS,
                        timeout=config.TIMEOUT_REASON)
    try:
        data = _parse_json_response(raw)
        if not isinstance(data, dict):
            data = {}
    except (json.JSONDecodeError, ValueError, TypeError, AttributeError) as exc:
        log.warning("Malformed finalize response, using defaults: %s", exc)
        data = {}
    return {
        "action": "recommend",
        "summary": data.get("summary", "based on what you told me"),
        "intent": _safe_query_intent(data.get("intent", {})),
        "context_note": data.get("context_note", ""),
    }


def next_turn(state: WizardState, ctx, force_finish: bool = False) -> dict:
    """Produce the next wizard turn: a question, or a finish signal with intent."""
    profile = ctx.taste_profile or ""
    tags = _taste_tags(ctx)
    cap_hit = state.turn_count >= config.WIZARD_MAX_QUESTIONS
    if force_finish or cap_hit:
        return _finalize(state, profile, ctx.llm, tags)

    raw = ctx.llm.generate(_prompt(state, profile, force_finish=False, tags=tags),
                           role="reason", max_tokens=config.WIZARD_MAX_TOKENS,
                           timeout=config.TIMEOUT_REASON)
    try:
        data = _parse_json_response(raw)
    except (json.JSONDecodeError, ValueError, TypeError) as exc:
        log.warning("Malformed wizard turn, finalizing instead: %s", exc)
        return _finalize(state, profile, ctx.llm, tags)

    if not isinstance(data, dict):
        log.warning("Wizard turn returned non-object JSON, finalizing instead")
        return _finalize(state, profile, ctx.llm, tags)

    if data.get("action") == "recommend":
        return {
            "action": "recommend",
            "summary": data.get("summary", "based on what you told me"),
            "intent": _safe_query_intent(data.get("intent", {})),
            "context_note": data.get("context_note", ""),
        }

    return {
        "action": "ask",
        "prompt": _plain(data.get("prompt", "What are you in the mood for?")),
        "subtext": _plain(data.get("subtext", "")),
        "chips": _normalize_chips(data.get("chips")),
        "multi": bool(data.get("multi", False)),
        "allow_free_text": bool(data.get("allow_free_text", True)),
    }


_MARKDOWN_CHARS = re.compile(r"[*_`#]+")


def _plain(text: str) -> str:
    """Strip markdown emphasis the model sometimes emits, so it never renders raw."""
    return _MARKDOWN_CHARS.sub("", str(text or "")).strip()


def _normalize_chips(raw) -> list[dict]:
    """Coerce LLM chip output into a list of {label, value} dicts, dropping junk."""
    if not isinstance(raw, list):
        return []
    chips = []
    for c in raw:
        if isinstance(c, dict) and c.get("value"):
            value = str(c["value"])
            chips.append({"label": _plain(c.get("label") or value), "value": value})
    return chips
