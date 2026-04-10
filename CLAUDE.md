# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

Streamline is a personal streaming recommendation engine. It ingests real watch history (Netflix, Prime Video, Apple TV, manual lists), enriches titles via TMDB and LLM, builds a full taste profile from all watched content, then answers natural language queries using hybrid candidate generation (TMDB Discover + LLM semantic suggestions). Supports Anthropic (Claude), Google (Gemini), and OpenAI as LLM providers.

## Product Philosophy

Streamline is a personal media system for one owner. Optimize for trust, clarity, and low operational burden over scale, abstraction, or SaaS-style architecture.

The product is the local library, profile, and recommendation quality. Every interface should strengthen that core model rather than compete with it or hide it behind unnecessary automation.

The CLI and web UI are both valid interfaces to the same system. Put each task in the interface that makes it clearer and safer, not the one that looks more "production". Browser-based settings are acceptable when they remain understandable and recoverable.

State-changing or expensive operations must be explicit, observable, and recoverable. Saving a setting, rebuilding derived data, and ingesting watch history are different actions and should not be blurred together.

Convenience features are welcome, but they must not remove the simple fallback path. A user should always be able to understand what happened, inspect the local state, and recover with a direct command.

Use progressive enhancement, not dependency layering. HTMX, JavaScript, background jobs, and deployment packaging may improve the experience, but core workflows must continue to work when optional layers fail.

Do not add infrastructure or complexity unless it materially improves the single-user experience. Prefer the simplest design that keeps behavior predictable and the system easy to operate.

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
./recommend setup --ingest-only                # validate configured provider zips
./recommend --debug "spy thriller"             # full pipeline trace
./recommend --provider gemini "spy thriller"   # use Gemini instead of default
./recommend --liked "Title"                    # feedback
./recommend --add "Title" --type tv            # add to watch history

# Web UI
./recommend-web start                          # http://localhost:5050
./recommend-web stop
./recommend-web restart
```

Required environment variables: `TMDB_API_KEY`, plus `ANTHROPIC_API_KEY` and/or `GEMINI_API_KEY` or `OPENAI_API_KEY`.
`.env` is optional local convenience. Settings in `config.yaml`: provider, model assignments, tunables, data paths.

## Architecture

Two-phase LLM pipeline. LLM calls use roles ("fast" for enrichment, "reason" for reasoning) mapped to provider-specific models in config.yaml.

**Offline (setup.py):** Ingest CSVs -> apply title overrides -> TMDB metadata fetch (with guessit classification + title cleanup fallback) -> watch index build (with rapidfuzz dedup) -> LLM enrichment (role=fast, only caches successes) -> LLM taste profile (role=reason, batched, processes ALL enriched titles, auto-backs up previous).

**Online (query_engine.py):** Parse intent (role=reason, supports conversational context) -> hybrid candidate generation (TMDB Discover + LLM suggestions, always both) -> content-type-aware watch filter -> streaming availability annotation -> rank (role=reason, query relevance primary, taste profile secondary).

### Key Modules
- `recommender/llm.py` — LLM provider abstraction. ABC-based `LLMClient` with `AnthropicClient` and `GeminiClient`. Role-based model dispatch, token usage tracking, rate limit retry.
- `recommender/ingestion/` — Platform parsers. Manual titles use `datetime.now()` for competitive scoring.
- `recommender/tmdb_client.py` — Metadata lookup with guessit title classification, title cleanup fallback (strips suffixes, tries alternate content type), discover endpoint (page-limited), watch providers.
- `recommender/watch_index.py` — Content-type-aware dual-key dedup (TMDB ID + `(normalized_title, content_type)`). Post-build rapidfuzz dedup. Stale cache cleanup.
- `recommender/enricher.py` — LLM enrichment (role=fast), 30s timeout, rate limit retry. Only caches successful responses. Identity-keyed index (`content_type/tmdb_id` or `unknown/slug`).
- `recommender/taste_profile_builder.py` — Batched profile builder (200 titles/batch, rate limit retry, merge pass). No top-N limit.
- `recommender/signals.py` — Scoring: completion (50%) + rewatch (30%) + true half-life recency decay (20%).
- `recommender/query_engine.py` — Full online pipeline. "Why not X?" trace mode, conversational context, platform filtering.
- `recommender/overrides.py` — Title override system (data/overrides.json). Auto-detects changes and triggers rebuild.
- `recommender/feedback.py` — (Deprecated) Original JSON-based feedback storage. Migrated to `user_store.py` SQLite tables.
- `recommender/user_store.py` — SQLite storage for watchlist (`saved_titles`), ratings (`title_ratings`), and manual archive additions (`manual_archive_entries`). Migration from `feedback.json`.
- `recommender/user_state.py` — `UserStateIndex` snapshot for TMDB-ID-first matching in query filtering and UI rendering.
- `recommender/web.py` — Flask web UI with HTMX search, poster grid, taste profile clusters, watchlist management (save/unsave/export CSV), search history with user state.
- `recommender/main.py` — Rich CLI with spinners, panels, stderr/stdout separation, REPL with inline feedback, usage stats.

### Data Models
- `QueryIntent` — genres, countries, languages, moods, similar_to, platforms, content_type, top_n
- `Recommendation` — title, score, explanation, streaming_providers
- `ConversationContext` — tracks last query/results for refinement ("more like that")
- `UsageStats` — accumulated token counts and cost per query

### Cache Layout
All under `recommender/cache/`: `tmdb/`, `enrichments/` (+ identity-keyed index.json), `providers/`, `watch_index.json`, `taste_profile.txt` (+ timestamped backups), `feedback.json`. User-managed state (watchlist, ratings, manual archive) lives in the same SQLite database as imported watch events (`events.db`).

## Configuration

- **Environment variables** — secrets: `TMDB_API_KEY`, `ANTHROPIC_API_KEY`, `GEMINI_API_KEY`, `OPENAI_API_KEY`
- **`.env`** — optional local convenience for setting those variables
- **`config.yaml`** — all settings in sections:
  - `provider`, `models.*` — LLM provider and model assignments
  - `llm.*` — timeouts, token limits, batch sizes, rate limit wait
  - `scoring.*` — engagement weights (completion/rewatch/recency), fallback runtimes
  - `manual.*` — synthetic timestamp and durations for manual list titles
  - Top-level: `default_top_n`, `min_vote_count`, `recency_half_life_days`, `watch_region`, `streaming_platforms`
  - Data paths: `platform_paths.*` (exact `.zip` file paths for netflix/prime/apple_tv; null to disable), `overrides_path`
- `models.<provider>.api_key_env` is optional. Use it only for non-standard environment variable names.
- **`config.py`** — thin loader, reads settings from `config.yaml` and secrets from the environment. All values have sensible defaults.
