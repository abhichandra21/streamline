# Test Plan: Apple TV Ingestion + Uniform Provider Zip Validation

Branch: `feat/apple-tv-ingestion`

## Automated Tests

### 1. Apple TV Ingestion (`recommender/ingestion/apple_tv.py`)

| # | Test | Status |
|---|------|--------|
| 1a | Nested zip extraction — outer zip containing `Apple_Media_Services.zip` containing `Video Play Activity.csv` | TODO |
| 1b | Zip validation — rejects non-`.zip` files (e.g. `.csv`) -> `ValueError` | TODO |
| 1c | Zip validation — rejects missing files -> `FileNotFoundError` | TODO |
| 1d | Zip validation — rejects corrupt zips -> `ValueError` | DONE (`test_apple_tv_parse_reports_invalid_zip`) |
| 1e | CSV missing inside zip — valid zip but no `Video Play Activity.csv` -> `ValueError` | TODO |
| 1f | Classification — TV episodes (has season/episode) vs movies (no season marker) via `_classify()` | TODO |
| 1g | Title building — correct format for TV (`Show: Season: E3: Title`) and movies via `_build_title()` | TODO |
| 1h | Dedup — multiple stop events for same (timestamp, series, ep) keeps highest duration | TODO |
| 1i | Filtering — skips `Action Type != stop`, skip subtypes (FeaturedPromo, Preview, etc.), < 5min watches | TODO |
| 1j | Timestamp parsing — handles `UTC Start Time`, falls back to `UTC End Time` | TODO |

### 2. Netflix/Prime Provider Input Support (`netflix.py`, `prime.py`)

| # | Test | Status |
|---|------|--------|
| 2a | Netflix zip-only contract — non-`.zip` input rejected by public `parse(path)` | TODO |
| 2b | Netflix zip ingestion — `.zip` containing `ViewingActivity.csv` | TODO |
| 2c | Netflix bad zip — corrupt `.zip` -> `ValueError` | TODO |
| 2d | Netflix missing CSV in zip — valid zip, no `ViewingActivity.csv` -> `ValueError` | TODO |
| 2e | Netflix missing file -> `FileNotFoundError` | TODO |
| 2f | Netflix clean extraction per parse — bad zip after prior success does not reuse stale extracted CSV | TODO |
| 2g | Prime zip-only contract — non-`.zip` input rejected by public `parse(path)` | TODO |
| 2h | Prime zip ingestion — `.zip` containing `Viewing History.csv` | TODO |
| 2i | Prime bad zip / missing CSV / missing file — same error cases as Netflix | TODO |
| 2j | Prime clean extraction per parse — bad zip after prior success does not reuse stale extracted CSV | TODO |

### 3. Config: `config.local.yaml` Override System (`config.py`)

| # | Test | Status |
|---|------|--------|
| 3a | `_merge_dicts` recursive merge — nested dict keys merge, scalars override | DONE (`test_merge_dicts_applies_local_overrides_recursively`) |
| 3b | `apple_tv` key exists in `PLATFORM_PATHS` and resolves correctly | TODO |
| 3c | `_resolve_platform_path` with `default=None` — returns `None` when key is omitted | TODO |
| 3d | Real loader path — `config.local.yaml` overrides `config.yaml` when both files are present | TODO |

### 4. Setup: `--ingest-only` Mode (`setup.py`)

| # | Test | Status |
|---|------|--------|
| 4a | No providers configured -> exit 1 with "No providers configured" message | DONE (`test_run_ingest_only_exits_if_no_provider_zips_are_configured`) |
| 4b | Empty but valid export -> passes (exit 0), prints "0 events" | DONE (`test_run_ingest_only_accepts_empty_provider_export`) |
| 4c | Mixed: one provider fails, one succeeds -> strict mode exit 1 | TODO |
| 4d | All providers valid with events -> prints summary with TV/movie counts | TODO |
| 4e | Full `run_setup()` with zero total events across all sources -> exit 1 with explicit "No watch events found" message | TODO |

### 5. Runtime Graceful Degradation (`main.py`, `web.py`)

| # | Test | Status |
|---|------|--------|
| 5a | CLI: configured provider missing at runtime -> warning, continues with empty events | DONE (`test_load_context_skips_invalid_configured_provider`) |
| 5b | Web: configured provider missing at runtime -> warning logged, continues | DONE (`test_web_build_context_skips_invalid_configured_provider`) |
| 5c | Ordinary recommendation flow still works after setup when provider zips are removed | TODO |

### Existing Tests to Verify Still Pass

- `tests/ingestion/test_netflix.py`
- All existing `tests/test_main.py` tests


## Manual Tests

### 6. E2E with Real Data

| # | Test | Steps | Expected |
|---|------|-------|----------|
| 6a | Ingest-only with real Apple TV zip | `./recommend setup --ingest-only` with `apple_tv` path set to real export zip | Prints event count, TV/movie breakdown, exits 0 |
| 6b | Ingest-only with Netflix zip (not CSV) | `./recommend setup --ingest-only` with `netflix` path set to a `.zip` export | Prints event count, exits 0 |
| 6c | Full setup with Apple TV configured | `./recommend setup` with `apple_tv` path set | Watch index includes Apple TV titles, enrichment runs, profile rebuilt |
| 6d | Runtime with missing apple_tv zip | Set `apple_tv` to a non-existent zip path, run `./recommend "something"` | Warning printed, recommendations still work from cached index/profile |
| 6e | Web UI with Apple TV configured | `./recommend-web start`, open http://localhost:5050 | Dashboard loads and derived artifacts built from Apple TV-backed setup remain usable |
| 6f | Web UI settings page messaging | Open settings | Page says shared settings save to `config.yaml` and local watch-history paths belong in `config.local.yaml` |
| 6g | `config.local.yaml` override | Create `config.local.yaml` with `platform_paths.apple_tv: path/to/zip`, run `./recommend setup --ingest-only` | Local override used, shared `config.yaml` unchanged |
| 6h | Ordinary CLI query after removing provider zips | Run `./recommend setup`, move configured provider zips away, then run `./recommend "something"` | Warning printed, ordinary recommendations still work from cached watch index/profile |
