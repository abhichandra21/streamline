---
description: Run the quality eval on the real library and grade search and profile results
argument-hint: "[run | grade | both] (default both)"
---

Grade how good Streamline's search results and taste profile are on the owner's real library.
The owner does not supply expectations. You judge from the library itself, with the fixed scorecard below.
All AI calls in the eval go through the Claude subscription (the `claude` CLI), never the API.

Argument: `$ARGUMENTS`. `run` only runs the eval, `grade` only grades the latest run, anything else does both.

## 1. Searches file

If `data/evals/searches.txt` is missing, write it once, then leave it unchanged so runs stay comparable.
Read `recommender/cache/taste_profile.txt` and write 15 searches, one per line, phrased the way a person types them:
- 6 that sit inside the owner's main tastes, at least one per large cluster
- 3 moods or occasions ("something light for a weeknight")
- 3 with constraints: a platform, a runtime, a decade, or movie vs show
- 2 "like <a title the owner rated more like this>"
- 1 outside the profile, to see whether results still respect the request

Tell the owner the file was created and where. Do not change it later unless they ask.

## 2. Run

`venv/bin/python -m tools.eval_quality` writes `data/evals/runs/<timestamp>/`. To grade a fix worktree, add `--code <worktree path>`; the data still comes from this checkout.
The profile rebuild takes several minutes, so run it in the background and wait for it to finish.
If it fails, show the last lines of the log in the run folder and stop.

## 3. Grade

Grade the newest run folder. Files: `searches.json` (results and code checks), `library.json` (ratings, follows, every watched title, data checks), `profile_new.txt`, `profile_current.txt`, `searches.log` and `profile_build.log` (TMDB matching warnings), `meta.json`.

Ground every judgment in the library. Name the titles that support it. Claude ranked these results, so be strict about mistakes Claude tends to make: plausible but wrong facts in explanations, generic taste claims, and picking famous titles over fitting ones.

**Trust failures come first.** The goal is an app the owner can trust, and one bad pick is what they remember, so judge every result on its own. A trust failure is any one of:
- A result that does not fit the request, or breaks a stated constraint (type, decade, platform, length). Count each such result, wherever it sits in the list. Filler the explanation admits is off-request counts.
- A search that returns nothing, or fewer results than it could have found.
- An already-watched title.
- A result the owner said they don't want: a "less" title, something close to one, or something the profile note rules out.
- An explanation with a wrong fact, or a taste claim `library.json` does not support (for example calling a watched-but-unrated title one they "like").
- In the profile: a "less" title described as a taste, or a taste the history does not support.

List each failure with the search or profile section, the title, and the reason. A clean run has zero. Scores below are secondary.

**Each search, 1 to 5 per line:**
- Relevance: does each result fit what was asked, including any constraint?
- Taste fit: do the results lean toward what the owner rates "more" and away from "less"?
- Explanations: are the stated facts true, and does the taste link hold up against `library.json`?
- Freshness: any already-watched titles (from the code checks, or your own look at `watched`)?
- Misses: name up to 3 titles that clearly should have appeared. Drop any that are in `watched`.

**Profile, 1 to 5 per line, for `profile_new.txt`:**
- Coverage: are the big groups in the watch history and the "more" ratings all there, roughly in size order?
- Accuracy: any claim the history does not support?
- Dislikes: are the "less" ratings reflected, and nothing pushed toward them?
- Specificity: would this profile describe this owner and not a generic viewer?

Then compare it with `profile_current.txt` and give one verdict, better, same or worse, with the two biggest differences.

**Data:** count the titles with no TMDB match, and the rejected or low-confidence matches in the logs. Name any that look like real titles matched wrongly.

## 4. Record

Write `grades.md` in the run folder: the trust failures first, then one table for searches, one for the profile, and the data notes.
Append one JSON line to `data/evals/scores.jsonl`: `{"run": ..., "commit": ..., "dirty": ..., "settings": ..., "trust_failures": [{"where": ..., "title": ..., "reason": ...}], "search_avg": ..., "profile_avg": ..., "per_search": {query: avg}, "profile": {line: score}, "vs_current_profile": ...}`.
If an earlier line exists, compare with it: which trust failures are gone, which are new.

Reply to the owner in a few plain lines: the trust failure count and what they were, what changed since the last run, then the two averages. Point to `grades.md` for detail.
