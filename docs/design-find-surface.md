# Design: the Find surface

Status: **implemented**, revision 3. All seven slices are built and on `feature/find-surface`; see PR #87.
Revision 2 revised the proposal after an external source-and-contract review (section 13). Revision 3 corrects this document to describe what was actually built, so the contract a reviewer reads is the contract the code keeps (section 14).
Written for external review. A reviewer needs no prior context beyond this file and the source it cites.

## 1. Intent contract

**Goal.** Give Streamline a way to look up and browse the whole TMDB catalogue, with every result annotated by what the local library already knows about it.
The value is not the search box; TMDB, IMDb and JustWatch all search better than we will.
The value is that none of them know what you have watched, saved, rated, or are following.

**In scope.**

- A new page at `/find` with two distinct modes: text lookup and faceted browse.
- Local-state annotation on every result row: watched, in watchlist, rated, followed, ignored.
- A hide-watched control that is visible, reversible, and honest about what it does to paging.
- Availability annotation showing where a title is playable and on what terms, subscription or rental or purchase.
- JustWatch attribution wherever availability data appears. This is a TMDB terms-of-use requirement, not a courtesy.
- Row actions that reuse the existing `POST /watchlist/save` and `POST /archive/add` routes.

**Out of scope.**

- Any LLM call. This surface is deterministic and costs no tokens.
- Taste-profile ranking. Results are ordered by TMDB, not by the profile.
- Changes to the recommend pipeline, the Mood Match wizard, or the taste profile.
- Person, company, keyword and collection search; trending; "similar to" browsing.
- Infinite scroll. Paging is explicit.
- Theatrical showtimes. TMDB cannot supply them; see section 5.

**Decisions that must not change.**

- `/find` is a lookup and browse tool. It is not a second recommender.
  The existing division holds: recommend and the wizard answer "decide for me"; `/find` answers "I know roughly what I want."
  If `/find` starts ranking by taste it becomes a worse version of a good feature and competes with the core model, which `CLAUDE.md` explicitly warns against.
- Local state is the reason this page exists, so annotation is never optional. Hiding is.
- Existing routes stay the owners of their writes. `/find` does not gain its own save or archive-add logic.
- `get_watch_providers()` keeps its current flatrate-only meaning, because `query_engine` depends on it meaning "on my subscription". Wider availability data goes through a new path. See section 6.

**Constraints and accepted risks.**

- TMDB's API does not permit combining free text with facets. Verified in section 4.
- TMDB has no theatrical showtime data. Availability means streaming, rental or purchase only.
- **The home server exposes this UI on the LAN with authentication off.** Verified on the running host, not inferred:
  `STREAMLINE_HOST=0.0.0.0` and `STREAMLINE_PORT=5050` in `~/streamline/.env`; `STREAMLINE_PASSWORD` unset, so `_check_auth` (`web.py:610`) returns `None` for every request; `ss -ltnp` confirms `LISTEN 0.0.0.0:5050`.
  Repository defaults differ and are not what runs: `recommend-web:14-15` defaults to `127.0.0.1:5051`, and Basic auth is available but disabled by the missing password.
  `/find` adds no new auth surface, but it does add an endpoint that spends TMDB quota and is reachable by anyone on the LAN. Accepted, not remediated here.
- Provider failures render as blank availability rather than as an error. Accepted by the owner; see section 7 for the one exception.
- The provider cache grows one file per `(content_type, region, tmdb_id)`. Bounded by query volume, not catalogue size. Accepted.

**What would count as unauthorized scope expansion.**

- Adding an LLM call anywhere in this path.
- Ranking by taste profile, or reusing `query_engine`'s ranking.
- Writing a new save, rating, or archive-add path instead of calling the existing routes.
- Changing `get_watch_providers()`'s return shape or its flatrate meaning.
- Building facets beyond the list in section 5, on the grounds that TMDB exposes 39 of them.
- Fixing the identity-matching inconsistency in section 9 as part of this work without a separate decision.
- Repairing the stale committed systemd unit noted in section 9.

## 2. What already exists

This is mostly assembly. The delta is a page, a route, one new thin Discover method, and one new availability method.

| Capability | Where | Fit |
|---|---|---|
| Text search across both content types, deduped by `(content_type, tmdb_id)`, ranked, with per-type failure flags | `tmdb_client.get_disambiguation_candidates()` | Direct fit. Truncated to 5 in three places; a `limit` argument now reaches all three. |
| A result row carrying id, type, title, year, poster, overview, original title, original language, vote average, vote count | `tmdb_client.DisambiguationCandidate` | Everything a search row needs. No detail fetch required. |
| Discover with genre, origin country, original language, year range | `tmdb_client.search_by_filters()` | **Not** a fit as-is. See section 6. |
| Cached flatrate provider names | `tmdb_client.get_watch_providers()` | Fit for the "on my subscription" meaning only. Not wide enough for this page. See section 6. |
| Watched check by typed TMDB identity, falling back to normalized title | `watch_index.is_watched()` | Direct fit. |
| Watchlist, ratings, manual archive, dismissed | `user_state.UserStateIndex` | Fit, with two caveats: it does not read `show_tracking` (section 6), and the identity caveat in section 9. |
| Follow and ignore state | `show_tracking` table, via `user_store.list_show_tracking()` | Needs loading once and indexing by id. Not part of `UserStateIndex`. |
| An annotated search-results picker UI | `templates/_archive_disambiguate.html` | Precedent for layout and row actions. |
| Save to watchlist, add to archive | `POST /watchlist/save`, `POST /archive/add` | Reused unchanged. |

## 3. The two reframes this design rests on

**Annotate, do not hide.**
Hiding by default is lossy and untrustworthy.
If a search returns nothing, the user cannot distinguish "TMDB does not have it" from "my filters excluded it" from "I watched it in 2024."
That breaks the product philosophy's requirement that a user can always understand what happened and recover.

So: every row shows its state, and hiding is a chip that names its own effect and reveals the hidden rows when clicked.
The chip defaults to **on**. Section 5 defines exactly what it does to paging, which is the part revision 1 left undefined.

**Two modes, not one box.**
TMDB cannot answer "British crime drama with 'bay' in the title."
A single input pretending to accept both text and facets produces queries the user cannot reason about and the backend cannot honestly serve.
TMDB's own UI separates Search from Discover for this reason.

## 4. The API constraint, verified

Checked against TMDB's published reference on 2026-09-11, not from memory.

| Endpoint | Free text | Facets |
|---|---|---|
| `/3/search/movie` | `query`, required | None. Accepts only `query`, `include_adult`, `language`, `primary_release_year`, `page`, `region`, `year`. No genre, rating, vote count, or sort. |
| `/3/discover/movie` | None | 39 parameters, including `with_genres`, `vote_average.gte`/`.lte`, `vote_count.gte`/`.lte`, `sort_by`, `release_date.gte`/`.lte`, `with_original_language`, `with_origin_country`, `with_watch_providers`, `with_watch_monetization_types`, `watch_region`. |

The codebase already records this constraint independently.
`tmdb_client._score_candidate` carries the comment: "TMDB's /search endpoint has no with_original_language filter (that's discover-only and discover has no free-text query)", and compensates with a ranking bonus.

Two further verified facts that shape the design:

- **Attribution is mandatory.** TMDB: *"In order to use this data you must attribute the source of the data as JustWatch."* And: *"If we find any usage not complying with these terms we will revoke access to the API."*
- **The rate limit is about 40 requests per second**, not 50: *"They sit somewhere in the 40 requests per second range."*

## 5. The design

### Text mode

Input: a query string. **No content-type control** — the method searches both types and always will, and a UI control implying otherwise would be a lie. Results show movies and TV together, labelled.

Backend: `get_disambiguation_candidates(query, hint, hints=None, limit=5)`.
The default stays 5 so the manual-add picker is unchanged; `/find` passes 20 explicitly.
The limit must reach all three existing truncation points — `tmdb_client.py:209`, `:218` (per endpoint) and `:267` (after the merge and rank).
No detail hydration: TMDB search rows already carry poster, year, overview and vote data.

### Browse mode

Input: the facets below. Backend: a new `discover_page()`, described in section 6.

| Facet | TMDB parameter | Why it earns its place |
|---|---|---|
| Content type | endpoint choice | Already how every surface is keyed. |
| Genre | `with_genres` | Already mapped in `TV_GENRE_IDS` / `MOVIE_GENRE_IDS`. |
| Year range | `first_air_date`/`primary_release_date` `.gte`/`.lte` | Already built in `search_by_filters`. |
| Original language, origin country | `with_original_language`, `with_origin_country` | Already built. Load-bearing for this library's taste. |
| Minimum rating and vote count | `vote_average.gte`, `vote_count.gte` | Currently hardcoded to `vote_average.desc` and `vote_count.gte=100`. Becomes a control. |
| Sort | `sort_by` | Required once the user can page. Without it, paging is meaningless. |
| Available on my platforms | `with_watch_providers` + `watch_region` | Deliberately **not** constrained to `flatrate`. See below. |

Everything else TMDB offers is out of scope. Thirty-nine parameters is not a target.

### Availability: what it means here

The owner's requirement is "I need to know what's out there, and where it's showing; rental is fine, unknown is fine."
So availability is **not** narrowed to subscriptions. `with_watch_monetization_types` is left unset in browse mode, which is what makes rent and buy results appear, and each row labels the terms it found:

| Label | TMDB `watch/providers` key |
|---|---|
| Stream | `flatrate` |
| Free / With ads | `free`, `ads` |
| Rent | `rent` |
| Buy | `buy` |
| (nothing) | no data for this region |

**Theatrical is not available.** TMDB's watch-providers data is streaming and transactional only. The nearest thing, `release_dates`, tells you a film *had* a theatrical release and when — not that it is playing near you. A "Theater" label would never populate, so it is not in the design.

Because `get_watch_providers()` must keep meaning "on my subscription" for `query_engine`, the wider data goes through a new sibling method. See section 6.

### Annotation, both modes

Each row is annotated from state loaded **once per request** — one `UserStateIndex.load()`, the in-memory `WatchIndex`, and one `list_show_tracking()` indexed by TMDB id. No per-row database or network I/O.

| State | Source | Row shows |
|---|---|---|
| Watched | `watch_index.is_watched()` or `is_manually_watched()` | `Watched`, plus rating if present |
| In watchlist | `is_in_watchlist()` | `In watchlist` |
| Following | `show_tracking.state == "following"`, TV only | `Following from S<n>` |
| Ignored | `show_tracking.state == "ignored"`, TV only | `Ignored` |
| No tracking decision | absent from `show_tracking`, TV only | nothing; see below |
| None of the above | — | Save and Add actions |

`Ignored` and "no decision" are different states and must render differently. Collapsing them, as revision 1 did, throws away the distinction the tracker exists to keep.

**No Follow action on this page**, though revision 2 planned one for undecided TV.
Implementation showed it would not work: `POST /shows/follow-title` requires the title to be a watched TV entry in the archive and returns 404 otherwise (`web.py:927`), and most search results are not in the archive.
Following stays on the title page, where that precondition is checkable and already checked.
The row links there, so the path is one click away.

Row actions post to the existing routes. Nothing new writes to the user store.

### Hiding and paging

TMDB pages server-side; watched state is applied locally afterwards. These cannot be reconciled without holding TMDB's ordering across requests and reconstructing pages, which is a subsystem for a cosmetic gain.

**Decision: filter within the page, and say so.** The chip reads `12 of 20 on this page hidden`, and a line states that pages are not backfilled. A page can legitimately come back nearly empty; that is information about your library, not a bug, and the count makes it legible. Local over-fetching and page reconstruction are explicitly rejected, not deferred.

## 6. Two new methods, and why not to reuse the existing ones

### `discover_page()` rather than `search_by_filters()`

Read the body at `tmdb_client.py:675`. It is a candidate generator for the recommend pipeline, not a browser:

- It fetches full details for every uncached result, one API call each, with a 50 ms sleep. Filling `size=50` costs about 50 calls on a cold cache.
- It loops up to `MAX_DISCOVER_PAGES = 20` internally until `size` is reached. The caller cannot request page 2.
- It returns `list(candidates.values())` from a dict, so TMDB's sort order is discarded. There is no total count.

A paged browse UI needs the opposite: one page, TMDB's order preserved, a total so paging can be bounded, no detail hydration, and request failure distinguishable from zero results.

```
discover_page(content_type, filters: dict, page: int = 1,
              sort_by: str = "popularity.desc",
              watch_region: str = "US") -> DiscoverPage
```

returning `DiscoverRow` rows plus `page`, `total_pages`, `total_results`, and a `failed` flag.
`DiscoverRow` deliberately is not a `TmdbMetadata`: hydrating one would reintroduce the per-row request this method exists to avoid. It carries the same fields a search candidate does, so `_find_annotations` reads both without knowing which it has.
`sort_by` is validated by the caller against the module-level `DISCOVER_SORTS` before it reaches TMDB.
`search_by_filters` is left untouched, because the recommend pipeline depends on its hydrating behaviour and this change must not reach into that path.

### `get_availability()` rather than widening `get_watch_providers()`

`get_watch_providers()` returns flatrate names only, and `query_engine.py:811-819` filters `rec.streaming_providers` against the configured platform list to mean *on my subscription*. Widening its return would silently change which recommendations survive that filter.

Two further reasons it cannot be edited in place:

- **Cache schema.** Existing entries are `{"providers": ["Netflix", ...]}` — flatrate names, no monetization types. Widening the shape would make every cached file read as "no rent or buy data" rather than "not fetched yet", which is a silent wrong answer.
- **Strict reader.** `query_engine` is the only consumer and reads the list positionally by name membership.

So: a new

```
get_availability(tmdb_id, content_type, region, cache_dir) -> dict
```

returning `{stream, free, ads, rent, buy, link, unknown}`, cached under a **separate path** — `config.AVAILABILITY_CACHE_DIR`, which is `recommender/cache/availability/` — leaving the existing `providers/` cache and `get_watch_providers()` untouched.

The provider ids that `with_watch_providers` needs come from a third method:

```
get_provider_options(content_type, region, cache_dir, limit=24) -> list[dict]
```

fetched once per region and cached, ordered by TMDB's `display_priority`. See section 10.

## 7. Ordering, boundaries and cost

**Knowledge timing.** Every input arrives in the request. Nothing is read before validation. There is no mutation on the read path: the only writes are the existing save and archive-add routes, on explicit user action. A cold cache is a miss, never an error.

**API calls per interaction.**

| Interaction | Calls | Note |
|---|---|---|
| Text search | 2 | One per content type. |
| Text search with availability | 2 + up to 20 | Cached per title after first sight. Opt-in. |
| Browse page | 1 | No hydration. Provider filter is a parameter, not a loop. |
| Browse page with availability labels | 1 + up to 20 | Same cache. |

TMDB's limit is around 40 requests per second and `TmdbRateLimitError` already exists in the client. No interaction above approaches it.

**Failure paths.**

- A search or discover error must render as an error, not as zero results. `get_disambiguation_candidates` already distinguishes these with `hinted_type_failed` and `alternate_type_failed`, and `_archive_disambiguate.html` already renders that distinction. `discover_page` must carry the same flag. Silently showing "no matches" on a network failure is the most likely defect in this feature and the one to test hardest.
- Availability failures render as blank, matching `get_watch_providers()`'s existing behaviour. Accepted by the owner: a missing label is not a wrong answer.
- **One exception.** `get_watch_providers()` catches every exception including `TmdbRateLimitError` and returns `[]` (`tmdb_client.py:841-843`), so a 20-row batch that hits a 429 fires 19 more requests into a rate-limited API. `get_availability()` must let `TmdbRateLimitError` abort the batch. This protects the API key, not the display.

**Resource bounds.** One `UserStateIndex.load()`, one `WatchIndex` read, one `list_show_tracking()` per request. No background job. One new cache directory. Growth as described in section 1.

## 8. Operational wiring

| Path | Change |
|---|---|
| Nav | A `/find` entry in `base.html`, desktop rail and mobile tabs, with an `on_find` flag matching the existing pattern. |
| Attribution | Visible JustWatch credit wherever availability appears, per TMDB's terms. Required, with a test. |
| Config | `watch_region` already exists and is used. `AVAILABILITY_CACHE_DIR` is new, pointing at `recommender/cache/availability/`. `streaming_platforms` is untouched and still governs the recommender's platform filter only; see section 10. |
| Help | `/help` has a section stating that Find is a lookup tool, does not use the taste profile, and costs no tokens, plus what hiding does to paging and that cinema listings are unavailable. Without it the two surfaces are indistinguishable to a user. |
| Settings | No change. |
| CLI | No change. `/find` is web-only by design; the CLI's job is querying the recommender. |
| Logs | Query string and facet set at debug level, consistent with existing TMDB logging. |

## 9. Newly discovered work, classified

**Required for correctness, inside this feature. All built and tested.**

- `discover_page()` reports request failure separately from zero results.
- The hide chip states its count and its page-local scope, including when zero are hidden.
- `get_availability()` uses a separate cache path so existing flatrate entries are never misread.
- `get_availability()` aborts the batch on `TmdbRateLimitError`.
- Follow state loaded once, indexed by id; `ignored` rendered distinctly from "no decision".

**Open question, needs a decision before implementation.** `UserStateIndex._match_tmdb_first` matches on `meta.tmdb_id` alone:

```python
if meta.tmdb_id is not None and meta.tmdb_id in tmdb_set:
    return True
```

while `watch_index.is_watched` matches on `(content_type, tmdb_id)`, and `get_disambiguation_candidates` documents the same typed-identity requirement and dedupes by it.
TMDB ids are a separate namespace per content type, so a movie and a TV show can share an integer id and `is_in_watchlist` can return a false positive across types.

Note the inconsistency is internal to `UserStateIndex`: its ratings maps *are* typed, with a comment saying so — "The TMDB key is typed so a movie and TV show sharing a numeric id don't collide" — while its watchlist, archive and dismissed sets are not.

Every existing surface is keyed to one content type at a time, which is why this has never been visible. `/find` shows movies and TV in one list, so it is the first surface where the collision can be seen.
Pre-existing defect in shared code. It should be fixed in its own change with its own tests. Deliberately not absorbed here.

**Deferred hardening.** No authentication on the LAN-exposed UI. Pre-existing, recorded in section 1.

**Unrelated, explicitly not doing.**

- `search_by_filters`'s hardcoded sort and vote floor could be parameterised for the recommend pipeline too. That path is not being touched.
- The committed `streamline-web.service` runs gunicorn on `127.0.0.1:5050`; the deployed unit runs `python3 -m recommender.web` on `0.0.0.0:5050`. The repository's unit file is stale relative to the server. Real, unrelated, needs its own change.

## 10. Open questions

None remain open.

Both of revision 2's questions concerned the platform facet, and both dissolved the same way.
`with_watch_providers` needs numeric ids while the rest of the app speaks provider names, so `get_provider_options()` fetches the region's provider list once and caches it, ordered by TMDB's own `display_priority`.
That makes the facet a checklist of real providers, so it no longer depends on `streaming_platforms` being filled in, and the question of declaring subscriptions is moot for this page.
A hardcoded name-to-id map was rejected: it would be a second authority for data TMDB already publishes, and it would drift.

`streaming_platforms` still governs the recommend pipeline's platform filter, which this change does not touch.

The original questions, for the record:

1. **Do you want to declare your subscriptions?**
   `streaming_platforms` is `[]`, so a "my platforms" filter has nothing to filter against and would ship inert. One line of config unlocks it.

2. **Provider IDs.** `with_watch_providers` takes numeric TMDB provider ids; `streaming_platforms` and the provider cache both use names. No map exists in the repo.
   Options: hardcode a small map, or fetch `/watch/providers/{type}?watch_region=US` once and cache it.
   Recommendation: fetch and cache; a hardcoded map is a second authority for the same data and will drift.
   Gates the platform facet only.

Resolved since revision 1: the hide chip defaults to on (section 5); availability is not narrowed to subscriptions (owner); the content-type control is dropped (section 5); the rate-limit abort is in (section 7).

## 11. Implementation slices

Each slice is independently reviewable and leaves the app working. Proofs are the focused tests for that slice.

1. **`limit` on `get_disambiguation_candidates`.** Default 5, propagated through all three truncation points. Proof: existing picker tests unchanged; a new test asserts `limit=20` returns more than 5 rows, and that each endpoint is no longer capped at 5.
2. **`/find` text mode, annotated, no hiding yet.** Route, template, row actions on the existing save and archive routes; state loaded once including tracking. Proof: every annotation state renders, `ignored` distinct from "no decision"; a TMDB failure renders an error and not "no matches".
3. **The hide chip.** Proof: the count is correct including zero, states its page-local scope, reveals on click, and survives paging.
4. **`get_availability()` plus JustWatch attribution.** New cache path, monetization labels, batch abort on rate limit. Proof: labels map correctly; a cold cache does not misread existing flatrate entries; a `TmdbRateLimitError` stops the batch; attribution renders.
5. **`discover_page()` on the client.** Proof: parameters map correctly; TMDB order preserved; failure distinguishable from empty; no detail hydration.
6. **`/find` browse mode with facets 1 to 6.** Proof: each facet reaches the right TMDB parameter; paging bounded by `total_pages`.
7. **The platform facet.** Only after questions 1 and 2 are answered.

Slices 1 to 3 are the useful minimum. Stop there and the feature is worth having.

**All seven are built.** Three decisions the slices did not anticipate:

- No Follow action on `/find` rows; it would have 404'd. Recorded in section 5.
- Availability is capped at three providers per way to watch, with a count for the rest. A popular title can be on twenty services, and naming them all buries the fact you wanted.
- `DISCOVER_SORTS` moved to module level in `tmdb_client`, and the sort value is validated against it before reaching TMDB. As a class attribute it was unreadable in tests that patch `TmdbClient`, and it is data about the endpoint rather than client state. The value arrives in the query string and is forwarded to the API, so it is not taken on trust.

## 12. Verification

- Every slice: the existing suite green, plus that slice's focused tests.
- Before merge: driven in the running app against the real cache, in the manner used for the follow control — render each annotation state, then exercise the actions and confirm the writes land in `data/streamline.db`.
- Explicitly tested, because they are the likeliest defects: a TMDB error rendering as an empty result set, and a widened availability cache misreading existing flatrate entries.
- Cross-contract review against current source before implementation. Green tests do not substitute for it.

## 13. What changed in revision 2

All eight findings from the external review identified something real. Resolutions:

| # | Resolution |
|---|---|
| 1 | Confirmed as an undefined monetization scope, but the proposed fix was inverted. The owner wants rent and buy included, so availability is widened rather than constrained to `flatrate`, with per-row type labels. Theatrical is documented as unobtainable. |
| 2 | Confirmed. JustWatch attribution added to scope, wiring, and slice 4's proof. |
| 3 | Confirmed. Paging is now defined as page-local filtering with a stated count; over-fetching explicitly rejected. |
| 4 | Confirmed. The content-type control is dropped rather than specified. |
| 5 | Failure masking accepted by the owner, with one exception: the batch now aborts on `TmdbRateLimitError`. The rate-limit figure is corrected from 50 to about 40 per second. |
| 6 | Confirmed. Tracking is loaded once and indexed; `ignored` and "no decision" render distinctly. |
| 7 | Confirmed, and the review was incomplete: there are three truncation points, not two. |
| 8 | The review's repository facts were right and revision 1 failed to cite the runtime. The risk statement itself was correct and stands, now verified against the live host. This also surfaced that the committed systemd unit is stale relative to the deployed one, logged as unrelated work. |

## 14. The contract, as built

What a reviewer should hold the code to. Every row was checked against the source on `feature/find-surface`, not against revision 2's intent.

### Surfaces

| Path | Behaviour |
|---|---|
| `GET /find` | Text lookup. `q` required; empty `q` renders the page and issues no request. |
| `GET /find?mode=browse` | Faceted browse. `type`, `genre`, `year_from`, `year_to`, `language`, `country`, `min_rating`, `min_votes`, `sort`, repeated `provider`, `page`. |
| `watched=show` | Reveals watched rows. Absent means hidden. Applies to both modes. |
| `where=1` | Adds availability. Absent means off. Applies to both modes. |

No new write routes. Row actions post to `POST /watchlist/save` and `POST /archive/add`, both unchanged.

### Client methods

| Method | Contract |
|---|---|
| `get_disambiguation_candidates(..., limit=5)` | Default 5 keeps the manual-add picker unchanged. `/find` passes 20. The limit reaches all three former truncation points. |
| `discover_page(...)` | Exactly one request. No hydration. TMDB's order preserved. Reports `total_pages`; `failed` distinguishes a broken request from an empty catalogue. |
| `get_availability(...)` | All five monetization buckets. Own cache path. `TmdbRateLimitError` propagates; other failures return empty buckets with `unknown=True`. |
| `get_provider_options(...)` | Region's providers by `display_priority`, cached; `[]` on failure. |
| `get_watch_providers(...)` | **Unchanged.** Still flatrate-only, still what `query_engine` filters against. |
| `search_by_filters(...)` | **Unchanged.** Still the recommender's hydrating candidate generator. |

### Invariants a reviewer should try to break

1. A TMDB failure never renders as an empty result set, in either mode.
2. A fully hidden page never says "no matches"; it says everything on it is already in the library.
3. The hide chip states its count even at zero.
4. Hiding is page-local and the page says so. Pages are not backfilled.
5. Availability is off unless asked for, and a rate limit stops the batch rather than finishing it.
6. JustWatch is credited whenever availability is shown, and only then.
7. `sort` never reaches TMDB unvalidated.
8. Local state is read once per request, never once per row.
9. Chip, availability and paging links carry the full facet set, so a toggle never silently drops a filter.
10. No LLM call and no taste-profile read anywhere in this path.

### Deliberately absent

- Theatrical showtimes. TMDB has no such data.
- A Follow action on Find rows. It would 404; see section 5.
- A content-type control in text mode. The backend always searches both.
- Local over-fetching to backfill hidden rows. Rejected in section 5, not deferred.
- Any fix to the `UserStateIndex` identity inconsistency, or to the stale systemd unit. Both section 9.

### Test coverage

53 tests across `tests/test_tmdb_client.py` and `tests/test_web.py`, 770 passing overall.
The two hardest-tested paths are the two likeliest to fail silently: failure rendering as emptiness, and a widened availability cache misreading existing flatrate entries.

Verified in the running app against live TMDB, not only in tests: both modes, all annotation states, the provider facet, paging bounds, availability labelling and attribution, and both row actions with their writes confirmed in `data/streamline.db` and then undone.
