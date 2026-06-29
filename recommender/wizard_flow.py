"""Deterministic wizard intake flow.

Owns the product-contract questions (content type, time, energy, tone) that
should not cost an LLM round-trip, plus Back/Edit navigation and the mapping
from structured answers into a ``QueryIntent`` seed. The LLM wizard
(``wizard.py``) is reduced to one optional adaptive follow-up after this intake.

Each deterministic ``ask`` turn is shaped like the existing LLM turn so
``_wizard_step.html`` can render both without branching.
"""

from __future__ import annotations

from dataclasses import asdict

from recommender.query_engine import QueryIntent, _safe_query_intent
from recommender.wizard import WizardState

# Order of deterministic intake steps; review follows the last one.
_DETERMINISTIC_ORDER = ["content_type", "time_window", "energy", "tone"]
# Answer keys cleared (from a given step onward) when an earlier answer is edited.
_CLEARABLE_ORDER = ["content_type", "time_window", "energy", "tone", "free_text"]

_CONTENT_TYPE_CHIPS = [
    {"label": "Movie", "value": "movie"},
    {"label": "Series", "value": "tv"},
    {"label": "Either", "value": "both"},
]

# Time windows differ by content type. Movie options never offer "under an hour"
# (feature films are rarely that short); TV options speak in episodes; the
# "either" group steers very short requests toward TV.
_TIME_WINDOWS = {
    "movie": [
        {"label": "Short (around 90 min)", "value": "short_movie", "max_runtime_minutes": 95},
        {"label": "Standard (up to 2 hours)", "value": "standard_movie", "max_runtime_minutes": 125},
        {"label": "However long it takes", "value": "no_limit", "max_runtime_minutes": None},
    ],
    "tv": [
        {"label": "A short episode (~30 min)", "value": "short_episode",
         "content_type": "tv", "max_runtime_minutes": 35},
        {"label": "One episode (~an hour)", "value": "one_episode",
         "content_type": "tv", "max_runtime_minutes": 60},
        {"label": "A few episodes", "value": "binge",
         "content_type": "tv", "max_runtime_minutes": None,
         "note": "open to watching several episodes in a row"},
    ],
    "both": [
        {"label": "Under an hour", "value": "under_hour",
         "content_type": "tv", "max_runtime_minutes": 60},
        {"label": "Around 90 minutes", "value": "around_90",
         "content_type": "both", "max_runtime_minutes": 95},
        {"label": "A couple of hours", "value": "two_hours_plus",
         "content_type": "both", "max_runtime_minutes": None},
    ],
}
_TIME_WINDOW_BY_VALUE = {
    opt["value"]: opt for group in _TIME_WINDOWS.values() for opt in group
}

_ENERGY_CHIPS = [
    {"label": "Easy", "value": "easy"},
    {"label": "Medium", "value": "medium"},
    {"label": "Locked in", "value": "locked_in"},
]
_ENERGY_NOTES = {
    "easy": "wants an easy, low-effort watch",
    "medium": "wants a moderately engaging watch",
    "locked_in": "is ready to fully focus and lock in",
}

_TONE_CHIPS = [
    {"label": "Funny", "value": "funny"},
    {"label": "Tense", "value": "tense"},
    {"label": "Warm", "value": "warm"},
    {"label": "Weird", "value": "weird"},
    {"label": "Thoughtful", "value": "thoughtful"},
    {"label": "Comfort", "value": "comfort"},
]

# Short adjectives used to phrase the review summary.
_TIME_SUMMARY_WORDS = {
    "short_movie": "short", "standard_movie": "standard-length", "no_limit": "any-length",
    "short_episode": "short", "one_episode": "one-episode", "binge": "bingeable",
    "under_hour": "short", "around_90": "around-90-minute", "two_hours_plus": "longer",
}
_ENERGY_SUMMARY_WORDS = {"easy": "easy", "medium": "medium-energy", "locked_in": "immersive"}
_TYPE_SUMMARY_WORDS = {"movie": "movie", "tv": "series", "both": "something"}

_STEP_LABELS = {
    "content_type": "Type",
    "time_window": "Time",
    "energy": "Energy",
    "tone": "Tone",
}


def _content_type(state: WizardState) -> str:
    ct = state.answers.get("content_type")
    return ct if ct in ("movie", "tv", "both") else "both"


def current_turn(state: WizardState) -> dict:
    """Return the renderable turn for ``state.step``.

    Deterministic ``ask`` steps return an LLM-turn-shaped dict; the review step
    returns an ``action == "review"`` payload (see ``review_model``).
    """
    step = state.step
    if step == "content_type":
        return {
            "action": "ask", "step": "content_type",
            "prompt": "Movie or series tonight?", "subtext": "",
            "chips": list(_CONTENT_TYPE_CHIPS), "multi": False,
            "allow_free_text": False, "can_go_back": False,
        }
    if step == "time_window":
        ct = _content_type(state)
        chips = [{"label": o["label"], "value": o["value"]} for o in _TIME_WINDOWS[ct]]
        return {
            "action": "ask", "step": "time_window",
            "prompt": "How much time do you have?", "subtext": "",
            "chips": chips, "multi": False,
            "allow_free_text": False, "can_go_back": True,
        }
    if step == "energy":
        return {
            "action": "ask", "step": "energy",
            "prompt": "What's your energy like?", "subtext": "",
            "chips": list(_ENERGY_CHIPS), "multi": False,
            "allow_free_text": False, "can_go_back": True,
        }
    if step == "tone":
        return {
            "action": "ask", "step": "tone",
            "prompt": "Any particular tone?",
            "subtext": "Optional — pick any that fit, or tell me in your own words.",
            "chips": list(_TONE_CHIPS), "multi": True,
            "allow_free_text": True, "can_go_back": True,
        }
    if step == "review":
        return review_model(state)
    # Adaptive and any unexpected step are handled by the LLM path in web.py.
    return {"action": "adaptive", "step": step}


def _next_step(step: str) -> str:
    if step in _DETERMINISTIC_ORDER:
        i = _DETERMINISTIC_ORDER.index(step)
        if i + 1 < len(_DETERMINISTIC_ORDER):
            return _DETERMINISTIC_ORDER[i + 1]
        return "review"
    return step


def apply_answer(state: WizardState, step: str, selected: list[str], free_text: str) -> WizardState:
    """Record an answer for ``step`` and advance to the next step."""
    selected = [s for s in (selected or []) if s]
    if step == "tone":
        state.answers["tone"] = selected
    elif selected:
        state.answers[step] = selected[0]
    free_text = (free_text or "").strip()
    if free_text:
        state.answers["free_text"] = free_text
    state.step = _next_step(step)
    return state


def previous_step(state: WizardState) -> WizardState:
    """Move to the previous step without touching recorded answers."""
    step = state.step
    if step == "adaptive":
        state.step = "review"
    elif step == "review":
        state.step = _DETERMINISTIC_ORDER[-1]
    elif step in _DETERMINISTIC_ORDER:
        i = _DETERMINISTIC_ORDER.index(step)
        if i > 0:
            state.step = _DETERMINISTIC_ORDER[i - 1]
    return state


def edit_step(state: WizardState, step: str) -> WizardState:
    """Jump back to ``step`` and clear it plus any dependent later answers."""
    if step in _CLEARABLE_ORDER:
        for key in _CLEARABLE_ORDER[_CLEARABLE_ORDER.index(step):]:
            state.answers.pop(key, None)
    state.step = step if step in _DETERMINISTIC_ORDER else "content_type"
    state.review_seen = False
    return state


def _summary(answers: dict) -> str:
    type_word = _TYPE_SUMMARY_WORDS.get(answers.get("content_type"), "something")
    adjectives = []
    time_word = _TIME_SUMMARY_WORDS.get(answers.get("time_window"))
    if time_word:
        adjectives.append(time_word)
    energy_word = _ENERGY_SUMMARY_WORDS.get(answers.get("energy"))
    if energy_word:
        adjectives.append(energy_word)
    lead = f"a {', '.join(adjectives)} " if adjectives else "a "
    summary = (lead + type_word).strip()
    tone = answers.get("tone") or []
    if tone:
        summary += f" with a {', '.join(tone)} tone"
    return summary


def review_model(state: WizardState) -> dict:
    """Build the 'what I heard' review surface from structured answers."""
    answers = state.answers
    rows = []
    for step in _DETERMINISTIC_ORDER:
        if step == "tone":
            value = answers.get("tone") or []
            display = ", ".join(value) if value else "any"
        else:
            value = answers.get(step)
            if value is None:
                continue
            display = _display_value(step, value)
        rows.append({"step": step, "label": _STEP_LABELS[step], "display": display})
    return {"action": "review", "summary": _summary(answers), "answers": rows}


def _display_value(step: str, value) -> str:
    if step == "content_type":
        return {"movie": "Movie", "tv": "Series", "both": "Either"}.get(value, str(value))
    if step == "time_window":
        opt = _TIME_WINDOW_BY_VALUE.get(value)
        return opt["label"] if opt else str(value)
    if step == "energy":
        return {"easy": "Easy", "medium": "Medium", "locked_in": "Locked in"}.get(value, str(value))
    return str(value)


def build_recommendation_seed(state: WizardState) -> tuple[dict, str, str]:
    """Map structured answers to (intent_dict, context_note, summary).

    Deterministic content type and runtime are authoritative here; the LLM may
    only enrich softer fields later via ``merge_intent_with_seed``.
    """
    answers = state.answers
    intent = _safe_query_intent({})
    intent_dict = asdict(intent)
    intent_dict["content_type"] = _content_type(state)

    note_parts = []
    tw = _TIME_WINDOW_BY_VALUE.get(answers.get("time_window"))
    if tw:
        if "content_type" in tw:
            intent_dict["content_type"] = tw["content_type"]
        intent_dict["max_runtime_minutes"] = tw["max_runtime_minutes"]
        if tw.get("note"):
            note_parts.append(f"Viewer {tw['note']}.")

    tone = answers.get("tone") or []
    if tone:
        intent_dict["mood_descriptors"] = list(tone)

    energy_note = _ENERGY_NOTES.get(answers.get("energy"))
    if energy_note:
        note_parts.append(f"Viewer {energy_note}.")

    free_text = (answers.get("free_text") or "").strip()
    if free_text:
        note_parts.append(f"In their words: {free_text}")

    context_note = " ".join(note_parts).strip()
    return intent_dict, context_note, _summary(answers)


def merge_intent_with_seed(llm_intent: QueryIntent, seed: dict) -> QueryIntent:
    """Merge an adaptive LLM intent over the deterministic seed.

    Deterministic ``content_type`` and ``max_runtime_minutes`` win; the LLM may
    add genres, mood descriptors, similar titles, and other soft signals.
    """
    merged = asdict(llm_intent)
    merged["content_type"] = seed.get("content_type", merged.get("content_type", "both"))
    merged["max_runtime_minutes"] = seed.get("max_runtime_minutes")
    # Preserve deterministic tone if the LLM dropped it.
    if seed.get("mood_descriptors") and not merged.get("mood_descriptors"):
        merged["mood_descriptors"] = list(seed["mood_descriptors"])
    return _safe_query_intent(merged)
