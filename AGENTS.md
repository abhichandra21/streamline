# Repository Guidelines

## Product Philosophy

Streamline is a personal media system for one owner. Optimize for trust, clarity, and low operational burden over scale, abstraction, or SaaS-style architecture.

The product is the local library, profile, and recommendation quality. Every interface should strengthen that core model rather than compete with it or hide it behind unnecessary automation.

The CLI and web UI are both valid interfaces to the same system. Put each task in the interface that makes it clearer and safer, not the one that looks more "production". Browser-based settings are acceptable when they remain understandable and recoverable.

State-changing or expensive operations must be explicit, observable, and recoverable. Saving a setting, rebuilding derived data, and ingesting watch history are different actions and should not be blurred together.

Convenience features are welcome, but they must not remove the simple fallback path. A user should always be able to understand what happened, inspect the local state, and recover with a direct command.

Use progressive enhancement, not dependency layering. HTMX, JavaScript, background jobs, and deployment packaging may improve the experience, but core workflows must continue to work when optional layers fail.

Do not add infrastructure or complexity unless it materially improves the single-user experience. Prefer the simplest design that keeps behavior predictable and the system easy to operate.

## Project Structure & Module Organization

Core application code lives in `recommender/`. Key areas:
- `recommender/ingestion/` parses provider exports and manual lists.
- `recommender/query_engine.py`, `tmdb_client.py`, and `taste_profile_builder.py` drive recommendation logic.
- `recommender/templates/` and `recommender/static/` back the Flask web UI.
- `recommender/cache/` stores generated artifacts such as `watch_index.json` and `taste_profile.txt`.

Tests live in `tests/`, with ingestion-specific coverage under `tests/ingestion/`. Entry points are `./recommend` for CLI/setup and `./recommend-web` for the web app. Shared defaults live in `config.yaml`; machine-local overrides belong in `config.local.yaml`.

## Build, Test, and Development Commands

Use the local virtualenv first:

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

Key commands:
- `./recommend setup` rebuilds watch history, TMDB enrichment, and taste profile artifacts.
- `./recommend setup --ingest-only` validates configured provider exports without rebuilding caches.
- `./recommend "good British crime drama"` runs a CLI recommendation query.
- `./recommend-web start` launches the Flask UI on `http://localhost:5050`.
- `pytest -q` runs the full test suite.

## Coding Style & Naming Conventions

Target Python 3.10+. Use 4-space indentation, type hints where the module already uses them, and `snake_case` for functions, variables, and test names. Keep changes direct and consistent with existing patterns. Prefer editing existing files over adding new ones. Comments should explain intent or constraints, not restate obvious code.

## Testing Guidelines

Pytest is the test framework. Name tests `test_<behavior>()` and keep provider-parser coverage in `tests/ingestion/`. When changing ingestion or setup logic, run the affected subset first, then `pytest -q`. For user-facing query or web changes, add or update tests in `tests/test_main.py` and related modules.

## Commit & Pull Request Guidelines

Recent history uses conventional prefixes such as `feat:`, `fix:`, and `refactor:` with imperative summaries. Keep commits scoped to one logical change. PRs should include:
- a concise summary of behavior changes
- validation commands and results
- config or migration notes if setup behavior changes
- screenshots only for visible web UI changes

## Security & Configuration Tips

Never commit secrets or personal watch-history paths. Keep API keys in the environment or `.env`, and keep machine-specific provider zips in `config.local.yaml`, not `config.yaml`.
