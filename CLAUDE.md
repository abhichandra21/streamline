# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

Streamline is a personal streaming recommendation engine. It ingests real watch history (Netflix, Prime Video, manual lists), enriches titles via TMDB and Claude AI, builds a taste profile, then ranks new candidates against that profile for natural language queries.

## Commands

```bash
# Run tests
python3 -m pytest tests/ -v

# Run a single test file
python3 -m pytest tests/test_query_engine.py -v

# Run a single test
python3 -m pytest tests/test_query_engine.py::test_parse_intent_basic -v

# Offline setup (fetches metadata, builds enrichments + taste profile)
python3 -m recommender.setup
# With flags: --refresh-data, --refresh-profile

# Interactive query mode
python3 -m recommender.main

# Single query
python3 -m recommender.main "good British crime drama"
```

Required env vars: `TMDB_API_KEY`, `ANTHROPIC_API_KEY`.

No build step, no linter configured. Pure Python, no venv checked in.

## Architecture

Two-phase LLM pipeline:

**Offline (setup.py):** Ingest CSVs -> TMDB metadata fetch -> watch index build -> Claude Haiku enrichment -> Claude Sonnet taste profile generation. Each step is cached and skipped on subsequent runs unless `--refresh-*` flags are passed.

**Online (query_engine.py):** Parse natural language intent (Sonnet) -> TMDB Discover filter -> exclude watched titles -> enrich candidates (Haiku) -> rank against taste profile (Sonnet) -> return scored recommendations.

### Model Usage
- **Claude Haiku** — cheap/fast enrichment (2-3 sentence title descriptions)
- **Claude Sonnet** — intent parsing, candidate ranking, taste profile building

### Key Modules
- `recommender/ingestion/` — Platform parsers (Netflix, Prime, manual). Each exposes `parse()` returning `WatchEvent` list.
- `recommender/tmdb_client.py` — TMDB metadata lookup and discover endpoint. Caches to `recommender/cache/tmdb/`.
- `recommender/watch_index.py` — Dual-key dedup (TMDB ID primary, normalized title fallback).
- `recommender/enricher.py` — Haiku enrichment with 30s timeout. Falls back to keyword string on failure.
- `recommender/taste_profile_builder.py` — Sonnet analyzes top 50 titles by engagement score.
- `recommender/signals.py` — Engagement scoring: completion (50%) + rewatch (30%) + recency decay (20%).
- `recommender/query_engine.py` — Full online pipeline. Handles special intents ("abandoned", "watchlist", "family").

### Data Models (dataclasses)
- `WatchEvent` — ingestion output (platform, title, content_type, duration, timestamp)
- `QueryIntent` — parsed query (genres, countries, languages, moods, year range, content_type, top_n)
- `TmdbMetadata` — TMDB response (tmdb_id, title, genres, cast, vote_average)
- `Recommendation` — final output (title, score, explanation)

### Cache Layout
All under `recommender/cache/`: `tmdb/` (metadata JSON), `enrichments/` (descriptions + index.json), `watch_index.json`, `taste_profile.txt`.

## Configuration

All in `config.py`. Key tunables: `RECENCY_HALF_LIFE_DAYS` (90), `CANDIDATE_POOL_SIZE`, `MIN_VOTE_COUNT`, `DEFAULT_TOP_N`. Platform CSV paths are in `PLATFORM_PATHS`, `MANUAL_TV_PATH`, `MANUAL_MOVIES_PATH`.
