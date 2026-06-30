# Architecture

## Overview

Two-phase LLM pipeline. The offline phase runs once (or on demand) to build persistent artifacts. The online phase runs on every query.

```
OFFLINE                                      ONLINE
────────────────────────────────────         ─────────────────────────────────────
Watch history CSVs                           User query (natural language)
      |                                            |
      v                                            v
  Ingestion                               parse_intent()  <- Claude Sonnet
  (netflix/prime/manual parsers)                |
      |                                     QueryIntent
      v                                     (genres, countries, languages,
  TMDB metadata fetch                        year range, content_type, top_n,
  (with title cleanup fallback;              platforms, similar_to, moods...)
   cached to disk)                                 |
      |                                            v
      v                                   +-------------------+
  Watch index                             | TMDB Discover API |  <- structured filter
  (tmdb_ids + normalized titles           | (filtered by       |
   with content type)                     |  intent fields)    |
      |                                   +--------+----------+
      v                                            |
  Claude Haiku enrichment                          |     +---------------------+
  (2-3 sentence descriptions,                      +---->| Claude suggestions   | <- semantic
   only cached on success)                         |     | (always-on, uses     |
      |                                            |     |  taste profile +     |
      v                                            |     |  similar_to context) |
  Engagement scoring                               |     +--------+------------+
  (completion + rewatch + recency;                 |              |
   true half-life decay)                           v              v
      |                                     Merge + deduplicate candidates
      v                                            |
  Taste profile                                    v
  (Claude Sonnet processes ALL                Filter watched titles
   enriched titles in batches,              (content-type-aware)
   merges into multi-cluster                       |
   prose summary)                                  v
                                            Annotate streaming availability
                                            (TMDB watch providers API, cached)
                                                   |
                                                   v
                                            enrich_batch()  <- Claude Haiku
                                            (cached descriptions)
                                                   |
                                                   v
                                            rank_candidates()  <- Claude Sonnet
                                            (query relevance primary,
                                             taste profile secondary,
                                             returns top_n with explanations)
```

## Components

### Ingestion (`recommender/ingestion/`)

| Module | Source | Key fields |
|--------|--------|------------|
| `netflix.py` | raw Netflix export zip with `ViewingActivity.csv` inside | title, duration, timestamp, profile |
| `prime.py` | raw Prime Video export zip with `Viewing History.csv` inside | title, duration, timestamp |
| `apple_tv.py` | raw Apple privacy export zip with `Video Play Activity.csv` inside | title, duration, timestamp, storefront |
| `manual.py` | `data/manual/tv.csv` + `movies.csv` | title only; synthetic duration + current timestamp |

All parsers emit `WatchEvent` dataclasses (defined in `base.py`). Manual entries use `datetime.now()` as timestamp so they score competitively with platform data. Each file type deduplicates independently (TV and movie lists use separate seen sets).

### Signals (`recommender/signals.py`)

Computes a score per unique title from raw watch events:

- **Completion ratio** (50%) — `watched_duration / runtime` (uses TMDB runtime when available, falls back to 45min TV / 90min movie)
- **Rewatch bonus** (30%) — log-scale multiplier for titles watched more than once
- **Recency decay** (20%) — true half-life decay: `0.5 ^ (days / half_life_days)`

### TMDB Client (`recommender/tmdb_client.py`)

Three roles:

1. **Metadata lookup** (`get_metadata`) — text search by title + content type, returns `TmdbMetadata`. On miss, tries cleaned title variants (strips parenthetical suffixes, edition markers, episode prefixes, season markers) and falls back to alternate content type. Cached at `recommender/cache/tmdb/`.

2. **Candidate discovery** (`search_by_filters`) — calls TMDB Discover endpoint with genre IDs, origin countries, languages, and year range. Page-limited to `MAX_DISCOVER_PAGES` (20). Failed fetches are logged, not silently swallowed.

3. **Watch providers** (`get_watch_providers`) — looks up flatrate streaming availability by region. Cached at `recommender/cache/providers/`.

Genre name -> TMDB ID mappings are stored as module-level dicts. TV has no "thriller" genre; it maps to Mystery (9648).

### Watch Index (`recommender/watch_index.py`)

Persisted at `recommender/cache/watch_index.json`.

Content-type-aware dual-key lookup:
- **Primary key** — TMDB ID
- **Fallback key** — `(normalized_title, content_type)` tuple

This prevents cross-media false matches (watching the TV show "Fargo" won't block the movie "Fargo" from recommendations).

### Enricher (`recommender/enricher.py`)

Calls Claude Haiku to generate 2-3 sentence semantic descriptions per title. Only successful LLM responses are cached — fallback descriptions (keyword strings) are not persisted, allowing retry on subsequent runs.

The enrichment index (`index.json`) uses identity keys: `content_type/tmdb_id` for resolved titles (e.g. `tv/12345`) and `unknown/slug` for unresolved ones. A `_title_keyed_enrichments()` bridge in `setup.py` converts identity keys back to display titles for the taste profile builder.

### Taste Profile Builder (`recommender/taste_profile_builder.py`)

Processes ALL enriched titles (no limit) in batches of 200. Each batch produces a mini taste profile, then a merge pass consolidates them into one document covering every taste cluster. Includes rate limit retry with backoff.

Previous profiles are auto-backed up with timestamps before rebuild.

Negative preferences from the feedback system are included in the prompt, generating a "What you don't enjoy" section.

### User Store (`recommender/user_store.py`)

SQLite storage (`events.db`) for user-managed state:

- **Watchlist** (`saved_titles` with status `watchlist`) — save/unsave from any UI page, CSV export
- **Dismissed** (`saved_titles` with status `dismissed`) — excluded from recommendations
- **Ratings** (`title_ratings`) — liked/disliked, applied as score multipliers during profile rebuild
- **Manual archive** (`manual_archive_entries`) — titles added via CLI or web UI
- Disliked titles inform negative preference prompting in the taste profile

All lookups use TMDB-ID-first matching with normalized-title fallback. `UserStateIndex` (in `user_state.py`) provides a read-only snapshot for fast matching in query filtering and UI rendering.

Note: `recommender/feedback.py` is deprecated. The original JSON-based feedback was migrated to the SQLite tables above.

### Query Engine (`recommender/query_engine.py`)

The online pipeline:

**1. Intent parsing** — Claude Sonnet parses natural language into `QueryIntent` with validation, defaults, and type coercion. Supports conversational context (refinements like "more like that", "but British"). Detects platform filters ("on Netflix"). All API calls have 30s timeout.

**2. Hybrid candidate generation** — Two sources run in parallel:
  - TMDB Discover (structured metadata filter)
  - Claude suggestions (semantic, taste-aware — always runs, not just as fallback)
  
  Both TV and movie versions are kept for suggested titles (ranker decides). Results are deduplicated by TMDB ID.

**3. Watch filter** — Content-type-aware exclusion via watch index.

**4. Streaming availability** — Each candidate annotated with flatrate providers for the configured region. Optionally filtered to user's subscribed platforms.

**5. Ranking** — Claude Sonnet ranks with query relevance as primary signal, taste profile as tiebreaker. Returns JSON with title, explanation, and score.

**Special modes:**
- `"why not X?"` — traces a title through the pipeline and explains exactly where it was filtered
- `"abandoned"` queries — checks watch history for partial viewing and advises whether to continue

### Mood Match Wizard (`recommender/wizard.py`, `recommender/wizard_flow.py`)

A guided alternative to free-text search that turns the taste profile into an interactive question loop, then hands a synthesized `QueryIntent` to the same online pipeline.

The flow is a hybrid of deterministic and LLM-led turns:

1. **Content-type tap** (`wizard_flow`) — an instant first question (movie / TV / either) rendered with no LLM call. It counts as question 1 toward the cap.
2. **Adaptive loop** (`wizard.next_turn`) — one `role="reason"` call per turn. The model sees the taste profile as a prior plus the answers so far and returns either the next question (chips + optional free text) or a finish signal carrying a `QueryIntent` and a free-text ranking note.
3. **Soft floor / hard cap** — the wizard must keep asking until it has `WIZARD_MIN_QUESTIONS` answers before it may finish on its own; below the floor an early `recommend` is rejected and one more question is forced (relenting if the model still will not ask, so the loop is bounded). `WIZARD_MAX_QUESTIONS` is the hard cap, enforced here independent of what the model returns. The user can always bail early with **Show me something now**.
4. **Review** — a recap surface before finalizing, so answers are visible and editable.
5. **Finalize** — the recommendation runs as a background job (`job_registry`); the page polls `/wizard/jobs/<id>/poll` (via `_polling.html`) until results render. When the model finishes, its adaptive intent is merged over the deterministic seed (`merge_intent_with_seed`).
6. **Refine / replay** — `/wizard/refine` applies deterministic directives (`shorter`, `lighter`, `more obscure`, `surprise me`, or free text) to the existing intent and re-runs while hard-excluding already-shown titles; `/wizard/replay` re-runs a stored wizard run from its structured intent (not its recap text).

`WizardState` is carried in a hidden form field across turns (size- and turn-count-bounded as a malformed-payload guard, not a security boundary). Routes live in `web.py` (`/wizard`, `/wizard/next`, `/wizard/jobs/<id>/poll`, `/wizard/refine`, `/wizard/replay`); templates are `wizard.html`, `_wizard_step.html`, `_wizard_review.html`, `_wizard_results.html`, and `_polling.html`.

### Web UI (`recommender/web.py`)

Flask app serving:
- `/` — Home: search bar (HTMX-powered), taste profile clusters (expandable, markdown-rendered), archive poster wall
- `/wizard` — Mood Match guided wizard (see the wizard section above)
- `/history` — Watch archive with switchable views (list, poster grid, compact). Search + type filter + A-Z/Z-A sort.
- `/title/:id` — Title detail with poster, TMDB overview, AI analysis, credits, keywords, TMDB link
- `/recommend` — Standalone discover page
- `/watchlist` — Saved titles with TMDB ratings, CSV export via `/watchlist/export`
- `/watchlist/save`, `/watchlist/unsave` — HTMX toggle endpoints for inline save/unsave from any page
- `/searches` — Query history with user state badges (watchlist, archived, dismissed) per result

### LLM Abstraction (`recommender/llm.py`)

ABC-based `LLMClient` with `AnthropicClient` and `GeminiClient`. Call sites use roles instead of model names:
- `role="fast"` — enrichment (high volume, simple descriptions)
- `role="reason"` — intent parsing, ranking, taste profile, suggestions (complex reasoning)

Model names are resolved from `config.yaml` per provider:
```yaml
models:
  anthropic:
    fast: claude-haiku-4-5-20251001
    reason: claude-sonnet-4-6
  gemini:
    fast: gemini-2.5-flash
    reason: gemini-2.5-pro
```

Gemini-specific handling: output token scaling (x3), JSON mode via `response_mime_type`, rate limit retry (429/RESOURCE_EXHAUSTED/504), extended timeout for Pro thinking mode.

Token usage and cost tracking via `UsageStats` — accumulated per query, printed after results.

### Title Overrides (`recommender/overrides.py`)

`data/overrides.json` maps raw platform titles to corrected titles, direct TMDB IDs, content type corrections, or skip. Auto-detected at setup time — if the overrides file is newer than the watch index, triggers a rebuild without `--refresh-data`.

### CLI (`recommender/main.py`)

Rich-powered output with spinners during API calls and panel-formatted results. Stderr/stdout separation for pipe-friendly usage. Interactive REPL with conversational context and inline feedback commands (`+liked`, `+disliked`, `+add`). Token usage and cost printed after each query.

## Cache Layout

```
recommender/cache/
  tmdb/                          TMDB metadata (JSON, by content_type/tmdb_id)
  enrichments/
    tv/{tmdb_id}.txt
    movie/{tmdb_id}.txt
    unknown/{slug}.txt
    index.json                   identity-keyed description index (content_type/tmdb_id -> text)
  providers/
    {content_type}/{region}/{tmdb_id}.json
  profile_batches/               Intermediate batch profiles (auto-cleaned after merge)
    fingerprint.txt              SHA-256 of scored title list (staleness check)
    batch_01.txt ... batch_NN.txt
  watch_index.json               [{tmdb_id, title, content_type}, ...]
  taste_profile.txt              LLM-generated prose output
  taste_profile_*.txt            Timestamped backups
  feedback.json                  User ratings and additions
```

## Configuration

Secrets come from the environment. Shared application settings live in
`config.yaml`, and optional machine-specific overrides live in
`config.local.yaml`.

**Environment variables**:
- `TMDB_API_KEY` — TMDB v3 API key
- `ANTHROPIC_API_KEY` — Anthropic API key
- `GEMINI_API_KEY` — Google Gemini API key (AIza* for AI Studio, AQ.* for Vertex AI)
- `OPENAI_API_KEY` — OpenAI-compatible API key

**`.env`** — optional local convenience for setting those variables (gitignored)

**`config.yaml`** — tracked shared settings, organized in sections:

| Section | Keys | Description |
|---------|------|-------------|
| *(top-level)* | `provider`, `models.*` | LLM provider and model assignments (fast/reason roles) |
| `llm.*` | `timeout_*`, `tokens_*`, `profile_batch_size`, `rate_limit_wait` | Per-call-type timeouts, token limits, batch sizes |
| `wizard.*` | `max_questions`, `min_questions`, `max_tokens` | Mood Match wizard question cap, soft floor, and per-turn output-token ceiling |
| `scoring.*` | `weight_completion`, `weight_rewatch`, `weight_recency`, `default_*_runtime`, `rewatch_saturation` | Engagement scoring weights and fallback runtimes |
| `manual.*` | `timestamp`, `tv_duration_minutes`, `movie_duration_minutes` | Synthetic values for manual list titles |
| *(top-level)* | `default_top_n`, `min_vote_count`, `recency_half_life_days` | Recommendation tuning |
| *(top-level)* | `watch_region`, `streaming_platforms` | Streaming availability |
| *(top-level)* | `platform_paths.*`, `manual_*_path`, `overrides_path` | Data file locations |

**`config.local.yaml`** — optional local overrides loaded after `config.yaml`.
Use this for machine-specific values such as `platform_paths.*` for personal
watch-history export zips. `config.local.yaml` is gitignored; start from
`config.local.example.yaml`.

Provider API keys use the standard environment variable names by default. Add `models.<provider>.api_key_env` only when a deployment needs a non-standard variable name.

`config.py` is a thin loader that reads `config.yaml`, applies
`config.local.yaml` overrides when present, and reads secrets from the
environment. All values have sensible defaults — a minimal `config.yaml` with
just `provider:` works.

### Taste Profile Batch Caching

The taste profile builder saves intermediate batch profiles to `recommender/cache/profile_batches/` as they complete. A SHA-256 fingerprint of the scored title list detects staleness — if enrichments, scores, or the title set change, cached batches are invalidated. This makes profile rebuilds resumable: if the merge step fails (timeout, rate limit), re-running `--refresh-profile` loads the cached batches and retries only the merge. Batch files are cleaned up after a successful merge.
