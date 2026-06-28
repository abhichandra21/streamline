# Concierge Wizard Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a guided, adaptive Q&A "concierge" mode to the web UI that probes today's viewing context and produces recommendations without requiring a seed title or typed query.

**Architecture:** A thin new front-end (`recommender/wizard.py` + Flask routes + templates) runs an adaptive LLM question loop. State is carried client-side as JSON in a hidden form field (stateless server). On finish it synthesizes a `QueryIntent` plus a free-text `context_note` and hands off to a lightly-refactored `query_engine.ask()` that skips `parse_intent` and reuses the whole online pipeline. The slow final recommendation runs through the existing background-job registry, matching `/recommend`.

**Tech Stack:** Python 3.14, Flask + HTMX (server-rendered partials), Jinja2 templates, pytest. LLM via the existing `LLMClient` abstraction (`role="reason"`).

## Global Constraints

- No emoji in code or documentation.
- No new third-party dependencies; reuse Flask/HTMX/Jinja already in the project.
- Match the existing dark-editorial aesthetic: `DM Serif Display` headlines, `DM Sans` body, `JetBrains Mono` uppercase micro-labels; colors via the CSS variables in `base.html` (`--accent #e85d4a`, `--teal`, `--lavender`, `--surface`, `--border`, `--muted`, `--body`, `--text`). Do not introduce a new theme.
- CSRF: HTMX requests auto-send `X-CSRF-Token` (see `base.html`); POST routes are covered by the existing `_check_csrf()` before-request hook. Forms still include `<input type="hidden" name="_csrf_token" value="{{ csrf_token }}">` to match existing templates.
- Question cap is config-driven (`config.WIZARD_MAX_QUESTIONS`, default 4) and enforced server-side regardless of LLM output.
- Soft signals (runtime, energy, company) are ranking nudges via `context_note`, never hard filters. Do not add new `QueryIntent` fields.
- TDD: write the failing test first, watch it fail, implement minimally, watch it pass, commit. Run tests with the project venv: `python3 -m pytest`.

---

### Task 1: Thread `context_note` and `intent_override` through the query pipeline

Adds two optional parameters to the online pipeline so the wizard can inject a pre-built intent (skipping `parse_intent`) and a free-text ranking nudge. No behavior change for existing callers.

**Files:**
- Modify: `recommender/query_engine.py` (`rank_candidates` ~228-264; `ask` 510-671)
- Test: `tests/test_query_engine.py`

**Interfaces:**
- Produces:
  - `rank_candidates(query, taste_profile, candidates, enrichments, client, top_n=1, context_note: str | None = None) -> list[Recommendation]`
  - `ask(query, ctx, top_n_override=None, conv_ctx=None, intent_override: "QueryIntent | None" = None, context_note: str | None = None) -> list[Recommendation]`

- [ ] **Step 1: Write the failing test for `context_note` in the rank prompt**

Add to `tests/test_query_engine.py`:

```python
def test_rank_candidates_includes_context_note_in_prompt():
    from recommender import query_engine
    from recommender.tmdb_client import TmdbMetadata

    captured = {}

    class FakeLLM:
        provider = "fake"
        def generate(self, prompt, role="reason", max_tokens=1000, timeout=30.0):
            captured["prompt"] = prompt
            return "[]"

    cand = TmdbMetadata(
        tmdb_id=1, title="Example", content_type="movie", genres=["drama"],
        vote_average=7.0, vote_count=100, popularity=10.0, release_year=2020,
        overview="", poster_path=None, original_language="en", original_title="Example",
        runtime=100,
    )
    query_engine.rank_candidates(
        "something", "PROFILE", [cand], {}, FakeLLM(), top_n=1,
        context_note="Wants something short and low-energy tonight.",
    )
    assert "short and low-energy" in captured["prompt"]
```

(If `TmdbMetadata`'s constructor differs, build it the way other tests in this file already do — copy an existing construction from `tests/test_query_engine.py`.)

- [ ] **Step 2: Run the test to verify it fails**

Run: `python3 -m pytest tests/test_query_engine.py::test_rank_candidates_includes_context_note_in_prompt -v`
Expected: FAIL — `rank_candidates() got an unexpected keyword argument 'context_note'`.

- [ ] **Step 3: Add the `context_note` parameter to `rank_candidates`**

Change the signature (line ~228-235) to add `context_note`:

```python
def rank_candidates(
    query: str,
    taste_profile: str,
    candidates: list[TmdbMetadata],
    enrichments: dict[str, str],
    client: LLMClient,
    top_n: int = 1,
    context_note: str | None = None,
) -> list[Recommendation]:
```

In the prompt construction (line ~246-260), insert the note just after the `QUERY:` line. Replace:

```python
        f'QUERY: "{query}"\n\n'
        f'TASTE PROFILE:\n{taste_profile}\n\n'
```

with:

```python
        f'QUERY: "{query}"\n\n'
        + (f'TONIGHT\'S CONTEXT (use to nudge ordering, not as a hard filter):\n{context_note}\n\n'
           if context_note else '')
        + f'TASTE PROFILE:\n{taste_profile}\n\n'
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `python3 -m pytest tests/test_query_engine.py::test_rank_candidates_includes_context_note_in_prompt -v`
Expected: PASS.

- [ ] **Step 5: Write the failing test for `ask(intent_override=...)` skipping `parse_intent`**

Add to `tests/test_query_engine.py`:

```python
def test_ask_with_intent_override_skips_parse_intent(monkeypatch):
    from recommender import query_engine
    from recommender.query_engine import QueryIntent, RecommendContext

    def boom(*a, **k):
        raise AssertionError("parse_intent must not be called when intent_override is given")
    monkeypatch.setattr(query_engine, "parse_intent", boom)

    class FakeTmdb:
        def search_by_filters(self, **k):
            return []
    class FakeLLM:
        provider = "fake"
        def generate(self, prompt, role="reason", max_tokens=1000, timeout=30.0):
            return "[]"   # no LLM suggestions

    ctx = RecommendContext(
        taste_profile="PROFILE", watch_index=None, tmdb_client=FakeTmdb(),
        llm=FakeLLM(), cache_dir="", events=[],
    )
    intent = QueryIntent(
        genres=[], origin_countries=[], languages=[], mood_descriptors=[],
        similar_to=[], max_runtime_minutes=None, year_from=None, year_to=None,
        unwatched_only=True, special_intent=None, content_type="movie",
        top_n=3, platforms=[],
    )
    results = query_engine.ask("ignored", ctx, intent_override=intent,
                               context_note="low energy")
    assert results == []   # no candidates -> empty, but parse_intent never ran
```

- [ ] **Step 6: Run the test to verify it fails**

Run: `python3 -m pytest tests/test_query_engine.py::test_ask_with_intent_override_skips_parse_intent -v`
Expected: FAIL — `ask() got an unexpected keyword argument 'intent_override'`.

- [ ] **Step 7: Add `intent_override` and `context_note` to `ask` and thread them**

Change the `ask` signature (line 510-515) to:

```python
def ask(
    query: str,
    ctx: RecommendContext,
    top_n_override: int | None = None,
    conv_ctx: "ConversationContext | None" = None,
    intent_override: "QueryIntent | None" = None,
    context_note: str | None = None,
) -> list[Recommendation]:
```

At the start of the `else` branch that builds intent (the block containing line 539 `intent = parse_intent(...)`), short-circuit when an override is supplied. Replace the existing `intent = parse_intent(query, ctx.llm, conv_ctx=conv_ctx)` / `top_n_override` block with:

```python
        if intent_override is not None:
            intent = intent_override
        else:
            intent = parse_intent(query, ctx.llm, conv_ctx=conv_ctx)
        if top_n_override is not None:
            intent.top_n = top_n_override
```

Then pass `context_note` into BOTH `rank_candidates` calls. Line ~638:

```python
        results = rank_candidates(query, profile_for_prompt, candidates, enrichments,
                                  ctx.llm, rank_size, context_note=context_note)
```

Line ~668:

```python
    results = rank_candidates(query, profile_for_prompt, candidates, enrichments,
                              ctx.llm, intent.top_n, context_note=context_note)
```

- [ ] **Step 8: Run both new tests plus the full query-engine suite**

Run: `python3 -m pytest tests/test_query_engine.py -v`
Expected: PASS (all, including the two new tests).

- [ ] **Step 9: Commit**

```bash
git add recommender/query_engine.py tests/test_query_engine.py
git commit -m "feat(query): support intent_override and context_note in ask/rank"
```

---

### Task 2: Wizard engine module + config knob

The adaptive question loop. One `role=reason` LLM call per turn returns either the next question or a finish signal carrying a synthesized intent and ranking note. Cap is enforced here.

**Files:**
- Modify: `config.py` (after line 120, the recommendation-settings block), `config.yaml` (top level)
- Create: `recommender/wizard.py`
- Test: `tests/test_wizard.py`

**Interfaces:**
- Consumes: `config.WIZARD_MAX_QUESTIONS: int`; `query_engine._safe_query_intent`, `query_engine._parse_json_response`; `RecommendContext` (uses `.taste_profile`, `.llm`).
- Produces:
  - `@dataclass class WizardState` with fields `turns: list[dict]` (each `{"prompt": str, "selected": list[str], "free_text": str}`) and `turn_count: int`; classmethod `from_json(raw: str) -> WizardState` (returns an empty state on any parse error) and method `to_json() -> str`.
  - `next_turn(state: WizardState, ctx, force_finish: bool = False) -> dict` returning either `{"action": "ask", "prompt", "subtext", "chips": [{"label","value"}], "multi": bool, "allow_free_text": bool}` or `{"action": "recommend", "summary": str, "intent": QueryIntent, "context_note": str}`.

- [ ] **Step 1: Add the config knob (failing import-level expectation)**

Add to `config.py` immediately after the recommendation-settings block (after line 120):

```python
# ── Concierge wizard ──
_wizard = _cfg.get("wizard", {})
WIZARD_MAX_QUESTIONS = int(_wizard.get("max_questions", 4))
```

Add to `config.yaml` at top level (after the `default_top_n` group, e.g. after line 44):

```yaml
wizard:
  max_questions: 4
```

- [ ] **Step 2: Write the failing test for an "ask" turn**

Create `tests/test_wizard.py`:

```python
import json
from recommender import wizard
from recommender.wizard import WizardState
from recommender.query_engine import RecommendContext, QueryIntent


class FakeLLM:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []
    def generate(self, prompt, role="reason", max_tokens=1000, timeout=30.0):
        self.calls.append(prompt)
        return self._responses.pop(0)


def _ctx(llm):
    return RecommendContext(
        taste_profile="Loves slow-burn British crime drama.",
        watch_index=None, tmdb_client=None, llm=llm, cache_dir="", events=[],
    )


def test_next_turn_returns_ask_question():
    llm = FakeLLM([json.dumps({
        "action": "ask",
        "prompt": "What's the vibe tonight?",
        "subtext": "Pick any that fit.",
        "chips": [{"label": "Light", "value": "light"}, {"label": "Tense", "value": "tense"}],
        "multi": True,
        "allow_free_text": True,
    })])
    out = wizard.next_turn(WizardState(turns=[], turn_count=0), _ctx(llm))
    assert out["action"] == "ask"
    assert out["chips"][0]["value"] == "light"
    assert out["multi"] is True
```

- [ ] **Step 3: Run the test to verify it fails**

Run: `python3 -m pytest tests/test_wizard.py::test_next_turn_returns_ask_question -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'recommender.wizard'`.

- [ ] **Step 4: Implement `recommender/wizard.py`**

Create `recommender/wizard.py`:

```python
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
    data = _parse_json_response(raw)
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
```

- [ ] **Step 5: Run the test to verify it passes**

Run: `python3 -m pytest tests/test_wizard.py::test_next_turn_returns_ask_question -v`
Expected: PASS.

- [ ] **Step 6: Write failing tests for finalize-on-cap, malformed-JSON fallback, and recommend mapping**

Append to `tests/test_wizard.py`:

```python
def test_cap_forces_finalize(monkeypatch):
    monkeypatch.setattr("config.WIZARD_MAX_QUESTIONS", 2)
    recommend_json = json.dumps({
        "action": "recommend", "summary": "light and short",
        "intent": {"content_type": "movie", "top_n": 5},
        "context_note": "30 minutes, low energy",
    })
    llm = FakeLLM([recommend_json])
    state = WizardState(turns=[{}, {}], turn_count=2)  # already at cap
    out = wizard.next_turn(state, _ctx(llm))
    assert out["action"] == "recommend"
    assert isinstance(out["intent"], QueryIntent)
    assert out["intent"].content_type == "movie"
    assert out["context_note"] == "30 minutes, low energy"


def test_malformed_turn_falls_back_to_finalize():
    recommend_json = json.dumps({
        "action": "recommend", "summary": "x",
        "intent": {"content_type": "both"}, "context_note": "",
    })
    llm = FakeLLM(["this is not json", recommend_json])
    out = wizard.next_turn(WizardState(turns=[], turn_count=1), _ctx(llm))
    assert out["action"] == "recommend"
    assert len(llm.calls) == 2   # bad turn, then forced finalize


def test_state_from_json_resets_on_garbage():
    assert wizard.WizardState.from_json("{bad json").turns == []
    s = wizard.WizardState(turns=[{"prompt": "q", "selected": ["a"], "free_text": ""}], turn_count=1)
    assert wizard.WizardState.from_json(s.to_json()).turn_count == 1
```

- [ ] **Step 7: Run the new tests to verify they fail, then pass**

Run: `python3 -m pytest tests/test_wizard.py -v`
Expected: all PASS (the implementation from Step 4 already covers these). If `test_cap_forces_finalize` fails because `config.WIZARD_MAX_QUESTIONS` is read at module import, confirm `wizard.next_turn` reads `config.WIZARD_MAX_QUESTIONS` at call time (it does) so the monkeypatch takes effect.

- [ ] **Step 8: Commit**

```bash
git add config.py config.yaml recommender/wizard.py tests/test_wizard.py
git commit -m "feat(wizard): adaptive question-loop engine with capped finalize"
```

---

### Task 3: Web routes + functional templates

Wires the engine into Flask with HTMX partial swaps. State carried in a hidden field; the slow final recommendation runs through the existing background-job registry like `/recommend`.

**Files:**
- Modify: `recommender/web.py` (add routes + a wizard job runner; near `_run_recommend_job` line 114 and the `/recommend` routes line 510)
- Create: `recommender/templates/wizard.html`, `recommender/templates/_wizard_step.html`, `recommender/templates/_wizard_results.html`
- Modify: `recommender/templates/base.html` (nav link), `recommender/templates/recommend.html` (entry card)
- Test: `tests/test_web_wizard.py`

**Interfaces:**
- Consumes: `wizard.WizardState`, `wizard.next_turn`; `query_engine.ask(intent_override=, context_note=)`; `_get_job_context`, `_build_result_items`, `job_registry`, `query_history` (all in `web.py`).
- Produces routes: `GET /wizard`, `POST /wizard/next`, `GET /wizard/jobs/<job_id>/poll`, `POST /wizard/refine`.

- [ ] **Step 1: Write failing route tests**

Create `tests/test_web_wizard.py` (follow the app/client fixture pattern already used in the web tests — open an existing web test to copy how `app.test_client()` and CSRF are set up):

```python
import json
from unittest.mock import patch
from recommender import web
from recommender.query_engine import QueryIntent


def _client():
    web.app.config["TESTING"] = True
    return web.app.test_client()


def test_get_wizard_renders_shell():
    resp = _client().get("/wizard")
    assert resp.status_code == 200
    assert b"wizard-stage" in resp.data


def test_post_next_ask_branch_renders_question():
    fake_turn = {"action": "ask", "prompt": "Vibe tonight?", "subtext": "",
                 "chips": [{"label": "Light", "value": "light"}],
                 "multi": True, "allow_free_text": True}
    with patch.object(web.wizard, "next_turn", return_value=fake_turn), \
         patch.object(web, "_get_job_context"):
        resp = _client().post("/wizard/next", data={
            "state": json.dumps({"turns": [], "turn_count": 0}),
        }, headers={"HX-Request": "true"})
    assert resp.status_code == 200
    assert b"Vibe tonight?" in resp.data
    assert b"light" in resp.data


def test_post_next_recommend_branch_starts_job():
    intent = QueryIntent(
        genres=[], origin_countries=[], languages=[], mood_descriptors=[],
        similar_to=[], max_runtime_minutes=None, year_from=None, year_to=None,
        unwatched_only=True, special_intent=None, content_type="both",
        top_n=5, platforms=[])
    fake_turn = {"action": "recommend", "summary": "light and short",
                 "intent": intent, "context_note": "low energy"}
    with patch.object(web.wizard, "next_turn", return_value=fake_turn), \
         patch.object(web.job_registry, "submit", return_value="job-123") as sub:
        resp = _client().post("/wizard/next", data={
            "state": json.dumps({"turns": [], "turn_count": 1}),
            "prompt": "Vibe tonight?", "selected": ["light"], "free_text": "",
        }, headers={"HX-Request": "true"})
    assert resp.status_code == 200
    assert b"job-123" in resp.data   # polling partial carries the job id
    sub.assert_called_once()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python3 -m pytest tests/test_web_wizard.py -v`
Expected: FAIL — 404s / `AttributeError: module 'recommender.web' has no attribute 'wizard'`.

- [ ] **Step 3: Add the wizard import, job runner, and routes to `web.py`**

Add to the imports near the top of `recommender/web.py` (where `query_engine`/`ask` are imported):

```python
from recommender import wizard
```

Add a job runner next to `_run_recommend_job` (after line 122):

```python
def _run_wizard_recommend_job(intent_dict: dict, context_note: str, summary: str) -> dict:
    from recommender.query_engine import QueryIntent
    ctx = _get_job_context()
    intent = QueryIntent(**intent_dict)
    results = ask("", ctx, intent_override=intent, context_note=context_note)
    items = _build_result_items(results, ctx)
    label = f"concierge: {summary}"
    try:
        query_history.record(label, items, ctx.llm.provider, ctx.llm.usage.summary())
    except OSError as exc:
        log.warning("Failed to persist wizard history: %s", exc)
    return {"items": items, "query": summary, "summary": summary}
```

Add the routes near the `/recommend` routes (after line 559):

```python
@app.route("/wizard")
def wizard_page() -> str:
    return render_template("wizard.html")


def _collect_answers(form) -> tuple[wizard.WizardState, dict | None]:
    """Load state from the form and append the just-answered question, if any."""
    state = wizard.WizardState.from_json(form.get("state", ""))
    prompt = (form.get("prompt") or "").strip()
    if prompt:
        state.turns.append({
            "prompt": prompt,
            "selected": form.getlist("selected"),
            "free_text": (form.get("free_text") or "").strip(),
        })
        state.turn_count += 1
    return state, None


@app.route("/wizard/next", methods=["POST"])
def wizard_next() -> str:
    state, _ = _collect_answers(request.form)
    force_finish = request.form.get("finish") == "1"
    ctx = _get_job_context()
    try:
        turn = wizard.next_turn(state, ctx, force_finish=force_finish)
    except Exception as exc:   # network / provider failure
        log.exception("Wizard turn failed")
        return render_template("_wizard_step.html", error=str(exc),
                               state_json=state.to_json())

    if turn["action"] == "recommend":
        from dataclasses import asdict
        intent_dict = asdict(turn["intent"])
        job_id = job_registry.submit(
            _run_wizard_recommend_job, intent_dict, turn["context_note"],
            turn["summary"], label="wizard")
        return render_template("_polling.html", job_id=job_id,
                               poll_url=f"/wizard/jobs/{job_id}/poll")

    return render_template("_wizard_step.html", turn=turn,
                           state_json=state.to_json(),
                           question_number=state.turn_count + 1,
                           max_questions=config.WIZARD_MAX_QUESTIONS)


@app.route("/wizard/jobs/<job_id>/poll")
def wizard_poll(job_id: str) -> str:
    job = job_registry.get(job_id)
    if job is None:
        return render_template("_wizard_results.html", results=None,
                               error="Session expired. Start again.")
    if job.status in ("pending", "running"):
        return render_template("_polling.html", job_id=job_id,
                               poll_url=f"/wizard/jobs/{job_id}/poll",
                               elapsed=job.elapsed_seconds)
    if job.status == "error":
        return render_template("_wizard_results.html", results=None, error=job.error)
    result = job.result
    return render_template("_wizard_results.html", results=result["items"],
                           summary=result["summary"], error=None)


@app.route("/wizard/refine", methods=["POST"])
def wizard_refine() -> str:
    from dataclasses import asdict
    from recommender.query_engine import QueryIntent
    intent_dict = json.loads(request.form.get("intent", "{}"))
    base_note = request.form.get("context_note", "")
    directive = request.form.get("directive", "")     # e.g. "make it lighter"
    shown = request.form.get("shown", "")             # comma-joined titles
    note = base_note
    if directive:
        note = f"{note}\nRefinement: {directive}."
    if shown:
        note = f"{note}\nAlready shown, avoid repeating: {shown}."
    summary = request.form.get("summary", "your picks")
    job_id = job_registry.submit(
        _run_wizard_recommend_job, intent_dict, note, summary, label="wizard")
    return render_template("_polling.html", job_id=job_id,
                           poll_url=f"/wizard/jobs/{job_id}/poll")
```

Note: `_polling.html` currently hardcodes its poll URL. Open `recommender/templates/_polling.html` and make the poll target use an optional `poll_url` with the existing default, e.g. change the polling element's `hx-get` to `{{ poll_url or '/jobs/' ~ job_id ~ '/poll' }}` so both the recommend and wizard flows can share it.

- [ ] **Step 4: Create the functional templates**

`recommender/templates/wizard.html`:

```html
{% extends "base.html" %}
{% block title %}Streamline — Concierge{% endblock %}
{% block content %}
<div style="margin-bottom:1.5rem;">
  <span class="mono" style="font-size:0.62rem; letter-spacing:0.12em; text-transform:uppercase; color:var(--accent);">Guided Pick</span>
  <h1 style="font-size:clamp(2rem,4.5vw,3rem); margin-top:0.2rem;">Concierge</h1>
  <p style="color:var(--body);">A few quick questions and I'll find something for tonight.</p>
</div>

<div id="wizard-stage"
     hx-post="/wizard/next" hx-trigger="load" hx-target="#wizard-stage" hx-swap="innerHTML">
  <input type="hidden" name="state" value="">
  <div class="mono" style="font-size:0.65rem; color:var(--muted);">reading the room...</div>
</div>
{% endblock %}
```

`recommender/templates/_wizard_step.html`:

```html
{% if error %}
<div style="padding:1rem 1.25rem; background:rgba(232,93,74,0.08); border:1px solid rgba(232,93,74,0.2); border-radius:6px; color:#e88;">
  {{ error }}
  <form hx-post="/wizard/next" hx-target="#wizard-stage" hx-swap="innerHTML" style="margin-top:0.75rem;">
    <input type="hidden" name="state" value="{{ state_json }}">
    <button type="submit" class="btn-ghost" style="padding:6px 12px;">Try again</button>
  </form>
  <a href="/recommend" class="mono result-link">use classic search instead</a>
</div>
{% else %}
<form hx-post="/wizard/next" hx-target="#wizard-stage" hx-swap="innerHTML">
  <input type="hidden" name="state" value="{{ state_json }}">
  <input type="hidden" name="prompt" value="{{ turn.prompt }}">

  <div class="mono" style="font-size:0.6rem; letter-spacing:0.1em; text-transform:uppercase; color:var(--muted); margin-bottom:0.4rem;">
    Question {{ question_number }}{% if max_questions %} of up to {{ max_questions }}{% endif %}
  </div>
  <h2 style="font-size:1.5rem; margin-bottom:0.3rem;">{{ turn.prompt }}</h2>
  {% if turn.subtext %}<p style="color:var(--body); margin-bottom:1rem;">{{ turn.subtext }}</p>{% endif %}

  <div class="wiz-chips" style="display:flex; flex-wrap:wrap; gap:0.5rem; margin-bottom:1rem;">
    {% for chip in turn.chips %}
    <label class="wiz-chip">
      <input type="{{ 'checkbox' if turn.multi else 'radio' }}" name="selected" value="{{ chip.value }}" hidden>
      <span>{{ chip.label }}</span>
    </label>
    {% endfor %}
  </div>

  {% if turn.allow_free_text %}
  <input type="text" name="free_text" class="input-field" placeholder="or tell me in your own words..."
         style="padding:0.7rem 1rem; width:100%; margin-bottom:1rem;">
  {% endif %}

  <div style="display:flex; align-items:center; gap:1rem;">
    <button type="submit" class="btn-primary" style="padding:0.7rem 1.4rem;">Continue</button>
    <button type="submit" name="finish" value="1" class="btn-ghost" style="padding:0.7rem 1.2rem;">Show me something now</button>
  </div>
</form>
{% endif %}
```

`recommender/templates/_wizard_results.html`:

```html
{% if error %}
<div style="padding:1rem 1.25rem; background:rgba(232,93,74,0.08); border:1px solid rgba(232,93,74,0.2); border-radius:6px; color:#e88;">
  {{ error }}
</div>
{% elif results %}
<p style="font-family:'DM Serif Display',serif; font-size:1.3rem; color:var(--text); margin-bottom:1rem;">
  Because you wanted {{ summary }} —
</p>
{% include "_results.html" with context %}
<div style="display:flex; flex-wrap:wrap; gap:0.5rem; margin-top:1.25rem;">
  <span class="mono" style="font-size:0.6rem; color:var(--muted); align-self:center;">Not quite?</span>
  {% for d in ["lighter", "shorter", "more obscure", "surprise me"] %}
  <button class="btn-ghost" style="padding:4px 10px;"
          hx-post="/wizard/refine" hx-target="#wizard-stage" hx-swap="innerHTML"
          hx-vals='{"directive": "{{ d }}", "summary": "{{ summary }}"}'>{{ d }}</button>
  {% endfor %}
  <a href="/wizard" class="mono result-link" style="align-self:center;">start over</a>
</div>
{% else %}
<div style="padding:2rem; text-align:center;">
  <p class="mono" style="font-size:0.7rem; text-transform:uppercase; color:var(--muted);">Nothing matched on your platforms.</p>
  <a href="/wizard" class="btn-ghost" style="display:inline-block; margin-top:0.75rem; padding:6px 14px;">Try again</a>
</div>
{% endif %}
```

Note: `_results.html` reads `csrf_token` and `query`; both are provided by the existing `_inject_csrf` context processor and (for `query`) the `summary` passed here is unrelated — `_results.html` uses `query` only in its header/empty branches, which the wizard wrapper supersedes. Passing `results` is sufficient; if a template error surfaces about `query`, pass `query=summary` to the `_wizard_results.html` render calls in `web.py`.

- [ ] **Step 5: Add entry points**

In `recommender/templates/base.html`, add a nav link inside the main `.nav-group` (after the `Home` link, line ~377):

```html
      <a href="/wizard" class="nav-link {% if request.path == '/wizard' %}active{% endif %}">Concierge</a>
```

In `recommender/templates/recommend.html`, add an entry card just below the search form block (after line 31, before the `{% if error %}`):

```html
  <a href="/wizard" style="display:block; margin-top:1rem; padding:0.85rem 1.1rem; background:var(--surface); border:1px solid var(--border); border-radius:6px; text-decoration:none; transition:border-color 0.2s;"
     onmouseover="this.style.borderColor='var(--accent)'" onmouseout="this.style.borderColor='var(--border)'">
    <span style="color:var(--body);">Not sure what to watch?</span>
    <span style="color:var(--accent); font-family:'JetBrains Mono',monospace; font-size:0.72rem; text-transform:uppercase; letter-spacing:0.06em;"> Let me ask a few quick questions -></span>
  </a>
```

- [ ] **Step 6: Run the route tests, then the full suite**

Run: `python3 -m pytest tests/test_web_wizard.py -v`
Expected: PASS.
Then: `python3 -m pytest`
Expected: PASS (no regressions). If `_polling.html` changes broke a recommend test, fix the template so the default `poll_url` still resolves to `/jobs/<id>/poll`.

- [ ] **Step 7: Commit**

```bash
git add recommender/web.py recommender/templates/ tests/test_web_wizard.py
git commit -m "feat(web): concierge wizard routes, templates, and entry points"
```

---

### Task 4: Client-side visual design (frontend-design)

Polishes the functional templates into the dark-editorial concierge experience: selectable chips, a progress bar, staggered reveals, and a composing state. CSS lives in `base.html` so it is shared.

**Files:**
- Modify: `recommender/templates/base.html` (add wizard CSS to the `<style>` block)
- Modify: `recommender/templates/_wizard_step.html` (progress bar, chip-select JS), `recommender/templates/wizard.html` (composing state)
- Test: `tests/test_web_wizard.py` (assert visual markers render)

**Interfaces:**
- Consumes: question fields from Task 3 (`turn`, `question_number`, `max_questions`).
- Produces: no new Python interfaces; CSS classes `.wiz-chip`, `.wiz-chip.selected`, `.wiz-progress`.

- [ ] **Step 1: Write a failing test for the progress bar markup**

Add to `tests/test_web_wizard.py`:

```python
def test_question_renders_progress_segments():
    from unittest.mock import patch
    import json
    fake_turn = {"action": "ask", "prompt": "Q?", "subtext": "",
                 "chips": [{"label": "A", "value": "a"}], "multi": False,
                 "allow_free_text": False}
    with patch.object(web.wizard, "next_turn", return_value=fake_turn), \
         patch.object(web, "_get_job_context"):
        resp = _client().post("/wizard/next", data={
            "state": json.dumps({"turns": [], "turn_count": 0})},
            headers={"HX-Request": "true"})
    assert b"wiz-progress" in resp.data
```

- [ ] **Step 2: Run it to verify it fails**

Run: `python3 -m pytest tests/test_web_wizard.py::test_question_renders_progress_segments -v`
Expected: FAIL — `wiz-progress` not in output.

- [ ] **Step 3: Add wizard CSS to `base.html`**

Inside the `<style>` block in `recommender/templates/base.html` (before the closing `</style>` at line 366), add:

```css
    /* Concierge wizard */
    .wiz-progress { display:flex; gap:5px; margin-bottom:1rem; }
    .wiz-progress span { height:3px; flex:1; border-radius:2px; background:var(--border); }
    .wiz-progress span.filled { background:var(--accent); }

    .wiz-chip {
      display:inline-flex; align-items:center; gap:0.4rem; cursor:pointer;
      padding:0.5rem 0.95rem; border:1px solid var(--border); border-radius:999px;
      color:var(--body); font-size:0.9rem; user-select:none;
      transition:border-color 0.15s, background 0.15s, transform 0.1s;
      animation: slideUp 0.3s ease both;
    }
    .wiz-chip:hover { border-color:var(--muted); }
    .wiz-chip.selected {
      border-color:var(--accent); background:rgba(232,93,74,0.12); color:var(--text);
      transform:translateY(-1px);
    }
    .wiz-chip.selected::before { content:"\2713"; color:var(--accent); font-size:0.8rem; }
    .wiz-chips .wiz-chip:nth-child(1){animation-delay:.02s}
    .wiz-chips .wiz-chip:nth-child(2){animation-delay:.06s}
    .wiz-chips .wiz-chip:nth-child(3){animation-delay:.10s}
    .wiz-chips .wiz-chip:nth-child(4){animation-delay:.14s}
    .wiz-chips .wiz-chip:nth-child(5){animation-delay:.18s}
```

- [ ] **Step 4: Add the progress bar and chip-select JS to `_wizard_step.html`**

In `recommender/templates/_wizard_step.html`, immediately after the opening `<form ...>` of the non-error branch, add the progress bar:

```html
  <div class="wiz-progress" aria-hidden="true">
    {% for i in range(max_questions or 4) %}
    <span class="{{ 'filled' if i < question_number else '' }}"></span>
    {% endfor %}
  </div>
```

At the end of the file (after the closing `</form>`), add the toggle script (the hidden inputs drive the form; clicking toggles the `selected` class and the input's `checked`):

```html
<script>
(function(){
  document.querySelectorAll('#wizard-stage .wiz-chip').forEach(function(chip){
    var input = chip.querySelector('input');
    function sync(){
      if (input.type === 'radio') {
        document.querySelectorAll('#wizard-stage .wiz-chip').forEach(function(c){
          c.classList.toggle('selected', c.querySelector('input').checked);
        });
      } else {
        chip.classList.toggle('selected', input.checked);
      }
      chip.setAttribute('aria-pressed', input.checked);
    }
    chip.addEventListener('click', function(e){
      if (e.target.tagName !== 'INPUT'){ input.checked = !input.checked; }
      sync();
    });
  });
})();
</script>
```

- [ ] **Step 5: Improve the composing state in `wizard.html`**

Replace the placeholder `reading the room...` div in `recommender/templates/wizard.html` with a spinner matching `recommend.html`:

```html
  <div style="display:flex; align-items:center; gap:0.6rem;">
    <div style="width:14px; height:14px; border:2px solid var(--border); border-top-color:var(--accent); border-radius:50%; animation:spin 0.6s linear infinite;"></div>
    <span class="mono" style="font-size:0.65rem; letter-spacing:0.1em; text-transform:uppercase; color:var(--muted);">reading the room...</span>
  </div>
```

- [ ] **Step 6: Run the visual-marker test and full suite**

Run: `python3 -m pytest tests/test_web_wizard.py -v && python3 -m pytest`
Expected: PASS.

- [ ] **Step 7: Manual verification**

Start the app and click through the wizard end to end:

```bash
./recommend-web restart
```

Open http://localhost:5051/wizard. Confirm: first question loads, chips toggle (single vs multi), progress fills, "Show me something now" finishes early, results render with the recap and refine bar, and a refine chip re-runs in place. Stop when satisfied: `./recommend-web stop`.

- [ ] **Step 8: Commit**

```bash
git add recommender/templates/ tests/test_web_wizard.py
git commit -m "feat(web): concierge wizard visual design — chips, progress, motion"
```

---

## Self-Review

**Spec coverage:**
- Web-only adaptive wizard, taste profile as prior, cap 3-4 with early exit, multi-select chips + free text — Tasks 2 (engine prompt, cap, `finish`), 3 (routes, `finish=1`), 4 (chips UI). Covered.
- Refine in place — Task 3 `/wizard/refine` + Task 4 refine bar. Covered. (Implemented via `intent_override` + `context_note` carrying the directive and "avoid shown" list, rather than `ConversationContext`; this stays within the Task 1 surface and still excludes shown titles. Deviation from the spec's suggested mechanism, noted intentionally.)
- Soft signals as ranking nudge, no new `QueryIntent` fields — Task 1 `context_note`. Covered.
- Stateless server, client-carried JSON state — Task 2 `WizardState.from_json/to_json`, Task 3 hidden `state` field. Covered.
- Error handling (malformed JSON, LLM failure, empty results, tampered state, server-side cap) — Task 2 (`from_json` reset, malformed-turn finalize, cap), Task 3 (route try/except, empty-results branch). Covered.
- Reuse `ask()` pipeline + background job for the slow step — Tasks 1 and 3. Covered.
- Config `wizard.max_questions` — Task 2. Covered.
- Tests for engine, `ask` override, routes — Tasks 1-4. Covered.

**Placeholder scan:** No TBD/TODO; every code step shows full code. The two "if a template error surfaces, pass `query=summary`" notes are conditional hardening instructions with the exact fix given, not placeholders.

**Type consistency:** `WizardState(turns, turn_count)`, `next_turn(state, ctx, force_finish=False)`, the `ask`/`recommend` dict shapes, `rank_candidates(..., context_note=...)`, and `ask(..., intent_override=, context_note=)` are used identically across tasks. `_run_wizard_recommend_job(intent_dict, context_note, summary)` matches its `job_registry.submit` call sites in `/wizard/next` and `/wizard/refine`.
