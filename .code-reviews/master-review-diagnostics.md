# Review Diagnostics: master (full codebase)

- Review Panel: gpt-5.4, claude-opus-4.6
- Synthesized by: Claude (orchestrator)
- Mode: extended
- Target: master branch (all 30 commits)

## Reviewer Stats

| Model | Wall Clock | Findings | Avg Severity | Skipped (noise) | Signal Rate |
|-------|-----------|----------|-------------|-----------------|-------------|
| gpt-5.4 | ~180s | 5 | 6.6 | 0 | 100% |
| claude-opus-4.6 | ~210s | 11 | 5.9 | 0 | 100% |

## Cross-Model Agreement

| Finding | gpt-5.4 | claude-opus-4.6 | Agreement |
|---------|---------|-----------------|-----------|
| TMDB key in source | Yes (sev 9) | Yes (sev 9) | Both |
| TMDB key guard dead code | No | Yes (sev 7) | Single -- logical consequence of shared finding |
| setup.py empty metadata | No | Yes (sev 7) | Single -- verified by synthesizer |
| Ignored QueryIntent fields | Yes (sev 7) | No | Single -- verified by synthesizer |
| LLM JSON parsing crashes | No | Yes (sev 7) | Single -- verified by synthesizer, matches prior review |
| Watch index content_type | Yes (sev 6) | No | Single |
| Manual cross-type dedup | Yes (sev 5) | No | Single |
| Fallback enrichment caching | No | Yes (sev 6) | Single |
| search_by_filters no page limit | No | Yes (sev 6) | Single |
| Recency decay semantics | No | Yes (sev 5) | Single |
| Dead code line 49 | No | Yes (sev 4) | Single |
| Prime parser no tests | No | Yes (sev 5) | Single |
| LLM calls no timeout | No | Yes (sev 5) | Single |
| TMDB silent exceptions | No | Yes (sev 5) | Single |
| Query mode re-parses exports | Yes (sev 6) | No | Single |

## False Positives (Already Addressed)

| # | Flagged Issue | Model | Why It's Fine |
|---|---------------|-------|---------------|
| 1 | TMDB key hardcoded default | Both | Already removed from working copy config.py during this session. Still in git history -- key rotation needed. |
| 2 | TMDB_API_KEY guard dead code | opus | Consequence of #1 -- already fixed in working copy. Guard now works. |

## Synthesis Notes

- Both models converged on the TMDB credential leak as the top finding.
- GPT-5.4 produced fewer but highly targeted findings focused on correctness gaps in the execution layer (ignored intent fields, cross-type dedup, content-type-blind watch filtering).
- Claude Opus 4.6 produced broader coverage including operability, testability, and maintainability concerns, plus a critical correctness bug (empty metadata in scoring) that GPT missed entirely.
- The setup.py metadata bug (finding #2 in main review) is arguably the highest-impact correctness issue for recommendation quality -- it affects every score computation.
- Three sev 7+ single-model findings were verified by the synthesizer reading the actual code paths rather than cross-model probing, since the claims were straightforward to confirm.

## Investigation Logs

- **gpt-5.4** -- Mapped codebase with find, read commit history, read all core pipeline modules. Attempted to run pytest but failed (no venv in worktree). Traced data flow through offline and online pipelines. Checked intent fields against execution paths. Investigation was focused and efficient.
- **claude-opus-4.6** -- Read all 34 Python files. Traced offline pipeline (setup.py ingest -> TMDB -> enrich -> profile -> index) and online pipeline (ask -> parse_intent -> search_by_filters -> filter -> enrich_batch -> rank). Verified config defaults, error handling, JSON parsing, cache lifecycle, scoring math, and pagination logic. Broader coverage but found the same top-priority issue.

## Per-Model Findings

- gpt-5.4 -- 5 findings (1 merge_gate/security, 2 merge_gate/correctness, 1 extended/operability, 1 merge_gate/correctness)
- claude-opus-4.6 -- 11 findings (1 merge_gate/security, 1 merge_gate/reliability, 1 merge_gate/correctness, 1 merge_gate/reliability, 2 extended/operability, 1 extended/reliability, 1 extended/correctness, 1 extended/maintainability, 1 extended/testability, 1 extended/architecture)
