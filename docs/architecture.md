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
| `netflix.py` | `ViewingActivity.csv` | title, duration, timestamp, profile |
| `prime.py` | `Viewing History.csv` | title, duration, timestamp |
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

### Taste Profile Builder (`recommender/taste_profile_builder.py`)

Processes ALL enriched titles (no limit) in batches of 200. Each batch produces a mini taste profile, then a merge pass consolidates them into one document covering every taste cluster. Includes rate limit retry with backoff.

Previous profiles are auto-backed up with timestamps before rebuild.

Negative preferences from the feedback system are included in the prompt, generating a "What you don't enjoy" section.

### Feedback (`recommender/feedback.py`)

Persists user feedback at `recommender/cache/feedback.json`:

- **Liked/disliked ratings** — applied as score multipliers (1.3x liked, 0.5x disliked) during profile rebuild
- **Title additions** — added to watch history with current timestamp
- Disliked titles inform negative preference prompting in the taste profile

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

### Web UI (`recommender/web.py`)

Flask app serving:
- `/` — Home: search bar (HTMX-powered), taste profile clusters (expandable, markdown-rendered), archive poster wall
- `/history` — Watch archive with switchable views (list, poster grid, compact). Search + type filter.
- `/title/:id` — Title detail with poster, TMDB overview, AI analysis, credits, keywords, TMDB link
- `/recommend` — Standalone discover page

### CLI (`recommender/main.py`)

Rich-powered output with spinners during API calls and panel-formatted results. Stderr/stdout separation for pipe-friendly usage. Interactive REPL with conversational context and inline feedback commands (`+liked`, `+disliked`, `+add`).

## Cache Layout

```
recommender/cache/
  tmdb/                          TMDB metadata (JSON, by content_type/tmdb_id)
  enrichments/
    tv/{tmdb_id}.txt
    movie/{tmdb_id}.txt
    unknown/{slug}.txt
    index.json                   title -> description index
  providers/
    {content_type}/{region}/{tmdb_id}.json
  watch_index.json               [{tmdb_id, title, content_type}, ...]
  taste_profile.txt              Claude Sonnet prose output
  taste_profile_*.txt            Timestamped backups
  feedback.json                  User ratings and additions
```

## Configuration (`config.py`)

| Key | Description |
|-----|-------------|
| `TMDB_API_KEY` | TMDB v3 API key (from env) |
| `ANTHROPIC_API_KEY` | Anthropic API key (from env) |
| `PLATFORM_PATHS` | Dict of platform -> CSV path |
| `DEFAULT_TOP_N` | Default results per query (3) |
| `MIN_VOTE_COUNT` | Minimum TMDB votes for discover (20) |
| `RECENCY_HALF_LIFE_DAYS` | Signal decay rate (90) |
| `WATCH_REGION` | Region for streaming providers (US) |
| `STREAMING_PLATFORMS` | User's subscribed platforms |
| `FEEDBACK_PATH` | Feedback JSON path |

## Model Usage

| Model | Where | Why |
|-------|-------|-----|
| `claude-haiku-4-5-20251001` | Enricher | High volume, simple descriptions, cheap |
| `claude-sonnet-4-6` | Taste profile, intent parser, ranker, suggestions | Complex reasoning, personalisation |
