# Code Review: master (full codebase)

Verdict: Needs fixes

The codebase has a solid two-phase architecture but contains several correctness bugs that silently degrade recommendation quality. The scoring pipeline always uses fallback runtimes instead of fetched TMDB data, several parsed query intent fields are never acted upon, and the LLM integration layer has no schema validation or timeouts. A hardcoded API key in git history needs rotation.

## Merge Gate Issues

#### 1. TMDB API key committed to git history
**Location:** `config.py:3`
**Severity / Confidence:** 9 / 1.0
**Flagged by:** gpt-5.4, claude-opus-4.6
**Trigger scenario:** Anyone with repo access obtains the key from current code or git history.
**Why it matters:** Credential leak. Key can be abused, rate-limited, or revoked -- breaking the tool for the owner. Permanent in git history even after removal from source.
**Evidence:** `TMDB_API_KEY = os.environ.get("TMDB_API_KEY", "55a3ba0fd07d2cd884871536b48dd04f")` (already removed from working copy but still in committed history).
**Recommended fix:** Rotate the TMDB API key on tmdb.org. Consider `git filter-branch` or BFG to scrub history if repo will be public.

#### 2. setup.py never passes TMDB metadata to compute_scores -- completion rates always wrong
**Location:** `recommender/setup.py:78`
**Severity / Confidence:** 7 / 1.0
**Flagged by:** claude-opus-4.6 (verified by synthesizer)
**Trigger scenario:** Every run of `python -m recommender.setup`. The `metadata` dict (with real runtimes from TMDB) is populated on lines 56-63 but `compute_scores(events, {}, ...)` always passes empty `{}`.
**Why it matters:** Completion rates (50% of the score weight) always use fallback runtimes (45min TV, 90min movie) instead of actual runtimes. A 23-minute anime episode watched fully gets 0.51 instead of 1.0. This systematically distorts the taste profile.
**Evidence:** `setup.py:78 scores = compute_scores(events, {}, config.RECENCY_HALF_LIFE_DAYS)` -- `metadata` is in scope but not passed. `signals.py:34-38` uses metadata runtime when available.
**Recommended fix:** `scores = compute_scores(events, metadata, config.RECENCY_HALF_LIFE_DAYS)`.

#### 3. Parsed QueryIntent fields are never used in execution
**Location:** `recommender/query_engine.py:174-211`
**Severity / Confidence:** 7 / 0.96
**Flagged by:** gpt-5.4 (verified by synthesizer)
**Trigger scenario:** User queries involving runtime limits, rewatch requests, family/watchlist intents, mood descriptors, or similar-to references. These are parsed by Claude and stored in QueryIntent but `ask()` ignores them.
**Why it matters:** The query appears understood (parsing succeeds) but results are wrong. A "short movie" query returns 3-hour epics. An "include rewatches" query still excludes watched titles. Expensive to debug because the parsing layer says the constraint was understood.
**Evidence:** `parse_intent()` asks Claude for `max_runtime_minutes`, `unwatched_only`, `mood_descriptors`, `similar_to`, `special_intent` (watchlist/family). `ask()` only branches on `special_intent == 'abandoned'` and unconditionally applies watch-index exclusion.
**Recommended fix:** Either wire each parsed field into execution or shrink QueryIntent to fields actually honored, with clear documentation of what's supported.

#### 4. LLM JSON response parsing crashes on schema deviations
**Location:** `recommender/query_engine.py:75-76, 114, 233`
**Severity / Confidence:** 7 / 0.9
**Flagged by:** claude-opus-4.6 (verified by synthesizer -- matches original Claude review finding)
**Trigger scenario:** Claude returns extra keys, missing fields, wrong types, or unexpected formatting. `QueryIntent(**data)` raises opaque TypeError.
**Why it matters:** Critical path of every query. LLM outputs are non-deterministic. An unhandled crash kills the interactive session with a stack trace.
**Evidence:** `query_engine.py:75-76` -- no try/except, no key filtering, no type coercion. Same pattern at lines 114 and 233.
**Recommended fix:** Wrap in try/except, filter to known keys, provide defaults for missing fields, cast types.

#### 5. Watch index ignores content_type in normalized-title fallback
**Location:** `recommender/watch_index.py:23-27`
**Severity / Confidence:** 6 / 0.94
**Flagged by:** gpt-5.4
**Trigger scenario:** A watched TV show and a recommended movie share the same normalized title (e.g., "Fargo", "The Witcher"). The movie is incorrectly filtered out.
**Why it matters:** Silently removes legitimate candidates from the recommendation pool for cross-media title collisions.
**Evidence:** `is_watched()` falls back to `_normalize(candidate.title) in self.normalized_titles` without considering `candidate.content_type`.
**Recommended fix:** Store normalized titles with content_type as `(normalized_title, content_type)` tuples.

#### 6. Manual ingestion deduplicates across TV and movies
**Location:** `recommender/ingestion/manual.py:16-45`
**Severity / Confidence:** 5 / 0.95
**Flagged by:** gpt-5.4
**Trigger scenario:** Same title appears in both `tv.csv` and `movies.csv` (e.g., "Fargo"). The movie entry is silently dropped.
**Why it matters:** Deletes part of watch history, propagates into taste profile with no warning.
**Evidence:** `parse()` uses one shared `seen` set across both file loops.
**Recommended fix:** Deduplicate by `(content_type, normalized_title)` or use separate `seen` sets per file.

## Maintainability

#### 7. Fallback enrichments permanently cached -- no retry mechanism
**Location:** `recommender/enricher.py:57-61`
**Severity / Confidence:** 6 / 1.0
**Flagged by:** claude-opus-4.6
**Why it matters:** Transient API failures produce low-quality fallback text written to the same cache path as real enrichments. Subsequent runs always hit cache. No way to identify or retry degraded entries. Silently degrades taste profile quality.
**Recommended fix:** Don't cache fallback descriptions, or write them to a separate directory/suffix, or add a `--refresh-enrichments` flag.

#### 8. Dead code: setup.py line 49 computes scores that are never read
**Location:** `recommender/setup.py:49`
**Severity / Confidence:** 4 / 1.0
**Flagged by:** claude-opus-4.6
**Why it matters:** Wastes CPU on every `--refresh-data` run and misleads readers into thinking scores feed the metadata fetch loop.
**Recommended fix:** Delete line 49.

## Readability & API Clarity

#### 9. Recency decay constant doesn't match half-life semantics
**Location:** `recommender/signals.py:60`
**Severity / Confidence:** 5 / 1.0
**Flagged by:** claude-opus-4.6
**Why it matters:** `RECENCY_HALF_LIFE_DAYS = 90` implies 50% at 90 days, but `exp(-days/90)` gives 36.8%. Operators tuning this parameter get different results than the name implies.
**Recommended fix:** Use `0.5 ** (days / half_life)` or rename to `RECENCY_DECAY_CONSTANT_DAYS`.

## Test Coverage & Safeguards

#### 10. Prime Video parser has zero test coverage
**Location:** `recommender/ingestion/prime.py:1-76`
**Severity / Confidence:** 5 / 1.0
**Flagged by:** claude-opus-4.6
**Why it matters:** `_classify` uses fragile heuristics (split on last hyphen). Multi-hyphen titles like "Self-Made" get truncated to "Made". Without tests, regressions are invisible. Netflix has 8 tests; Prime has zero.
**Recommended fix:** Add `tests/ingestion/test_prime.py` with fixture CSV covering standard episodes, movies, multi-hyphen titles.

## Operational Concerns

#### 11. search_by_filters has no page limit -- can loop indefinitely
**Location:** `recommender/tmdb_client.py:170-195`
**Severity / Confidence:** 6 / 0.8
**Flagged by:** claude-opus-4.6
**Why it matters:** If TMDB returns duplicate-heavy results, the loop keeps fetching pages without progress. Could make hundreds of API calls. CLI appears hung.
**Recommended fix:** Add `max_pages` limit (10-20) as loop guard.

#### 12. All Sonnet API calls lack timeout
**Location:** `recommender/query_engine.py:50,95,150,220` and `recommender/taste_profile_builder.py:24`
**Severity / Confidence:** 5 / 0.9
**Flagged by:** claude-opus-4.6
**Why it matters:** Enricher correctly sets `timeout=30.0`. Query engine and taste profile builder omit it. A slow API call blocks the interactive REPL indefinitely.
**Recommended fix:** Add `timeout=30.0` to all `client.messages.create` calls.

#### 13. Silent exception swallowing in TMDB fetch loops
**Location:** `recommender/tmdb_client.py:191,224`
**Severity / Confidence:** 5 / 0.9
**Flagged by:** claude-opus-4.6
**Why it matters:** Bare `except Exception: continue` makes systematic failures (bad key, rate limit) indistinguishable from "no matching titles." No logging anywhere in the module.
**Recommended fix:** Log exceptions at WARNING level. Track consecutive failures and raise if all candidates fail.

#### 14. Query mode re-parses raw export files on every invocation
**Location:** `recommender/main.py:16-23`
**Severity / Confidence:** 6 / 0.95
**Flagged by:** gpt-5.4
**Why it matters:** After setup produces persisted artifacts, `load_context()` still requires the original Netflix/Prime/manual CSV files. Breaks if raw files are moved/deleted after setup.
**Recommended fix:** Persist normalized events during setup and load from disk. Only lazy-load raw files for the abandoned-query path.

## Scope
- Target: master branch (all 30 commits, 34 files, 4508 additions)
- Reviewed files: 34
- Skipped generated/vendored/lockfiles: 0
- Diagnostics: `master-review-diagnostics.md`
