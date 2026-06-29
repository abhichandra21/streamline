# Mood Match Wizard Control Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add deterministic pre-LLM intake, Back/Edit controls, and a review-before-results step to Mood Match so users can correct answers and the app owns the most important constraints.

**Architecture:** Keep the current Flask + HTMX flow and the existing recommendation handoff through `_run_wizard_recommend_job(...)`. Add a small server-side flow module that owns deterministic wizard steps and structured preference state, then use the existing LLM wizard only for optional/adaptive nuance after content type, time, and energy are already known. Preserve backward compatibility for existing hidden state payloads, search history replay, and current wizard recommendation jobs.

**Tech Stack:** Python 3.10+, Flask + HTMX, Jinja templates, pytest, existing `QueryIntent`, `wizard.WizardState`, `LLMClient`, job registry, and query history storage. No new third-party dependencies.

**Source PR:** https://github.com/abhichandra21/streamline/pull/58

---

## Why This Exists

The current PR fixed the highest-impact recommendation correctness problems: runtime/content type are no longer only prose nudges, wizard history can replay structured payloads, refinements mutate structured intent, and the flow is advertised as shorter.

Testing exposed a remaining product gap: the wizard is still too LLM-led. The prompt asks the model to resolve content type, time, and selection mode, but those are product-contract decisions that should be deterministic. The current implementation also has no way to go back or edit a mis-tap, even though answer correction was part of the earlier UX critique.

This plan is intentionally scoped to interaction control inside the open PR. It should not rewrite the recommendation algorithm or replace the existing search-history work.

## Current State Anchors

- `recommender/wizard.py`
  - `WizardState` stores only `turns` and `turn_count`.
  - `_prompt(...)` asks the LLM to resolve content type and time.
  - `next_turn(...)` always calls the LLM unless `force_finish` or the question cap triggers finalize.

- `recommender/web.py`
  - `_collect_answers(...)` appends the current prompt answer and cannot replace or remove answers.
  - `wizard_next(...)` appends form state, calls `wizard.next_turn(...)`, and immediately starts a job when the model returns `recommend`.
  - `_run_wizard_recommend_job(...)` and `/wizard/replay` should be preserved.

- `recommender/templates/_wizard_step.html`
  - Renders single-select and multi-select inputs.
  - Shows prior answers as read-only text.
  - Has no Back button, no Edit affordance, and no review step.

- `tests/test_web_wizard.py` and `tests/test_wizard.py`
  - Already cover the existing ask/recommend branches, single vs multi rendering, progress display, structured history, replay, and posted payload validation.
  - New tests should extend these rather than replacing them.

## Product Contract

Mood Match should become a guided decision flow with deterministic first-class controls:

- The first screen after Start must be app-rendered and must not call the LLM.
- Content type must be asked before time because time choices should differ for movies vs TV.
- Time and content type must map to structured fields before the LLM sees anything.
- Low-level answer correction must be available before results:
  - Back returns to the previous step without appending a blank or duplicate answer.
  - Edit from the answer summary returns to a selected previous step.
  - Editing an earlier deterministic answer clears dependent later answers.
- The user should see a compact "what I heard" review before recommendations are launched.
- The LLM may ask at most one adaptive follow-up by default after deterministic intake.
- The user must be able to skip the adaptive LLM follow-up and go directly to results from review.
- Existing wizard replay, refinement, and search history behavior must keep working.

## Non-Goals

- Do not add a client-side SPA or a front-end framework.
- Do not add database-backed sessions.
- Do not remove the existing LLM wizard engine; reduce its responsibility.
- Do not broaden this PR into semantic retrieval, genre ontology work, or TMDB keyword expansion.
- Do not change classic search behavior.
- Do not require migration of old wizard history entries.

## Recommended Flow

Use deterministic steps before the LLM:

1. `content_type`
   - Single-select.
   - Chips: `Movie`, `Series`, `Either`.
   - Stored value maps to `QueryIntent.content_type`: `movie`, `tv`, `both`.

2. `time_window`
   - Single-select.
   - Options depend on content type.
   - Movie choices should avoid impossible "under an hour" movie-only requests.
   - Series choices should use episode language.
   - Either choices can steer very short requests toward TV or short specials rather than movie-only.

3. `energy`
   - Single-select.
   - Chips: `Easy`, `Medium`, `Locked in`.
   - Stored as a soft ranker signal, not a hard filter.

4. `tone`
   - Multi-select, optional.
   - Chips should stay short and broad enough to be useful: `Funny`, `Tense`, `Warm`, `Weird`, `Thoughtful`, `Comfort`.
   - Free text remains allowed.

5. `review`
   - Shows answer chips and a short generated or deterministic summary.
   - Primary action: `Show picks`.
   - Secondary action: `Ask one more`.
   - Each deterministic answer row has an Edit button.

6. `adaptive`
   - Optional.
   - One LLM-generated question, seeded with the structured answers.
   - The adaptive question must not re-ask content type, time, or energy.
   - After one adaptive answer, return to review or finish.

This keeps the default path around 4 deterministic interactions plus review, with one optional LLM refinement when the user wants it.

## State Contract

Extend `wizard.WizardState` instead of replacing it. Existing hidden JSON that contains only `turns` and `turn_count` must still parse.

Recommended shape:

```python
@dataclass
class WizardState:
    turns: list[dict] = field(default_factory=list)
    turn_count: int = 0
    step: str = "content_type"
    answers: dict = field(default_factory=dict)
    adaptive_turns: list[dict] = field(default_factory=list)
    review_seen: bool = False
```

Keep `turns` for old LLM-style Q&A compatibility and history context. Use `answers` for deterministic fields keyed by step id:

```python
{
    "content_type": "movie",
    "time_window": "short_movie",
    "energy": "easy",
    "tone": ["funny", "warm"],
    "free_text": "nothing bleak"
}
```

Rules:

- `from_json(...)` defaults missing new fields without rejecting old payloads.
- `to_json(...)` includes new fields.
- State size and turn caps remain enforced.
- Editing an answer removes that answer and any later dependent values.
- Back/edit/navigation actions never append the current form as an answered prompt.

## File Map

- Create: `recommender/wizard_flow.py`
  - Deterministic step definitions.
  - State mutation helpers for answer, back, edit, and review.
  - Mapping from structured answers to `QueryIntent` seed fields and `context_note`.
  - Rendering data for deterministic steps and review.

- Modify: `recommender/wizard.py`
  - Extend `WizardState`.
  - Add prompt support for structured preferences before adaptive/finalize LLM calls.
  - Keep malformed JSON and cap behavior defensive.

- Modify: `recommender/web.py`
  - Route `/wizard/next` through deterministic flow before `wizard.next_turn(...)`.
  - Add form action handling for Back, Edit, Ask one more, and Show picks.
  - Keep `_run_wizard_recommend_job(...)`, `/wizard/replay`, and `/wizard/refine` contracts stable.

- Modify: `recommender/templates/_wizard_step.html`
  - Render Back when there is a previous deterministic step.
  - Render Edit controls in the answer summary.
  - Preserve current single-select and multi-select chip behavior.
  - Avoid visible text that explains implementation mechanics.

- Create: `recommender/templates/_wizard_review.html`
  - Render "what I heard" summary.
  - Render editable answer chips/rows.
  - Render `Show picks`, `Ask one more`, and Start over.

- Modify: `recommender/templates/wizard.html`
  - Start should load the deterministic `content_type` step rather than causing an immediate LLM call.
  - Copy can stay close to current wording, but remove any implication that every question is LLM-generated.

- Tests:
  - Create: `tests/test_wizard_flow.py`
  - Modify: `tests/test_wizard.py`
  - Modify: `tests/test_web_wizard.py`

## Task 1: Add Structured State Compatibility Tests

**Purpose:** Lock in backward-compatible state serialization before adding new flow behavior.

**Files:**
- Modify: `recommender/wizard.py`
- Test: `tests/test_wizard.py`

- [ ] **Step 1: Add tests for legacy and new state shapes**

Add tests with these names:

```python
def test_wizard_state_loads_legacy_payload_without_new_fields():
    raw = json.dumps({"turns": [], "turn_count": 0})
    state = wizard.WizardState.from_json(raw)
    assert state.step == "content_type"
    assert state.answers == {}
    assert state.adaptive_turns == []


def test_wizard_state_round_trips_structured_answers():
    state = wizard.WizardState(
        turns=[],
        turn_count=0,
        step="time_window",
        answers={"content_type": "movie"},
        adaptive_turns=[],
    )
    loaded = wizard.WizardState.from_json(state.to_json())
    assert loaded.step == "time_window"
    assert loaded.answers == {"content_type": "movie"}
```

- [ ] **Step 2: Run focused tests and confirm failure**

Run:

```bash
source .venv/bin/activate
python3 -m pytest tests/test_wizard.py::test_wizard_state_loads_legacy_payload_without_new_fields tests/test_wizard.py::test_wizard_state_round_trips_structured_answers -v
```

Expected result before implementation: fails because `WizardState` does not have the new fields.

- [ ] **Step 3: Extend `WizardState`**

Add fields for `step`, `answers`, `adaptive_turns`, and `review_seen`. Keep the current defensive parsing behavior:

- Non-dict `answers` becomes `{}`.
- Non-list `adaptive_turns` becomes `[]`.
- Missing `step` defaults to `"content_type"`.
- Unknown `step` resets to `"content_type"` unless it is one of the deterministic/adaptive/review step ids.

- [ ] **Step 4: Run the focused tests**

Run the same focused pytest command. Expected result: both tests pass.

## Task 2: Create Deterministic Wizard Flow Module

**Purpose:** Move product-owned questions out of the LLM prompt path.

**Files:**
- Create: `recommender/wizard_flow.py`
- Test: `tests/test_wizard_flow.py`

- [ ] **Step 1: Add tests for first step and step progression**

Create `tests/test_wizard_flow.py` with focused tests:

```python
from recommender.wizard import WizardState
from recommender import wizard_flow


def test_first_step_is_content_type():
    turn = wizard_flow.current_turn(WizardState())
    assert turn["action"] == "ask"
    assert turn["step"] == "content_type"
    assert turn["multi"] is False
    assert [c["value"] for c in turn["chips"]] == ["movie", "tv", "both"]


def test_answering_content_type_advances_to_time_window():
    state = WizardState()
    state = wizard_flow.apply_answer(state, "content_type", ["movie"], "")
    assert state.answers["content_type"] == "movie"
    assert state.step == "time_window"
```

- [ ] **Step 2: Add tests for time choices by content type**

Use separate tests so failures identify the broken mapping:

```python
def test_movie_time_choices_do_not_offer_under_an_hour():
    state = WizardState(step="time_window", answers={"content_type": "movie"})
    values = [c["value"] for c in wizard_flow.current_turn(state)["chips"]]
    assert "under_hour" not in values
    assert "short_movie" in values


def test_tv_time_choices_use_episode_language():
    state = WizardState(step="time_window", answers={"content_type": "tv"})
    labels = [c["label"].lower() for c in wizard_flow.current_turn(state)["chips"]]
    assert any("episode" in label for label in labels)
```

- [ ] **Step 3: Add the minimal module**

Implement these public functions in `recommender/wizard_flow.py`:

```python
def current_turn(state: WizardState) -> dict:
    ...


def apply_answer(state: WizardState, step: str, selected: list[str], free_text: str) -> WizardState:
    ...


def previous_step(state: WizardState) -> WizardState:
    ...


def edit_step(state: WizardState, step: str) -> WizardState:
    ...


def review_model(state: WizardState) -> dict:
    ...


def build_recommendation_seed(state: WizardState) -> tuple[dict, str, str]:
    ...
```

The implementation should return plain dictionaries shaped like the existing LLM `ask` turn so `_wizard_step.html` can keep rendering both deterministic and adaptive questions.

- [ ] **Step 4: Encode deterministic mappings**

Use explicit step ids and answer ids:

- `content_type`
  - `movie` -> `content_type: "movie"`
  - `tv` -> `content_type: "tv"`
  - `both` -> `content_type: "both"`

- `time_window`
  - For movies:
    - `short_movie` -> `max_runtime_minutes: 95`
    - `standard_movie` -> `max_runtime_minutes: 125`
    - `no_limit` -> `max_runtime_minutes: None`
  - For TV:
    - `short_episode` -> `content_type: "tv"`, `max_runtime_minutes: 35`
    - `one_episode` -> `content_type: "tv"`, `max_runtime_minutes: 60`
    - `binge` -> `content_type: "tv"`, `max_runtime_minutes: None`, context note says the user is open to multiple episodes
  - For either:
    - `under_hour` -> `content_type: "tv"`, `max_runtime_minutes: 60`
    - `around_90` -> `content_type: "both"`, `max_runtime_minutes: 95`
    - `two_hours_plus` -> `content_type: "both"`, `max_runtime_minutes: None`

- `energy`
  - `easy`, `medium`, `locked_in` append soft context notes.

- `tone`
  - Multi-select values map to `mood_descriptors` and context note.

- [ ] **Step 5: Run flow tests**

Run:

```bash
source .venv/bin/activate
python3 -m pytest tests/test_wizard_flow.py -q
```

Expected result: all new flow tests pass.

## Task 3: Wire Deterministic Steps Into `/wizard/next`

**Purpose:** Starting Mood Match should not spend an LLM call just to ask content type.

**Files:**
- Modify: `recommender/web.py`
- Modify: `recommender/templates/wizard.html`
- Test: `tests/test_web_wizard.py`

- [ ] **Step 1: Add a web test proving Start does not call the LLM**

Add a test:

```python
def test_wizard_start_renders_deterministic_content_type_without_llm(client):
    with patch.object(web.wizard, "next_turn") as next_turn:
        resp = client.post("/wizard/next", data=_csrf_form(), headers={"HX-Request": "true"})
    assert resp.status_code == 200
    assert b"Movie" in resp.data
    assert b"Series" in resp.data
    next_turn.assert_not_called()
```

- [ ] **Step 2: Add a web test for deterministic progression**

Post `step=content_type`, `selected=movie`, and assert the next HTML contains movie time choices and still does not call `wizard.next_turn(...)`.

- [ ] **Step 3: Update `wizard_next(...)`**

Route order should be:

1. Parse state.
2. If form action is Back, call `wizard_flow.previous_step(...)` and render current deterministic/adaptive step.
3. If form action is Edit, call `wizard_flow.edit_step(...)` and render that step.
4. If a deterministic `step` was posted, apply it with `wizard_flow.apply_answer(...)`.
5. If state points at another deterministic step, render it.
6. If state points at review and the user did not choose `ask_more` or `finish`, render `_wizard_review.html`.
7. If the user chooses `ask_more`, call the LLM adaptive path once.
8. If the user chooses `finish`, build the recommendation seed and submit `_run_wizard_recommend_job(...)`.

Keep this branching small in `web.py`. Put state mutation and seed-building in `wizard_flow.py`.

- [ ] **Step 4: Preserve existing LLM branch tests**

The existing ask/recommend branch tests can either:

- Set `state.step` to `"adaptive"` before posting, or
- Patch the flow helper so the route enters the adaptive branch.

Do not delete those tests; the LLM branch still exists.

- [ ] **Step 5: Run web wizard tests**

Run:

```bash
source .venv/bin/activate
python3 -m pytest tests/test_web_wizard.py -q
```

Expected result: web wizard tests pass.

## Task 4: Add Back and Edit Behavior

**Purpose:** A mis-tap should not force the user to start over.

**Files:**
- Modify: `recommender/wizard_flow.py`
- Modify: `recommender/web.py`
- Modify: `recommender/templates/_wizard_step.html`
- Modify: `recommender/templates/_wizard_review.html`
- Test: `tests/test_wizard_flow.py`
- Test: `tests/test_web_wizard.py`

- [ ] **Step 1: Add state-level tests for Back**

Add tests:

```python
def test_back_moves_to_previous_step_without_appending_answer():
    state = WizardState(
        step="energy",
        answers={"content_type": "movie", "time_window": "short_movie"},
    )
    updated = wizard_flow.previous_step(state)
    assert updated.step == "time_window"
    assert updated.answers["content_type"] == "movie"
    assert updated.answers["time_window"] == "short_movie"
```

- [ ] **Step 2: Add state-level tests for Edit**

Add tests:

```python
def test_editing_content_type_clears_dependent_answers():
    state = WizardState(
        step="review",
        answers={
            "content_type": "movie",
            "time_window": "short_movie",
            "energy": "easy",
            "tone": ["funny"],
        },
    )
    updated = wizard_flow.edit_step(state, "content_type")
    assert updated.step == "content_type"
    assert updated.answers == {}
```

Also add a test for editing `energy` that preserves `content_type` and `time_window` but clears `energy` and later fields.

- [ ] **Step 3: Add route-level tests for Back/Edit not appending prompt answers**

Use posted data with a current prompt and Back action. Assert the returned state hidden input does not include a skipped or duplicate answer for the prompt. The existing `_HiddenInputParser` in `tests/test_web_wizard.py` can parse hidden inputs.

- [ ] **Step 4: Update templates**

In `_wizard_step.html`:

- Add a hidden `step` input for deterministic/adaptive steps.
- Add a Back button when `can_go_back` is true.
- Keep the `finish` button, but route it through structured seed-building instead of appending a skipped answer.

In `_wizard_review.html`:

- Each answer row includes an Edit button:
  - `name="edit_step"`
  - `value="<step id>"`
- Edit buttons post the current state and target `#wizard-stage`.

- [ ] **Step 5: Run focused tests**

Run:

```bash
source .venv/bin/activate
python3 -m pytest tests/test_wizard_flow.py tests/test_web_wizard.py -q
```

Expected result: flow and web wizard tests pass.

## Task 5: Add Review Before Results

**Purpose:** Let the user confirm the recommendation contract before spending the recommendation job.

**Files:**
- Create: `recommender/templates/_wizard_review.html`
- Modify: `recommender/web.py`
- Modify: `recommender/wizard_flow.py`
- Test: `tests/test_web_wizard.py`
- Test: `tests/test_wizard_flow.py`

- [ ] **Step 1: Add review model tests**

Add tests:

```python
def test_review_model_summarizes_structured_answers():
    state = WizardState(
        step="review",
        answers={
            "content_type": "movie",
            "time_window": "short_movie",
            "energy": "easy",
            "tone": ["funny", "warm"],
        },
    )
    model = wizard_flow.review_model(state)
    assert model["summary"] == "a short, easy movie with a funny, warm tone"
    assert [row["step"] for row in model["answers"]] == [
        "content_type",
        "time_window",
        "energy",
        "tone",
    ]
```

The exact summary string can differ if the implementation chooses a better phrase, but the test should lock the selected wording once the implementation chooses it.

- [ ] **Step 2: Add web test for review rendering**

Post deterministic answers through the route until `review`. Assert:

- The response includes the summary.
- The response includes `Show picks`.
- The response includes edit controls.
- `job_registry.submit` is not called until `finish=1`.

- [ ] **Step 3: Implement `_wizard_review.html`**

Keep it compact:

- One short heading.
- Answer rows/chips with Edit controls.
- Primary `Show picks` button.
- Secondary `Ask one more` button.
- Start over link.

Do not create a large explanatory card. The review is a control surface, not a landing page.

- [ ] **Step 4: Run review tests**

Run:

```bash
source .venv/bin/activate
python3 -m pytest tests/test_wizard_flow.py::test_review_model_summarizes_structured_answers tests/test_web_wizard.py -q
```

Expected result: tests pass.

## Task 6: Seed the LLM With Structured Preferences

**Purpose:** The LLM should refine known preferences, not rediscover them.

**Files:**
- Modify: `recommender/wizard.py`
- Modify: `recommender/web.py`
- Test: `tests/test_wizard.py`
- Test: `tests/test_web_wizard.py`

- [ ] **Step 1: Add prompt tests**

Add tests:

```python
def test_adaptive_prompt_includes_structured_preferences():
    state = WizardState(
        step="adaptive",
        answers={
            "content_type": "movie",
            "time_window": "short_movie",
            "energy": "easy",
        },
    )
    prompt = wizard._prompt(state, "profile", force_finish=False)
    low = prompt.lower()
    assert "structured preferences" in low
    assert "content_type: movie" in low
    assert "max_runtime_minutes: 95" in low
    assert "do not ask content type" in low
```

- [ ] **Step 2: Update prompt construction**

Add a helper in `wizard.py` that converts `state.answers` into a concise structured block. The adaptive prompt should say:

- Content type, time, and energy are already known.
- Do not ask those again.
- Ask at most one question that would materially improve mood/novelty/theme fit.
- If enough signal exists, return `recommend`.

- [ ] **Step 3: Ensure final intent preserves deterministic constraints**

When finalizing, deterministic `content_type` and `max_runtime_minutes` must override any LLM output that conflicts with them. The LLM may add mood descriptors, genres, similar titles, and context note.

Recommended helper boundary:

```python
def merge_intent_with_seed(llm_intent: QueryIntent, seed: dict) -> QueryIntent:
    ...
```

This can live in `wizard_flow.py` or `wizard.py`. Pick the location that keeps `web.py` simple.

- [ ] **Step 4: Run wizard tests**

Run:

```bash
source .venv/bin/activate
python3 -m pytest tests/test_wizard.py tests/test_wizard_flow.py -q
```

Expected result: tests pass.

## Task 7: Build Recommendation Seed From Structured Answers

**Purpose:** Finishing from review should produce coherent recommendations even if the adaptive LLM question is skipped.

**Files:**
- Modify: `recommender/wizard_flow.py`
- Modify: `recommender/web.py`
- Test: `tests/test_wizard_flow.py`
- Test: `tests/test_web_wizard.py`

- [ ] **Step 1: Add seed-building tests**

Add tests:

```python
def test_build_seed_maps_movie_short_time_to_intent_fields():
    state = WizardState(
        step="review",
        answers={
            "content_type": "movie",
            "time_window": "short_movie",
            "energy": "easy",
            "tone": ["funny"],
        },
    )
    intent_dict, context_note, summary = wizard_flow.build_recommendation_seed(state)
    assert intent_dict["content_type"] == "movie"
    assert intent_dict["max_runtime_minutes"] == 95
    assert intent_dict["mood_descriptors"] == ["funny"]
    assert "easy" in context_note.lower()
    assert "short" in summary.lower()
```

Add a separate test for `both` + `under_hour`:

```python
def test_build_seed_steers_either_under_hour_to_tv():
    state = WizardState(
        step="review",
        answers={"content_type": "both", "time_window": "under_hour", "energy": "easy"},
    )
    intent_dict, context_note, summary = wizard_flow.build_recommendation_seed(state)
    assert intent_dict["content_type"] == "tv"
    assert intent_dict["max_runtime_minutes"] == 60
```

- [ ] **Step 2: Use the seed when finishing**

In `wizard_next(...)`, when `finish=1` from review:

- Call `wizard_flow.build_recommendation_seed(state)`.
- Submit `_run_wizard_recommend_job(intent_dict, context_note, summary, label="wizard")`.
- Do not call `wizard.next_turn(...)`.

- [ ] **Step 3: Keep adaptive finish deterministic**

If the user chose `Ask one more`, merge adaptive LLM output with the deterministic seed before submitting. Deterministic fields win.

- [ ] **Step 4: Run web tests**

Run:

```bash
source .venv/bin/activate
python3 -m pytest tests/test_web_wizard.py tests/test_wizard_flow.py -q
```

Expected result: tests pass.

## Task 8: Manual UX Pass

**Purpose:** Verify the control flow feels correct in a browser, not only in tests.

**Files:**
- No required code file changes unless manual testing surfaces a bug.

- [ ] **Step 1: Start the app**

Run:

```bash
source .venv/bin/activate
./recommend-web start
```

Open `http://localhost:5051/wizard`.

- [ ] **Step 2: Check default path**

Verify:

- Start opens content type quickly without an LLM spinner.
- Content type selection advances to tailored time options.
- Back returns to the prior answer with the current value visible.
- Editing content type from review clears incompatible downstream answers.
- Review appears before results.
- Show picks launches polling and renders results.
- Search history stores a compact Mood Match label and replay still works.

- [ ] **Step 3: Check optional adaptive path**

Verify:

- Ask one more triggers one adaptive LLM question.
- The adaptive question does not ask movie vs series, time, or energy again.
- Finishing after the adaptive answer preserves structured content type and runtime.

- [ ] **Step 4: Stop the app**

Run:

```bash
./recommend-web stop
```

## Verification Commands

Run focused tests first:

```bash
source .venv/bin/activate
python3 -m pytest tests/test_wizard_flow.py -q
python3 -m pytest tests/test_wizard.py -q
python3 -m pytest tests/test_web_wizard.py -q
```

Then run the full suite:

```bash
python3 -m pytest -q
```

Also run:

```bash
git diff --check
```

## Acceptance Criteria

- Starting Mood Match renders the deterministic content-type question without calling the LLM.
- The deterministic flow captures content type, time, energy, and tone in structured state.
- Time choices are tailored by content type and avoid movie-only under-hour traps.
- Back works without appending blank/skipped answers.
- Edit works from the review step and clears dependent answers when an earlier answer changes.
- Review appears before results and clearly shows what will be used.
- Users can skip the adaptive LLM question.
- If the adaptive LLM is used, it asks at most one new question by default.
- Final intent preserves deterministic `content_type` and `max_runtime_minutes`.
- Wizard replay and refinement still work for entries created before and after this change.
- Existing tests plus the new focused tests pass.

## Implementation Notes

- Keep deterministic step definitions data-driven, but do not overbuild a generic survey engine.
- Prefer adding `wizard_flow.py` over making `web.py` absorb all flow logic.
- Use existing hidden form state rather than adding server sessions.
- Hidden state is acceptable for this single-user app, but keep the existing size guard.
- Treat `finish=1`, Back, and Edit as navigation actions first; they should not blindly append the current prompt to `turns`.
- Keep templates server-rendered and HTMX-driven.
- Do not introduce new dependencies for form parsing, state machines, or front-end behavior.

## What This Leaves For A Later PR

- Richer mood-to-genre or mood-to-keyword candidate generation.
- Visible explanation when runtime constraints were relaxed because no strict matches existed.
- Search history grouping for refinement chains.
- Full persistence of in-progress wizard state across page refreshes.
- Metrics for drop-off by step and edit/back usage.
