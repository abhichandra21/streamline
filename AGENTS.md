# Repository Guidelines

## Project Structure & Module Organization

Streamline is a personal Flask and CLI media recommendation app. Core Python code lives in `recommender/`. Query logic is in `recommender/query_engine.py`, TMDB access in `recommender/tmdb_client.py`, enrichment/profile generation in `recommender/enricher.py` and `recommender/taste_profile_builder.py`, and web routes in `recommender/web.py`.

Templates live in `recommender/templates/`; generated caches and local artifacts live under `recommender/cache/`, `data/`, and `logs/`. Tests live in `tests/`, with shared test helpers such as `tests/mock_llm.py`.

## Build, Test, and Development Commands

Use the local virtualenv first:

```bash
source venv/bin/activate
pip install -r requirements.txt
```

Key commands:

```bash
./recommend setup
```

Rebuilds watch history, TMDB cache, enrichments, and taste profile.

```bash
./recommend "good British crime drama"
```

Runs a CLI recommendation query.

```bash
./recommend-web start
```

Starts the Flask web UI on `http://localhost:5050`.

```bash
python3 -m pytest -q
```

Runs the full test suite.

## Coding Style & Naming Conventions

Target Python 3.10+. Use 4-space indentation, `snake_case` for functions and variables, and direct, descriptive names. Match existing module patterns before introducing new abstractions. Prefer simple functions over framework-heavy designs. Comments should explain intent or constraints, not restate obvious code.

Do not use emoji in code, documentation, templates, or test fixtures.

## Testing Guidelines

Pytest is the test framework. Name tests `test_<behavior>()` and keep them close to the module behavior they verify. Use real SQLite temp files for persistence tests when possible. For web changes, update `tests/test_web.py`; for recommendation behavior, update `tests/test_query_engine.py` or `tests/test_main.py`.

Run focused tests first, then `python3 -m pytest -q` before merging.

## Commit & Pull Request Guidelines

Recent history uses concise imperative commits with prefixes such as `feat:`, `fix:`, and `refactor:`. Keep each commit scoped to one logical change.

Pull requests should include a short summary, validation commands with results, migration or cache notes when relevant, and screenshots for visible web UI changes.

## Security & Configuration Tips

Do not commit secrets, personal provider exports, or machine-local paths. Keep local overrides in `config.local.yaml` or environment variables. Use `config.local.example.yaml` as the public reference for configuration shape.
