# Architecture

## Overview

Two-phase LLM pipeline. The offline phase runs once (or on demand) to build persistent artifacts. The online phase runs on every query.

```
OFFLINE                                     ONLINE
───────────────────────────────────         ──────────────────────────────────
Watch history CSVs                          User query (natural language)
      │                                           │
      ▼                                           ▼
  Ingestion                              parse_intent()  ← Claude Sonnet
  (netflix/prime/manual parsers)               │
      │                                    QueryIntent
      ▼                                    (genres, countries,
  TMDB metadata fetch                       languages, year range,
  (cached to disk)                          content_type, top_n, ...)
      │                                           │
      ▼                                           ▼
  Watch index                           TMDB Discover API
  (tmdb_ids + normalized titles)        (filtered candidates)
      │                                           │
      ▼                                           ▼
  Claude Haiku enrichment               Filter watched titles
  (2-3 sentence descriptions,           (via watch index)
   cached per tmdb_id)                         │
      │                                          ▼
      ▼                                   enrich_batch()  ← Claude Haiku
  Taste profile                          (cached descriptions)
  (Claude Sonnet reads top 50                    │
   titles + enrichments, writes                  ▼
   a multi-cluster taste summary)        rank_candidates()  ← Claude Sonnet
                                         (ranks against taste profile,
                                          explains fit, returns top_n)
```

## Components

### Ingestion (`recommender/ingestion/`)

| Module | Source | Key fields |
|--------|--------|------------|
| `netflix.py` | `ViewingActivity.csv` | title, duration, timestamp, profile |
| `prime.py` | `Viewing History.csv` | title, duration, timestamp |
| `manual.py` | `data/manual/tv.csv` + `movies.csv` | title only; synthetic duration + 2022-01-01 date |

All parsers emit `WatchEvent` dataclasses (defined in `base.py`). Manual entries use a neutral past date and full-watch duration as proxies since no real viewing data exists.

### Signals (`recommender/signals.py`)

Computes a score per unique title from raw watch events:

- **Completion ratio** — `watched_duration / total_duration`
- **Rewatch bonus** — multiplier for titles watched more than once
- **Recency decay** — exponential decay with configurable half-life (default 90 days)

Used by the taste profile builder to select the most meaningful titles.

### TMDB Client (`recommender/tmdb_client.py`)

Two roles:

1. **Metadata lookup** (`get_metadata`) — text search by title + content type, returns `TmdbMetadata` (genres, cast, keywords, vote average, language, director/creator). Cached at `recommender/cache/tmdb/`.

2. **Candidate discovery** (`search_by_filters`) — calls the TMDB Discover endpoint with genre IDs, origin countries, languages, and year range. Returns up to `size` results with pagination. Used by the online query path.

Genre name → TMDB ID mappings are stored as module-level dicts (`MOVIE_GENRE_IDS`, `TV_GENRE_IDS`).

### Watch Index (`recommender/watch_index.py`)

Persisted at `recommender/cache/watch_index.json`.

Dual-key lookup for cross-platform deduplication:
- **Primary key** — TMDB ID (reliable, platform-agnostic)
- **Fallback key** — normalized title (lowercased, parentheticals stripped)

The `_normalize()` function strips suffixes like `(4K UHD)` or `(Hindi Subtitled)` that platforms append to titles. Deduplication at build time is TMDB-ID-first: if two events resolve to the same TMDB ID, only one entry is kept.

### Enricher (`recommender/enricher.py`)

Calls Claude Haiku (`claude-haiku-4-5-20251001`) to generate a 2-3 sentence semantic description per title, capturing mood, pacing, tone, cultural flavor, and themes.

Cache layout:
```
recommender/cache/enrichments/
  tv/{tmdb_id}.txt
  movie/{tmdb_id}.txt
  unknown/{title-slug}.txt    ← titles TMDB couldn't resolve
  index.json                  ← title → description map for fast reload
```

Falls back to a keyword string (genres + cast + language) on API error or timeout. Timeout is 30 seconds per call.

### Taste Profile Builder (`recommender/taste_profile_builder.py`)

Takes the top 50 titles by signal score, filters to those with enrichments, and asks Claude Sonnet (`claude-sonnet-4-6`) to write a multi-cluster taste profile — a prose document describing viewing patterns, tone preferences, and engagement signals.

The profile is saved to `recommender/cache/taste_profile.txt` and loaded on every query. It is the primary personalisation artifact.

### Query Engine (`recommender/query_engine.py`)

The online pipeline in three steps:

**1. `parse_intent(query, client)`**

Claude Sonnet parses the natural language query into a `QueryIntent` dataclass:

```python
@dataclass
class QueryIntent:
    genres: list[str]
    origin_countries: list[str]       # ISO-3166 alpha-2
    languages: list[str]              # ISO-639-1
    mood_descriptors: list[str]
    similar_to: list[str]
    max_runtime_minutes: int | None
    year_from: int | None
    year_to: int | None
    unwatched_only: bool
    special_intent: str | None        # "abandoned", "watchlist", "family"
    content_type: str                 # "tv", "movie", "both"
    top_n: int                        # 1 default; 3-5 for "a few"/"some options"
```

**2. TMDB Discover + watch filter**

`search_by_filters` is called with the parsed intent fields. Results are filtered through `watch_index.is_watched()`. If zero candidates remain, `_generate_suggestions()` asks Claude for specific title suggestions which are then looked up via TMDB.

**3. `rank_candidates(query, taste_profile, candidates, enrichments, client, top_n)`**

Claude Sonnet receives the taste profile, the query, and the candidate list (with enrichment descriptions). It returns a ranked JSON array with `title`, `explanation`, and `score`. Hallucinated titles (not in the candidate set) are silently dropped. Results are sliced to `top_n`.

### Setup Orchestration (`recommender/setup.py`)

Coordinates the offline pipeline:

1. Load watch events from all sources
2. If `--refresh-data` or no watch index: fetch TMDB metadata → build watch index → run `enrich_batch` → save `index.json`
3. If `--refresh-profile` or no taste profile: run `build_taste_profile` → save to disk

Skipping is index-existence-gated: if `watch_index.json` exists and no refresh flags are set, the expensive steps are skipped entirely.

### CLI (`recommender/main.py`)

`load_context()` assembles a `RecommendContext` from disk (watch index, taste profile, TMDB client, Anthropic client). Exits with a clear error if required artifacts are missing.

Two modes:
- **Single-shot**: `python3 -m recommender.main "query"`
- **Interactive REPL**: `python3 -m recommender.main`

## Cache Layout

```
recommender/cache/
  tmdb/                        TMDB metadata (JSON, keyed by title+type)
  enrichments/
    tv/{tmdb_id}.txt
    movie/{tmdb_id}.txt
    unknown/{slug}.txt
    index.json                 title → description index
  watch_index.json             [{tmdb_id, title, content_type}, ...]
  taste_profile.txt            Claude Sonnet prose output
```

## Configuration (`config.py`)

| Key | Description |
|-----|-------------|
| `TMDB_API_KEY` | TMDB v3 API key |
| `ANTHROPIC_API_KEY` | Anthropic API key (from env) |
| `PLATFORM_PATHS` | Dict of platform → CSV path |
| `MANUAL_TV_PATH` | Path to `data/manual/tv.csv` |
| `MANUAL_MOVIES_PATH` | Path to `data/manual/movies.csv` |
| `CACHE_DIR` | TMDB metadata cache |
| `ENRICHMENT_CACHE_DIR` | Enrichment text cache |
| `TASTE_PROFILE_PATH` | Taste profile file |
| `WATCH_INDEX_PATH` | Watch index JSON |
| `RECENCY_HALF_LIFE_DAYS` | Signal decay rate (default 90) |

## Model Usage

| Model | Where | Why |
|-------|-------|-----|
| `claude-haiku-4-5-20251001` | Enricher | High volume, simple task, cheap |
| `claude-sonnet-4-6` | Taste profile, intent parser, ranker | Complex reasoning, personalisation |
