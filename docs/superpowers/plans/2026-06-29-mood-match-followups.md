# Mood Match Follow-up Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix the Mood Match wizard so its answers produce constrained, coherent, faithfully replayable recommendations instead of broad prose-nudged searches.

**Architecture:** Keep the existing Flask + HTMX wizard and the `ask(intent_override=..., context_note=...)` handoff. Tighten the contract between wizard answers and `QueryIntent`: content type, runtime, and platform are hard constraints; mood, company, novelty, and energy are ranking signals. Extend query history with backward-compatible structured metadata so wizard runs can be displayed compactly and replayed faithfully.

**Tech Stack:** Python 3.10+, Flask + HTMX, Jinja templates, pytest, existing `LLMClient`, TMDB metadata/cache APIs. No new third-party dependencies.

**Source Issue:** https://github.com/abhichandra21/streamline/issues/57

---

## Global Constraints

- Keep the existing single-user, server-rendered app architecture.
- Preserve old `query_history.json` entries. New fields must be optional and templates must fall back to `entry.query`.
- Do not introduce a database migration for search history unless absolutely necessary. The JSON history file is acceptable for this feature.
- Do not commit secrets, provider exports, local config, cache churn, or generated logs.
- Do not use emoji in code, docs, templates, or tests.
- Prefer focused helpers over large rewrites of `web.py` or `query_engine.py`.
- Use the project venv: `source .venv/bin/activate`.
- Run focused tests first, then `python3 -m pytest -q`.

## Product Contract

Implement this distinction consistently:

- **Hard filters:** `content_type`, `max_runtime_minutes`, explicit platform constraints, watched/dismissed exclusions, and configured minimum rating/year.
- **Soft ranker signals:** mood descriptors, energy, company, novelty, and free-text nuance.
- **Structured replay payload:** wizard history entries must store the intent and context used to create the results. Do not replay wizard history by reparsing a display string.

## File Map

- `recommender/query_engine.py`
  - Enforce `QueryIntent.max_runtime_minutes` in candidate filtering.
  - Keep `context_note` as a ranker signal, but stop relying on it for runtime/content-type behavior.
  - Optionally improve ranking prompt metadata so runtime/content type/provider data are visible to the ranker.

- `recommender/wizard.py`
  - Update the wizard prompt/schema so it asks or infers content type early.
  - Stop forcing every question to be multi-select.
  - Encourage structured runtime output when time is answered.
  - Validate LLM turn shapes defensively.

- `recommender/web.py`
  - Record wizard history with structured metadata.
  - Add a faithful wizard-history replay path.
  - Make refinement mutate structured intent fields where possible.
  - Harden posted wizard/refine payloads.

- `recommender/history.py`
  - Extend `record()` to accept optional metadata fields without breaking existing callers.
  - Keep serialization backward-compatible for existing search history.

- `recommender/templates/wizard.html`
  - Lower the advertised max/expected flow length.
  - Keep copy aligned with the new 4-5 question target.

- `recommender/templates/_wizard_step.html`
  - Support single-select and multi-select question modes.
  - Fix the progress display so it does not imply all max questions are expected.
  - Consider adding Back or Start over controls.

- `recommender/templates/_wizard_results.html`
  - Keep refinement controls, but ensure their payloads support structured updates.

- `recommender/templates/searches.html`
  - Render compact `label` when present.
  - Make wizard replay use stored payload instead of `/?q=...`.

- `recommender/templates/index.html`
  - Render compact recent-search labels where available.

- Tests:
  - `tests/test_query_engine.py`
  - `tests/test_wizard.py`
  - `tests/test_web_wizard.py`
  - `tests/test_web.py` if search-history display/replay behavior is easier to cover there.

---

## Task 1: Enforce Runtime Constraints in the Query Pipeline

**Purpose:** Make time answers matter. A short-time wizard run should not return long titles unless constraints are explicitly loosened.

**Files:**
- Modify: `recommender/query_engine.py`
- Test: `tests/test_query_engine.py`

- [ ] **Step 1: Add failing tests for runtime filtering**

Add focused tests that create movie candidates with different `runtime_minutes` and an intent with `max_runtime_minutes`.

Cover:

- A movie over the max runtime is excluded.
- A movie under the max runtime is retained.
- A candidate with unknown runtime is handled deliberately. Recommended behavior: keep unknown-runtime candidates only if the candidate pool would otherwise be empty, or rank them below known-good candidates.
- TV runtime is treated as episode runtime, not series runtime.

Suggested test names:

- `test_ask_filters_movies_over_max_runtime()`
- `test_ask_allows_tv_episode_under_max_runtime()`
- `test_ask_handles_unknown_runtime_conservatively()`

Use the existing `make_meta()` helper pattern in `tests/test_query_engine.py`; extend it locally in the test if needed to set `runtime_minutes`.

- [ ] **Step 2: Run the focused failing tests**

Run:

```bash
source .venv/bin/activate
python3 -m pytest tests/test_query_engine.py::test_ask_filters_movies_over_max_runtime -v
```

Expected: FAIL because runtime is not currently applied in `_candidate_allowed()`.

- [ ] **Step 3: Implement runtime filtering**

In `recommender/query_engine.py`, thread `intent.max_runtime_minutes` into candidate eligibility.

Recommended approach:

- Add `max_runtime_minutes: int | None` to `_candidate_allowed(...)`.
- Pass `intent.max_runtime_minutes` from both call sites that append candidates.
- Compare against `candidate.runtime_minutes`.
- Keep the logic small and explicit; do not bury runtime behavior inside ranking.

Decision to encode:

- If `max_runtime_minutes` is set and `candidate.runtime_minutes` is known and greater than the max, exclude it.
- If runtime is unknown, keep it for now unless this causes noisy results in testing. Add a comment that unknown runtime cannot be safely filtered.

- [ ] **Step 4: Run focused and query-engine tests**

Run:

```bash
python3 -m pytest tests/test_query_engine.py::test_ask_filters_movies_over_max_runtime -v
python3 -m pytest tests/test_query_engine.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add recommender/query_engine.py tests/test_query_engine.py
git commit -m "fix(query): enforce runtime constraints"
```

---

## Task 2: Make Content Type an Explicit Wizard Constraint

**Purpose:** Avoid incoherent mixed movie/TV lists unless the user intentionally chooses either.

**Files:**
- Modify: `recommender/wizard.py`
- Modify: `recommender/templates/_wizard_step.html`
- Test: `tests/test_wizard.py`
- Test: `tests/test_web_wizard.py`

- [ ] **Step 1: Add tests for content-type handling**

Add tests that verify the wizard prompt and/or returned turn supports an early content-type question.

Cover:

- First question can be single-select.
- The prompt tells the LLM to resolve movie/series/either early.
- A finalized intent with no explicit content type remains valid, but the prompt should no longer encourage defaulting to `both`.

Suggested test names:

- `test_wizard_prompt_asks_to_resolve_content_type_early()`
- `test_question_can_render_single_select_mode()`

- [ ] **Step 2: Run the focused failing tests**

Run:

```bash
python3 -m pytest tests/test_wizard.py::test_wizard_prompt_asks_to_resolve_content_type_early -v
python3 -m pytest tests/test_web_wizard.py::test_question_can_render_single_select_mode -v
```

Expected: FAIL until the prompt/template support the new mode.

- [ ] **Step 3: Update the wizard prompt contract**

In `recommender/wizard.py`, update `_prompt(...)` so it instructs the model to:

- Resolve commitment/content type early: movie, series, or either.
- Use single-select for mutually exclusive dimensions.
- Use multi-select only for additive dimensions like mood or themes.
- Map content-type answers to `intent.content_type`.
- Infer content type from time only when the answer is strong; otherwise ask.

Do not hardcode a complete deterministic first question unless you decide the LLM is too inconsistent. If deterministic first-turn behavior is chosen, keep it small: return a static content-type question when `state.turn_count == 0`, then resume adaptive LLM turns afterward.

- [ ] **Step 4: Support single-select controls**

In `_wizard_step.html`:

- Render radio inputs when `turn.multi` is false.
- Render checkbox inputs when `turn.multi` is true.
- Adjust helper text so it does not always say "choose more than one."
- Keep selected styling working for both input types.

Keep the client-side script local to the wizard stage. Avoid introducing a new JS framework.

- [ ] **Step 5: Run focused wizard/web tests**

Run:

```bash
python3 -m pytest tests/test_wizard.py tests/test_web_wizard.py -q
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add recommender/wizard.py recommender/templates/_wizard_step.html tests/test_wizard.py tests/test_web_wizard.py
git commit -m "fix(wizard): resolve content type explicitly"
```

---

## Task 3: Map Time Answers to `max_runtime_minutes`

**Purpose:** Ensure the wizard emits structured runtime constraints instead of burying time in prose.

**Files:**
- Modify: `recommender/wizard.py`
- Test: `tests/test_wizard.py`

- [ ] **Step 1: Add tests for runtime intent synthesis**

Use `FakeLLM` in `tests/test_wizard.py` to simulate a final response where the model sets `max_runtime_minutes`.

Cover:

- `_safe_query_intent` preserves integer `max_runtime_minutes`.
- The wizard prompt explicitly tells the LLM to set `max_runtime_minutes` when time is known.

Suggested test names:

- `test_finalize_preserves_max_runtime_minutes()`
- `test_wizard_prompt_maps_time_to_max_runtime()`

- [ ] **Step 2: Run tests and confirm current gap**

Run:

```bash
python3 -m pytest tests/test_wizard.py::test_wizard_prompt_maps_time_to_max_runtime -v
```

Expected: FAIL if the prompt does not yet include the runtime mapping instruction.

- [ ] **Step 3: Update finalize guidance**

In `wizard.py`, update the final intent instructions:

- If the user gives a time window, set `intent.max_runtime_minutes`.
- For "under an hour," use `60`.
- For "around 90 minutes," use `90`.
- For "one episode," keep `content_type` as `tv` and set a reasonable episode max if the answer implies it.
- Keep `context_note` for nuance, not as the only carrier of time.

Avoid building a separate natural-language parser unless tests show the model cannot do this reliably. The first implementation should be prompt-contract plus downstream enforcement from Task 1.

- [ ] **Step 4: Run tests**

Run:

```bash
python3 -m pytest tests/test_wizard.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add recommender/wizard.py tests/test_wizard.py
git commit -m "fix(wizard): emit runtime constraints"
```

---

## Task 4: Store Structured Wizard History and Compact Labels

**Purpose:** Make search history readable and preserve enough data to replay Mood Match faithfully.

**Files:**
- Modify: `recommender/history.py`
- Modify: `recommender/web.py`
- Modify: `recommender/templates/searches.html`
- Modify: `recommender/templates/index.html`
- Test: `tests/test_web_wizard.py` or `tests/test_web.py`

- [ ] **Step 1: Add tests for history metadata**

Add tests around `_run_wizard_recommend_job(...)` or the history rendering path.

Cover:

- Wizard history entries include `source: "wizard"`.
- Wizard entries include a compact `label`.
- Wizard entries include `summary`, `intent_dict`, and `context_note`.
- Old history entries with only `query` still render.

Suggested test names:

- `test_wizard_job_records_structured_history()`
- `test_searches_render_label_when_present()`
- `test_searches_fall_back_to_query_for_old_entries()`

- [ ] **Step 2: Extend `history.record()` carefully**

In `recommender/history.py`, extend `record()` with an optional keyword-only metadata parameter. Keep existing callers unchanged.

Recommended shape:

```python
def record(query, results, provider, usage_summary, *, metadata=None):
    ...
```

When metadata is present, merge safe top-level keys into the entry. Keep `query` for backward compatibility, even for wizard entries.

Allowed metadata keys should be explicit, not arbitrary. Recommended:

- `source`
- `label`
- `summary`
- `intent_dict`
- `context_note`
- `refinement`

- [ ] **Step 3: Record wizard metadata**

In `_run_wizard_recommend_job(...)`:

- Build a compact label from the summary.
- Keep the old `query` value usable but do not rely on it for display/replay.
- Pass metadata to `query_history.record(...)`.

Label guidance:

- Prefix with `Mood Match`.
- Keep under roughly 60 characters.
- Prefer concise phrase labels over full sentences.
- ASCII only; use `-` or `:` rather than special separators.

- [ ] **Step 4: Render labels in templates**

In `searches.html` and `index.html`:

- Display `entry.label` when present.
- Use `entry.query` as fallback.
- Show the full summary as secondary text or `title` if useful.
- Keep old entries visually unchanged.

- [ ] **Step 5: Run focused tests**

Run:

```bash
python3 -m pytest tests/test_web_wizard.py tests/test_web.py -q
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add recommender/history.py recommender/web.py recommender/templates/searches.html recommender/templates/index.html tests/test_web_wizard.py tests/test_web.py
git commit -m "fix(history): store structured mood match entries"
```

---

## Task 5: Add Faithful Wizard History Replay

**Purpose:** Make the Searches page replay the original wizard intent/context instead of reparsing the generated summary.

**Files:**
- Modify: `recommender/web.py`
- Modify: `recommender/templates/searches.html`
- Test: `tests/test_web_wizard.py` or `tests/test_web.py`

- [ ] **Step 1: Add failing replay tests**

Cover:

- A wizard history entry renders a replay action that does not link to `/?q=...`.
- Replay submits the stored `intent_dict`, `context_note`, and `summary`.
- Non-wizard entries still use classic re-search.
- Missing or malformed wizard payload returns a clear error or disables replay.

Suggested test names:

- `test_wizard_history_replay_uses_stored_intent()`
- `test_classic_history_research_still_uses_query()`
- `test_wizard_history_replay_rejects_missing_payload()`

- [ ] **Step 2: Add a replay route**

Add a route in `web.py` for wizard replay. Keep it POST-only because it starts a recommendation job.

Recommended route:

```text
POST /wizard/replay
```

Inputs:

- `intent`
- `context_note`
- `summary`

Behavior:

- Validate CSRF through the existing before-request hook.
- Parse intent safely.
- Start `_run_wizard_recommend_job(...)`.
- Return the existing polling partial targeting either `#wizard-stage` or a searches-page result target.

If rendering replay results inside the Searches page is too invasive, the minimal version can redirect to `/wizard` with a job/polling stage. Do not use `/?q=` for wizard replay.

- [ ] **Step 3: Update Searches actions**

In `searches.html`:

- For `entry.source == "wizard"` and valid payload, render a form/button that posts to `/wizard/replay`.
- For old/classic entries, keep the existing `/?q=...` behavior.
- If a wizard entry lacks payload, render a disabled or relabeled action such as `Open` rather than `Re-search`.

- [ ] **Step 4: Run replay tests**

Run:

```bash
python3 -m pytest tests/test_web_wizard.py::test_wizard_history_replay_uses_stored_intent -v
python3 -m pytest tests/test_web.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add recommender/web.py recommender/templates/searches.html tests/test_web_wizard.py tests/test_web.py
git commit -m "fix(web): replay mood match history from stored intent"
```

---

## Task 6: Make Refinements Structural

**Purpose:** Ensure refinement buttons behave like commands, not weak prose suggestions.

**Files:**
- Modify: `recommender/web.py`
- Modify: `recommender/templates/_wizard_results.html`
- Test: `tests/test_web_wizard.py`

- [ ] **Step 1: Add refinement behavior tests**

Cover:

- `shorter` lowers or sets `max_runtime_minutes`.
- `more obscure` adjusts a structured field or context that the query engine can honor.
- Already shown titles are excluded using structured identifiers where available.
- Malformed refine intent is rejected gracefully.

Suggested test names:

- `test_wizard_refine_shorter_updates_runtime()`
- `test_wizard_refine_rejects_bad_intent_json()`
- `test_wizard_refine_excludes_shown_titles_as_json()`

- [ ] **Step 2: Serialize shown titles safely**

In `_wizard_results.html` and `web.py`, replace comma-joined `shown` with JSON.

Preferred shown item shape:

```json
{"title": "Example", "content_type": "movie", "tmdb_id": 123}
```

Fallback to title-only only when no TMDB ID is available.

- [ ] **Step 3: Implement a small refinement helper**

In `web.py`, add a focused helper that takes `intent_dict`, `context_note`, and `directive`, then returns updated versions.

Recommended behavior:

- `shorter`: if no max runtime, set one based on content type; otherwise reduce it by a modest amount with a sensible floor.
- `lighter`: add or emphasize light mood descriptors; keep as a ranker signal if no structured mapping exists.
- `more obscure`: add context for the ranker now, and consider a later query-engine field if needed.
- `surprise me`: keep constraints but add novelty/variety context and exclude shown titles.

Keep the helper deterministic and easy to test.

- [ ] **Step 4: Validate refine payloads**

Use the same safe intent parsing path as other wizard outputs. Do not call `QueryIntent(**intent_dict)` directly on untrusted form data.

- [ ] **Step 5: Run focused tests**

Run:

```bash
python3 -m pytest tests/test_web_wizard.py -q
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add recommender/web.py recommender/templates/_wizard_results.html tests/test_web_wizard.py
git commit -m "fix(wizard): apply refinements structurally"
```

---

## Task 7: Lower Question Cap and Fix Progress Display

**Purpose:** Align the flow with the low-decision-energy user moment.

**Files:**
- Modify: `config.yaml`
- Modify: `config.py` if defaults need changing
- Modify: `recommender/templates/wizard.html`
- Modify: `recommender/templates/_wizard_step.html`
- Test: `tests/test_web_wizard.py`

- [ ] **Step 1: Add tests for displayed flow length**

Cover:

- Wizard landing copy does not advertise 10 questions.
- Step UI does not render 10 fixed progress dots when early finish is possible.

Suggested test names:

- `test_wizard_shell_advertises_short_flow()`
- `test_wizard_progress_does_not_use_max_as_expected_count()`

- [ ] **Step 2: Lower config**

Set:

```yaml
wizard:
  max_questions: 5
```

If `config.py` still defaults to 4, decide whether the repo default and config file should match. Recommended: default to 5 only if product copy says "up to 5"; otherwise keep `config.py` at 4 and set config explicitly.

- [ ] **Step 3: Update copy and progress**

In `wizard.html`, change the landing copy toward:

- "A few quick questions."
- "Usually 3-4 questions."
- "You can stop and see picks at any point."

In `_wizard_step.html`, prefer one of:

- `Question {{ question_number }}` with no denominator.
- A small "up to {{ max_questions }}" note outside the progress visual.
- Dots based on expected count, not hard cap.

- [ ] **Step 4: Run web wizard tests**

Run:

```bash
python3 -m pytest tests/test_web_wizard.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add config.yaml config.py recommender/templates/wizard.html recommender/templates/_wizard_step.html tests/test_web_wizard.py
git commit -m "fix(wizard): shorten flow and clarify progress"
```

---

## Task 8: Harden Wizard State and Intent Validation

**Purpose:** Prevent malformed hidden-form state or refine payloads from crashing jobs or producing invalid recommendation parameters.

**Files:**
- Modify: `recommender/wizard.py`
- Modify: `recommender/web.py`
- Test: `tests/test_wizard.py`
- Test: `tests/test_web_wizard.py`

- [ ] **Step 1: Add validation tests**

Cover:

- Invalid `content_type` falls back to `both` or is rejected gracefully.
- Oversized wizard state resets or errors cleanly.
- Non-list chip payloads do not crash `next_turn()`.
- Bad refine JSON returns a 400 or inline error, not a background job crash.

Suggested test names:

- `test_wizard_ignores_malformed_chips()`
- `test_wizard_state_rejects_oversized_payload()`
- `test_refine_bad_intent_returns_error()`

- [ ] **Step 2: Add small validation helpers**

Recommended helpers:

- In `wizard.py`: normalize LLM ask-turn output so `chips` is always a list of dicts with string `label` and `value`.
- In `web.py`: parse posted intent through `_safe_query_intent` and `dataclasses.asdict(...)`.
- In `WizardState.from_json(...)`: consider a size/turn cap before parsing or after loading.

Keep validation boring and explicit. This is not a security boundary for a public app, but it should avoid avoidable crashes.

- [ ] **Step 3: Run focused tests**

Run:

```bash
python3 -m pytest tests/test_wizard.py tests/test_web_wizard.py -q
```

Expected: PASS.

- [ ] **Step 4: Commit**

```bash
git add recommender/wizard.py recommender/web.py tests/test_wizard.py tests/test_web_wizard.py
git commit -m "fix(wizard): harden posted state validation"
```

---

## Task 9: Optional Back/Edit Controls

**Purpose:** Let users recover from a mis-tap without restarting the whole wizard.

**Files:**
- Modify: `recommender/web.py`
- Modify: `recommender/templates/_wizard_step.html`
- Test: `tests/test_web_wizard.py`

This task is optional if Tasks 1-8 already feel like a large release. Do it if the UX still feels brittle after shorter flow and single-select controls.

- [ ] **Step 1: Pick the smallest viable recovery path**

Recommended first version:

- Add a Start over link during the flow.
- Add Back only if the state model can pop the last answered turn cleanly.

Do not build arbitrary answer editing in the first pass.

- [ ] **Step 2: Add tests**

Cover:

- Start over link is visible during a question.
- Back pops one answered turn, if implemented.
- Back at the first question does not crash.

- [ ] **Step 3: Implement minimal controls**

If implementing Back:

- Add a `back=1` submit action.
- In `_collect_answers`, do not append the current prompt when going back.
- Pop the previous turn and request the prior step or restart from a clean state if reconstruction is too complex.

If this becomes more than a small patch, stop and split into a separate issue.

- [ ] **Step 4: Run tests and commit**

Run:

```bash
python3 -m pytest tests/test_web_wizard.py -q
```

Commit:

```bash
git add recommender/web.py recommender/templates/_wizard_step.html tests/test_web_wizard.py
git commit -m "fix(wizard): add flow recovery controls"
```

---

## Final Verification

After all selected tasks are complete:

- [ ] Run focused suites:

```bash
source .venv/bin/activate
python3 -m pytest tests/test_wizard.py tests/test_web_wizard.py tests/test_query_engine.py tests/test_web.py -q
```

- [ ] Run the full suite:

```bash
python3 -m pytest -q
```

- [ ] Run a manual web smoke test:

```bash
./recommend-web start
```

Open `http://localhost:5051/wizard` and verify:

- The flow resolves movie/series/either early.
- Short-time answers produce short recommendations.
- Pivotal questions are single-select.
- The wizard usually finishes in 3-4 questions.
- Results include coherent content type scope.
- Refinement `shorter` actually produces shorter candidates.
- Search history shows compact Mood Match labels.
- Wizard replay uses stored intent/context, not the recap sentence.

Stop the server:

```bash
./recommend-web stop
```

## Completion Criteria

The implementation is done when:

- Runtime limits are enforced by candidate filtering.
- Content type is asked or inferred before recommendation.
- Wizard search history is compact and structured.
- Wizard history replay is faithful.
- The default flow targets 3-4 questions and caps around 4-5.
- Single-select and multi-select modes are both supported.
- Refinement commands update structured constraints where possible.
- Malformed wizard/refine payloads fail gracefully.
- Full pytest suite passes.

## Notes for the Implementing Agent

- Do not rewrite the whole wizard. The current architecture is good; the issue is the contract between answers, intent, and replay.
- Keep each task independently shippable. If Task 4 history metadata is done before Task 5 replay, old behavior should still work.
- If a task exposes a deeper design problem, stop and update this plan or the GitHub issue before building a larger abstraction.
- Prefer deterministic helpers for constraints and replay over additional LLM calls.
