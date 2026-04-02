# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

Streamline is a personal streaming recommendation engine. It ingests real watch history (Netflix, Prime Video, manual lists), enriches titles via TMDB and LLM, builds a full taste profile from all watched content, then answers natural language queries using hybrid candidate generation (TMDB Discover + LLM semantic suggestions). Supports Anthropic (Claude) and Google (Gemini) as LLM providers.

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
./recommend --provider gemini "spy thriller"   # use Gemini instead of default
./recommend --liked "Title"                    # feedback
./recommend --add "Title" --type tv            # add to watch history

# Web UI
./recommend-web start                          # http://localhost:5050
./recommend-web stop
./recommend-web restart
```

Required env vars in `.env`: `TMDB_API_KEY`, plus `ANTHROPIC_API_KEY` and/or `GEMINI_API_KEY`.
Settings in `config.yaml`: provider, model assignments, tunables, data paths.

## Architecture

Two-phase LLM pipeline. LLM calls use roles ("fast" for enrichment, "reason" for reasoning) mapped to provider-specific models in config.yaml.

**Offline (setup.py):** Ingest CSVs -> apply title overrides -> TMDB metadata fetch (with guessit classification + title cleanup fallback) -> watch index build (with rapidfuzz dedup) -> LLM enrichment (role=fast, only caches successes) -> LLM taste profile (role=reason, batched, processes ALL enriched titles, auto-backs up previous).

**Online (query_engine.py):** Parse intent (role=reason, supports conversational context) -> hybrid candidate generation (TMDB Discover + LLM suggestions, always both) -> content-type-aware watch filter -> streaming availability annotation -> rank (role=reason, query relevance primary, taste profile secondary).

### Key Modules
- `recommender/llm.py` — LLM provider abstraction. ABC-based `LLMClient` with `AnthropicClient` and `GeminiClient`. Role-based model dispatch, token usage tracking, rate limit retry.
- `recommender/ingestion/` — Platform parsers. Manual titles use `datetime.now()` for competitive scoring.
- `recommender/tmdb_client.py` — Metadata lookup with guessit title classification, title cleanup fallback (strips suffixes, tries alternate content type), discover endpoint (page-limited), watch providers.
- `recommender/watch_index.py` — Content-type-aware dual-key dedup (TMDB ID + `(normalized_title, content_type)`). Post-build rapidfuzz dedup. Stale cache cleanup.
- `recommender/enricher.py` — LLM enrichment (role=fast), 30s timeout, rate limit retry. Only caches successful responses.
- `recommender/taste_profile_builder.py` — Batched profile builder (200 titles/batch, rate limit retry, merge pass). No top-N limit.
- `recommender/signals.py` — Scoring: completion (50%) + rewatch (30%) + true half-life recency decay (20%).
- `recommender/query_engine.py` — Full online pipeline. "Why not X?" trace mode, conversational context, platform filtering.
- `recommender/overrides.py` — Title override system (data/overrides.json). Auto-detects changes and triggers rebuild.
- `recommender/feedback.py` — Liked/disliked ratings, title additions. Score multipliers applied at profile rebuild.
- `recommender/web.py` — Flask web UI with HTMX search, poster grid, taste profile clusters.
- `recommender/main.py` — Rich CLI with spinners, panels, stderr/stdout separation, REPL with inline feedback, usage stats.

### Data Models
- `QueryIntent` — genres, countries, languages, moods, similar_to, platforms, content_type, top_n
- `Recommendation` — title, score, explanation, streaming_providers
- `ConversationContext` — tracks last query/results for refinement ("more like that")
- `UsageStats` — accumulated token counts and cost per query

### Cache Layout
All under `recommender/cache/`: `tmdb/`, `enrichments/` (+ index.json), `providers/`, `watch_index.json`, `taste_profile.txt` (+ timestamped backups), `feedback.json`.

## Configuration

- **`.env`** — secrets: `TMDB_API_KEY`, `ANTHROPIC_API_KEY`, `GEMINI_API_KEY`
- **`config.yaml`** — settings: provider, models (fast/reason per provider), tunables (`default_top_n`, `min_vote_count`, `recency_half_life_days`, `watch_region`, `streaming_platforms`), data paths, overrides path
- **`config.py`** — thin loader, reads from both
