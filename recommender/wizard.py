"""Concierge wizard: adaptive guided-recommendation question loop.

One role=reason LLM call per turn. The model sees the taste profile as a prior
and the answers so far, and returns either the next question or a finish signal
carrying a synthesized QueryIntent plus a free-text ranking note. The question
cap is enforced here, independent of what the model returns.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field

import config
from recommender.query_engine import _parse_json_response, _safe_query_intent

log = logging.getLogger(__name__)


@dataclass
class WizardState:
    turns: list[dict] = field(default_factory=list)
    turn_count: int = 0

    @classmethod
    def from_json(cls, raw: str) -> "WizardState":
        try:
            data = json.loads(raw) if raw else {}
            turns = data.get("turns", [])
            if not isinstance(turns, list):
                raise ValueError("turns must be a list")
            return cls(turns=turns, turn_count=int(data.get("turn_count", len(turns))))
        except (json.JSONDecodeError, ValueError, TypeError) as exc:
            log.warning("Resetting wizard state, could not parse: %s", exc)
            return cls(turns=[], turn_count=0)

    def to_json(self) -> str:
        return json.dumps({"turns": self.turns, "turn_count": self.turn_count})


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


def _prompt(state: WizardState, profile: str, force_finish: bool) -> str:
    cap = config.WIZARD_MAX_QUESTIONS
    finish_clause = (
        "You MUST finish now: return the recommend action. Do not ask another question.\n"
        if force_finish else
        f"You may ask at most {cap} questions total ({state.turn_count} asked so far). "
        "Ask another only if it would materially change the recommendation; otherwise finish.\n"
    )
    return (
        "You are a film/TV concierge helping someone who cannot decide what to watch. "
        "Use their taste profile as a PRIOR: do not ask what it already implies; probe only "
        "tonight's context (mood, energy, time available, alone or with others, novelty vs comfort).\n\n"
        f"TASTE PROFILE:\n{profile}\n\n"
        f"ANSWERS SO FAR:\n{_qa_so_far(state)}\n\n"
        f"{finish_clause}"
        "Return ONLY valid JSON, exactly one of:\n"
        '1) {"action":"ask","prompt":str,"subtext":str,'
        '"chips":[{"label":str,"value":str}],"multi":bool,"allow_free_text":bool}\n'
        "   3-5 short, tappable chips. Set multi=true when several answers can co-apply.\n"
        '2) {"action":"recommend","summary":str,'
        '"intent":{"genres":[],"origin_countries":[],"languages":[],"mood_descriptors":[],'
        '"similar_to":[],"max_runtime_minutes":null,"year_from":null,"year_to":null,'
        '"unwatched_only":true,"special_intent":null,"content_type":"both","top_n":5,'
        '"platforms":[]},"context_note":str}\n'
        '   "summary" is a one-line recap ("something light, short, a little offbeat"). '
        '"context_note" carries soft signals (runtime/energy/company) for ranking.\n'
    )


def _finalize(state: WizardState, profile: str, llm) -> dict:
    """Force a recommend turn. Used on cap hit or early-exit."""
    raw = llm.generate(_prompt(state, profile, force_finish=True),
                        role="reason", max_tokens=config.TOKENS_INTENT,
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
    cap_hit = state.turn_count >= config.WIZARD_MAX_QUESTIONS
    if force_finish or cap_hit:
        return _finalize(state, profile, ctx.llm)

    raw = ctx.llm.generate(_prompt(state, profile, force_finish=False),
                           role="reason", max_tokens=config.TOKENS_INTENT,
                           timeout=config.TIMEOUT_REASON)
    try:
        data = _parse_json_response(raw)
    except (json.JSONDecodeError, ValueError, TypeError) as exc:
        log.warning("Malformed wizard turn, finalizing instead: %s", exc)
        return _finalize(state, profile, ctx.llm)

    if not isinstance(data, dict):
        log.warning("Wizard turn returned non-object JSON, finalizing instead")
        return _finalize(state, profile, ctx.llm)

    if data.get("action") == "recommend":
        return {
            "action": "recommend",
            "summary": data.get("summary", "based on what you told me"),
            "intent": _safe_query_intent(data.get("intent", {})),
            "context_note": data.get("context_note", ""),
        }

    return {
        "action": "ask",
        "prompt": data.get("prompt", "What are you in the mood for?"),
        "subtext": data.get("subtext", ""),
        "chips": [c for c in data.get("chips", []) if c.get("value")],
        "multi": bool(data.get("multi", False)),
        "allow_free_text": bool(data.get("allow_free_text", True)),
    }
