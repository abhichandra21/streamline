# Concierge Wizard — Guided Recommendation Mode

**Date:** 2026-06-27
**Status:** Design approved, pending spec review

## Problem

Streamline today answers "recommend something *like X*" queries well. But there is no path for the
moment when the user cannot think of anything to watch and wants a *general* recommendation. They do
not have a seed title or a query in mind — they want the system to ask a few probing questions and
suggest based on the answers.

This adds a **concierge wizard**: a guided, adaptive Q&A in the web UI that gathers today's viewing
context, synthesizes a structured intent, and hands off to the existing recommendation pipeline.

## Decisions (captured during brainstorming)

- **Surface:** Web UI only. Matches the "browsing on the couch" moment.
- **Question style:** Adaptive LLM probing — the next question depends on previous answers.
- **Taste profile role:** Used as a **prior**. The LLM sees the profile up front and probes only the
  gaps (today's mood/energy/time/company/novelty), skipping what the profile already implies.
- **Stopping rule:** Hard cap of 3-4 questions (config-driven), with a "show me something now" early
  exit after any answer.
- **Answer input:** LLM-generated **multi-select choice chips** per question, plus an optional
  free-text box.
- **Post-results:** **Refine in place** — re-rank/regenerate without restarting the wizard, reusing
  the existing `ConversationContext` refinement.
- **Soft signals (runtime/energy/company):** **Ranking nudge only** via a `context_note` injected
  into the rank step. No new `QueryIntent` fields (YAGNI).

## Architecture

The wizard is a thin new front-end over the existing online pipeline. It produces a structured
`QueryIntent` plus a human-readable recap, then calls a lightly-refactored `ask()` that skips
`parse_intent` and reuses everything downstream: TMDB Discover, LLM semantic suggestions,
content-type-aware watch filter, streaming-availability annotation, and ranking.

**Server stays stateless.** The in-progress Q&A is carried client-side in a hidden form field as
JSON (HTMX), so it survives page refresh and adds no server-side session memory or lifecycle
concerns. This fits the single-user homelab product philosophy.

### Data flow

```
GET  /wizard         -> wizard.html shell; hx-trigger="load" fires the first turn
POST /wizard/next    -> append current answers to state -> wizard.next_turn(state, ctx)  [1 reason call]
       |- action "ask"        -> render _wizard_step.html   (next question + chips)
       |- action "recommend"  -> ask(ctx, intent_override=..., context_note=...) -> _wizard_results.html
POST /wizard/refine  -> adjust context_note / excludes -> re-run ask() -> swap results
```

## Components

### 1. Wizard engine — `recommender/wizard.py` (new)

- `WizardState` — JSON-serializable dataclass:
  - `turns: list[{prompt: str, selected: list[str], free_text: str}]`
  - `turn_count: int`
- `next_turn(state: WizardState, ctx: RecommendContext) -> dict` — one `role=reason` LLM call.
  - **Prompt input:** the taste-profile summary as a prior (reuses
    `query_engine._profile_for_prompt`), the Q&A so far, `turn_count`, and the cap. Instruction:
    probe today's context (mood, energy, time available, alone/with others, novelty appetite); skip
    what the profile already implies; keep chips short and tappable.
  - **Output:** strict JSON, exactly one of:
    - `{"action": "ask", "prompt": str, "subtext": str, "chips": [{"label": str, "value": str}], "multi": bool, "allow_free_text": bool}`
    - `{"action": "recommend", "summary": str, "intent": {<QueryIntent fields>}, "context_note": str}`
- **Cap enforcement (server-side):** once `turn_count` reaches the cap (default 4, from config), the
  next call forces finalize regardless of the LLM's chosen action. The "show me now" button posts
  `finish=1`, triggering immediate finalize. This bounds loops and LLM cost.
- **Intent mapping:** hard signals (genre, content_type, language, mood) map to existing
  `QueryIntent` fields. Soft signals with no field today (runtime, energy, company) ride in
  `context_note`.

### 2. `ask()` integration — `recommender/query_engine.py` (small refactor)

New signature:

```python
def ask(query, ctx, top_n_override=None, conv_ctx=None,
        intent_override=None, context_note=None) -> list[Recommendation]:
```

- If `intent_override` is provided, skip `parse_intent` (currently line 539) and use it directly.
- If `context_note` is provided, append it to the **rank** prompt so soft signals steer ordering.
- No behavior change for existing callers (both new params default to `None`).

### 3. Web layer — `recommender/web.py` + templates

- Routes: `GET /wizard`, `POST /wizard/next`, `POST /wizard/refine`. CSRF via the existing
  `htmx:configRequest` header injection.
- State carried in a hidden `state` input (JSON); each step's form posts `state` + the current
  question's answers (selected chip values + free text). HTMX swaps the `#wizard-stage` innerHTML.
- Templates (new):
  - `wizard.html` — extends `base.html`; the stage shell + `hx-trigger="load"` for the first turn.
  - `_wizard_step.html` — one question card (progress, prompt, subtext, chips, free text, actions).
  - `_wizard_results.html` — recap line + reuse of the existing `result-card` markup + refine bar.

## Client-side design (frontend-design)

Extends the existing **dark editorial** aesthetic (do not introduce a clashing theme):
`DM Serif Display` headlines, `DM Sans` body, `JetBrains Mono` uppercase micro-labels, coral accent
`#e85d4a` with amber/teal/lavender, cards on `#1c1922`, staggered `slideUp` reveals.

- **Tone:** a focused "concierge" — calmer and more spacious than the Discover search box, but
  unmistakably the same product.
- **Entry points:** a `Concierge` nav link, plus a card on the Discover page —
  *"Not sure what to watch? Let me ask a few quick questions ->"* (serif prompt, accent arrow,
  subtle hover lift).
- **Stage:** centered single column, generous negative space. Mono kicker
  `GUIDED PICK - QUESTION 02`, a large `DM Serif Display` question, `--body` subtext.
- **Progress:** a 4-segment mono bar; filled = `--accent`, pending = `--border`. Honest about the
  "up to 4, maybe fewer" cap.
- **Chips (multi-select):** pill buttons evolved from `.btn-ghost`. Unselected = bordered/muted;
  selected = accent-tinted fill + `--accent` border + a check glyph, with `aria-pressed`. Staggered
  reveal via existing `slideUp` + `animation-delay`. Selection is a tactile micro-interaction
  (border snap + 1px lift).
- **Free text:** slim optional `.input-field` below chips — *"or tell me in your own words..."*.
- **Actions:** `Continue` (`.btn-primary`), `Show me something now` (`.btn-ghost`, early exit), and
  a quiet `<- back` link.
- **Between turns:** a "composing" state reusing the spinner with rotating mono microcopy
  (*"reading the room..."*).
- Real buttons, keyboard-navigable, CSS-only motion (no JS framework — fits the HTMX stack).

## Results & refinement

- Reuses the existing `result-card` markup.
- Prepends a serif recap — *"Because you wanted something light, short, and a little offbeat —"*.
- **Refine bar** of ghost chips: `lighter` - `shorter` - `more obscure` - `surprise me` -
  `none of these`. Refinement reuses `ConversationContext` to exclude already-shown titles and
  re-runs in place (no wizard restart).
- A `tweak this search ->` link drops the synthesized query into the classic search box for power
  users.

## Error handling

- **Malformed LLM JSON:** retry once; still bad -> graceful finalize with what we have, or fall back
  to classic search pre-filled with the partial recap (recoverable, per product philosophy).
- **LLM / network failure:** inline error card with `try again` + `use classic search instead`.
- **Empty results after finalize:** recap + *"nothing matched on your platforms — loosen the
  filter?"* refine action (reuses the existing empty-state pattern).
- **Tampered / unparseable state JSON:** restart the wizard cleanly from turn 0.
- **Cap** is enforced server-side regardless of LLM output.

## Testing

Follows existing patterns in `tests/test_query_engine.py` and `tests/test_main.py`.

- Engine (`tests/test_wizard.py`, new): `next_turn` with a stub `LLMClient` — ask vs recommend
  branching, cap forces finalize, malformed-JSON fallback, intent mapping.
- `ask(intent_override=...)` skips `parse_intent` (LLM not called for parse); `context_note` reaches
  the rank prompt.
- Web routes: `GET /wizard`, `POST /wizard/next` (both branches), `/wizard/refine`,
  malformed-state restart, CSRF enforcement.

## Cost note

Each turn is one `role=reason` call; bounded at the cap (default 4) plus finalize, then the usual
candidate-gen + rank. Wizard turns are short prompts. Acceptable for single-user use; the cap keeps
it bounded.

## Out of scope

- CLI wizard front-end (web only for now).
- New `QueryIntent` hard-filter fields (runtime/energy stay as ranking nudges).
- Server-side session storage for wizard state (client-carried JSON instead).
- Persisting wizard sessions to history (a wizard run can still save its synthesized search via the
  existing search-history path, but no new wizard-specific persistence).

## Config

- `wizard.max_questions` (default 4) — the hard cap. Lives in `config.yaml` under a new `wizard`
  section, read via `config.py` with a sensible default.
