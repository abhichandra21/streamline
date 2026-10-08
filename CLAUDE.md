# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

Streamline is a personal streaming recommendation engine. It ingests real watch history (Netflix, Prime Video, Apple TV, Disney+, Max, Plex, manual lists), enriches titles via TMDB and LLM, builds a full taste profile from all watched content, then answers natural language queries using hybrid candidate generation (TMDB Discover + LLM semantic suggestions). Supports Anthropic (Claude), Google (Gemini), and OpenAI as LLM providers.

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
./recommend setup --refresh-imdb               # re-download IMDb ratings, rebuild Find's language lists
./recommend --debug "spy thriller"             # full pipeline trace
./recommend --provider gemini "spy thriller"   # use Gemini instead of default
./recommend --liked "Title"                    # feedback
./recommend --add "Title" --type tv            # add to watch history
./recommend plex ratings                       # bring Plex rating changes into Streamline

# Web UI
./recommend-web start                          # http://localhost:5051
./recommend-web stop
./recommend-web restart
# Read-only JSON + iCal for Home Assistant: /api/summary, /api/on-deck,
# /api/watchlist, /api/coming-soon.ics (see docs/home-assistant.md)

# Docker: same commands inside the container; data, cache, logs and both config files are bind mounts
docker compose run --rm streamline ./recommend setup
docker compose up -d                           # image: ghcr.io/abhichandra21/streamline

# Docs site (Astro Starlight, in site/). Screenshots come from a made-up demo library, never real data
./venv/bin/python demo/build.py /tmp/streamline-demo   # build the demo copy (needs TMDB_API_KEY, makes no LLM calls)
(cd site && npm run screenshots -- /tmp/streamline-demo) # recapture site/src/assets/screenshots/
(cd site && npm run dev)                                 # preview the docs

# Make local an exact copy of the home server's state (backs up local first)
./recommend-mirror
./recommend-mirror --dry-run                   # show the steps, change nothing
```

Required environment variables: `TMDB_API_KEY`, plus `ANTHROPIC_API_KEY` and/or `GEMINI_API_KEY` or `OPENAI_API_KEY`.
`.env` is optional local convenience. Defaults in `config.yaml` (tracked); overrides in `config.local.yaml` (gitignored): export paths, and whatever the Settings page saves, which writes only the values that differ from `config.yaml`.

## Docs

User-facing docs live in `site/src/content/docs/`. When a change alters what a user sees or does, update the matching page in the same PR, and recapture screenshots when a page's look changes. `docs/home-assistant.md` is the owner's own deployment notes and stays out of the public site.

## Architecture

Two-phase LLM pipeline. LLM calls use roles ("fast" for enrichment, "reason" for reasoning) mapped to provider-specific models in config.yaml.

**Offline (setup.py):** Ingest CSVs -> apply title overrides -> TMDB metadata fetch (with guessit classification + title cleanup fallback) -> watch index build (with rapidfuzz dedup) -> LLM enrichment (role=fast, only caches successes) -> LLM taste profile (role=reason, batched, processes ALL enriched titles, auto-backs up previous).

**Online (query_engine.py):** Parse intent (role=reason, supports conversational context) -> hybrid candidate generation (TMDB Discover + LLM suggestions, always both) -> content-type-aware watch filter -> streaming availability annotation -> rank (role=reason, query relevance primary, taste profile secondary).

**Mood Match wizard (wizard.py, web routes):** Alternative entry point to the same online pipeline. Instant content-type tap (no LLM) -> adaptive role=reason question loop grounded in the taste profile (soft floor `min_questions`, hard cap `max_questions`, user can bail via "Show me something now") -> review -> background finalize job with `/wizard/jobs/<id>/poll` polling, merging the adaptive intent over the deterministic seed. `/wizard/refine` and `/wizard/replay` re-run from structured intent.

### Key Modules
- `recommender/llm.py` — LLM provider abstraction. ABC-based `LLMClient` with `AnthropicClient` and `GeminiClient`. Role-based model dispatch, token usage tracking, rate limit retry.
- `recommender/ingestion/` — Platform parsers. Manual titles use `datetime.now()` for competitive scoring; setup stores them in SQLite as provider `manual` (replaced on each setup, kept if the files are missing).
- `recommender/tmdb_client.py` — Metadata lookup with guessit title classification, title cleanup fallback (strips suffixes, tries alternate content type), discover endpoint (page-limited), watch providers. `get_imdb_id()` maps a TMDB title to its IMDb ID.
- `recommender/imdb_ratings.py` — Local copy of IMDb's daily `title.ratings.tsv.gz` in SQLite. TMDB stays the catalogue and identity; IMDb only supplies rating and vote count. Every displayed, filtered, or ranked rating prefers IMDb and falls back to TMDB (`TmdbMetadata.rating` / `rating_source`). Refreshed by setup and, in the web UI, by a background job once the copy is a day old.
- `recommender/language_catalog.py` — Find's original-language lists (Hindi). A background build reads every TMDB Discover page for the language over 10 years with no TMDB vote floor, attaches IMDb ratings, and saves the list; Find then filters (period, genre, `LANGUAGE_MIN_IMDB_VOTES`, IMDb rating) and sorts it locally. Built on first use from the Find page or by `--refresh-imdb`, then rebuilt daily.
- `recommender/plex.py` — Plex webhook (`POST /plex/webhook?token=`, exempt from password and CSRF) saves each `media.scrobble` as a `plex` watch event with an exact TMDB ID (movies from the payload, shows by one cached Plex lookup). After each play, and via `./recommend plex ratings`, syncs Plex ratings newer-wins with a scan-start high-water mark in `user_store_meta`. Plex plays exist only in SQLite, so `event_store.PRESERVED_PROVIDERS` keeps setup from deleting them. Never writes to Plex.
- `recommender/tvmaze.py` — A followed show's regular air time (TVmaze schedule time and timezone), looked up by IMDb then TVDB ID, cached in `cache/tvmaze/` and re-checked weekly. On Deck counts an episode as aired at its TMDB air date plus that time; shows with no set time (streaming) count from midnight Central. Failures keep the last saved time or the midnight rule. TVmaze data is CC BY-SA 4.0, credited on On Deck.
- `recommender/watch_index.py` — Content-type-aware dual-key dedup (TMDB ID + `(normalized_title, content_type)`). Post-build rapidfuzz dedup. Stale cache cleanup.
- `recommender/enricher.py` — LLM enrichment (role=fast), 30s timeout, rate limit retry. Only caches successful responses. Identity-keyed index (`content_type/tmdb_id` or `unknown/slug`).
- `recommender/taste_profile_builder.py` — Batched prose profile builder (200 titles/batch, rate limit retry, merge pass). No top-N limit. Rebuilt only on request (`--refresh-profile`, `--rethink-themes`, or first install); `--refresh-data` never rebuilds it.
- `recommender/taste_tags.py`, `taste_themes.py`, `taste_rows.py` — Taste rows built from Loved titles (Rate It, followed shows): 5-8 tags per title, a 10-16 theme map, code placement with saved AI placements for unclear titles, saved row words. All cached, so an unchanged rebuild makes no LLM calls.
- `recommender/show_tracker.py` — On Deck: followed shows, release snapshots, ready-now / Coming soon / Worth following sections.
- `recommender/signals.py` — Scoring. Every watched title scores 1.0 by default (`scoring.use_viewing_signals: false`); when on, completion (50%) + rewatch (30%) + true half-life recency decay (20%).
- `recommender/query_engine.py` — Full online pipeline. "Why not X?" trace mode, conversational context, platform filtering.
- `recommender/wizard.py` — Mood Match adaptive loop. One role=reason call per turn returns the next question or a finish signal carrying a synthesized `QueryIntent`. Soft floor (`WIZARD_MIN_QUESTIONS`) rejects an early recommend; hard cap (`WIZARD_MAX_QUESTIONS`) forces finalize. `WizardState` is carried in a hidden form field, size/turn-bounded.
- `recommender/wizard_flow.py` — Deterministic side of the wizard: the instant content-type tap (counts as question 1, no LLM), recommendation seed builder, review surface, and merge of the adaptive intent over the seed.
- `recommender/overrides.py` — Title override system (data/overrides.json). Auto-detects changes and triggers rebuild.
- `recommender/feedback.py` — (Deprecated) Original JSON-based feedback storage. Migrated to `user_store.py` SQLite tables.
- `recommender/user_store.py` — SQLite storage for watchlist (`saved_titles`), ratings (`title_ratings`), and manual archive additions (`manual_archive_entries`). Migration from `feedback.json`.
- `recommender/user_state.py` — `UserStateIndex` snapshot for TMDB-ID-first matching in query filtering and UI rendering.
- `recommender/api.py` — Read-only `/api/` blueprint for Home Assistant: summary, On Deck, watchlist, iCal feed. No LLM calls. On Deck endpoints refresh releases through the same `_show_sections_with_refresh()` helper as `/shows`. Optional `STREAMLINE_API_TOKEN` bearer auth for `GET /api/*` only.
- `recommender/web.py` — Flask web UI with HTMX search, poster grid, taste profile clusters, watchlist management (save/unsave/export CSV), search history with user state.
- `recommender/main.py` — Rich CLI with spinners, panels, stderr/stdout separation, REPL with inline feedback, usage stats.

### Data Models
- `QueryIntent` — genres, countries, languages, moods, similar_to, platforms, content_type, top_n
- `Recommendation` — title, score, explanation, streaming_providers
- `ConversationContext` — tracks last query/results for refinement ("more like that")
- `UsageStats` — accumulated token counts and cost per query

### Cache Layout
All under `recommender/cache/`: `tmdb/`, `enrichments/` (+ identity-keyed index.json), `providers/`, `find/` (Find page: US now-playing ids on a 6h TTL, and `language_<code>.json` IMDb-rated language lists), `tmdb/external_ids/` (TMDB-to-IMDb ID map), `imdb_ratings.db` (IMDb ratings copy), `releases/` and `tvmaze/` (On Deck), `watch_index.json`, `taste_profile.txt` (+ timestamped backups), `taste_profile_structured.json`, `taste_tags.json` / `taste_themes.json` / `taste_placements.json` / `taste_words.json` (taste rows), `feedback.json` (deprecated). User-managed state (watchlist, ratings, manual archive) and query history live in the same SQLite database as imported watch events (`data/streamline.db`). A pre-SQLite `query_history.json` is imported once and kept as `query_history.json.migrated`.

## Configuration

- **Environment variables** — secrets: `TMDB_API_KEY`, `ANTHROPIC_API_KEY`, `GEMINI_API_KEY`, `OPENAI_API_KEY`, and for Plex `PLEX_WEBHOOK_TOKEN`, `PLEX_URL`, `PLEX_TOKEN`
- **`.env`** — optional local convenience for setting those variables
- **`config.yaml`** — all settings in sections:
  - `provider`, `models.*` — LLM provider and model assignments
  - `llm.*` — timeouts, token limits, batch sizes, rate limit wait
  - `wizard.*` — `max_questions` (hard cap), `min_questions` (soft floor, bounded by max), `max_tokens` (per-turn output ceiling)
  - `scoring.*` — engagement weights (completion/rewatch/recency), fallback runtimes
  - `manual.*` — synthetic timestamp and durations for manual list titles
  - Top-level: `default_top_n`, `min_vote_count`, `recency_half_life_days`, `watch_region`, `streaming_platforms`
  - Data paths: `platform_paths.*` (exact `.zip` file paths for netflix/prime/apple_tv; null to disable), `overrides_path`
- `models.<provider>.api_key_env` is optional. Use it only for non-standard environment variable names.
- **`config.py`** — thin loader, reads settings from `config.yaml` and secrets from the environment. All values have sensible defaults.
