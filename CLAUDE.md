# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

Streamline is a personal streaming recommendation engine. It ingests real watch history (Netflix, Prime Video, manual lists), enriches titles via TMDB and Claude AI, builds a full taste profile from all watched content, then answers natural language queries using hybrid candidate generation (TMDB Discover + Claude semantic suggestions).

## Commands

```bash
# Run tests
python3 -m pytest tests/ -v

# Run a single test file
python3 -m pytest tests/test_query_engine.py -v

# Everything goes through ./recommend:
./recommend "good British crime drama"        # single query
./recommend                                    # interactive REPL
./recommend setup                              # first-time offline setup
./recommend setup --refresh-data               # re-fetch TMDB + rebuild all
./recommend setup --refresh-profile            # rebuild taste profile only
./recommend --debug "spy thriller"             # full pipeline trace
./recommend --liked "Title"                    # feedback
./recommend --add "Title" --type tv            # add to watch history

# Web UI
./recommend-web start                          # http://localhost:5050
./recommend-web stop
./recommend-web restart
```

Required env vars in `.env`: `TMDB_API_KEY`, `ANTHROPIC_API_KEY`.

## Architecture

Two-phase LLM pipeline:

**Offline (setup.py):** Ingest CSVs -> TMDB metadata fetch (with title cleanup fallback) -> watch index build -> Claude Haiku enrichment (only caches successes) -> Claude Sonnet taste profile (batched, processes ALL enriched titles, auto-backs up previous).

**Online (query_engine.py):** Parse intent (Sonnet, supports conversational context) -> hybrid candidate generation (TMDB Discover + Claude suggestions, always both) -> content-type-aware watch filter -> streaming availability annotation -> rank (Sonnet, query relevance primary, taste profile secondary).

### Key Modules
- `recommender/ingestion/` — Platform parsers. Manual titles use `datetime.now()` for competitive scoring.
- `recommender/tmdb_client.py` — Metadata lookup with title cleanup fallback (strips suffixes, tries alternate content type), discover endpoint (page-limited), watch providers.
- `recommender/watch_index.py` — Content-type-aware dual-key dedup (TMDB ID + `(normalized_title, content_type)`).
- `recommender/enricher.py` — Haiku enrichment, 30s timeout. Only caches successful responses.
- `recommender/taste_profile_builder.py` — Batched profile builder (200 titles/batch, rate limit retry, merge pass). No top-N limit.
- `recommender/signals.py` — Scoring: completion (50%) + rewatch (30%) + true half-life recency decay (20%).
- `recommender/query_engine.py` — Full online pipeline. "Why not X?" trace mode, conversational context, platform filtering.
- `recommender/feedback.py` — Liked/disliked ratings, title additions. Score multipliers applied at profile rebuild.
- `recommender/web.py` — Flask web UI with HTMX search, poster grid, taste profile clusters.
- `recommender/main.py` — Rich CLI with spinners, panels, stderr/stdout separation, REPL with inline feedback.

### Data Models
- `QueryIntent` — genres, countries, languages, moods, similar_to, platforms, content_type, top_n
- `Recommendation` — title, score, explanation, streaming_providers
- `ConversationContext` — tracks last query/results for refinement ("more like that")

### Cache Layout
All under `recommender/cache/`: `tmdb/`, `enrichments/` (+ index.json), `providers/`, `watch_index.json`, `taste_profile.txt` (+ timestamped backups), `feedback.json`.

## Configuration

All in `config.py`. Key tunables: `DEFAULT_TOP_N` (3), `MIN_VOTE_COUNT` (20), `RECENCY_HALF_LIFE_DAYS` (90), `WATCH_REGION` (US), `STREAMING_PLATFORMS` (empty = annotate only).
