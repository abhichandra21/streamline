# Design: the Find surface

Specification, revision 3.
This document is the source of truth for the Find surface. Where the code and this document disagree, the code is wrong. Revision history is at the end.
Written to be read without prior context; a reviewer needs only this file and the source it cites.

## 1. Intent contract

**Goal.** Give Streamline a way to look up and browse the whole TMDB catalogue, with every result annotated by what the local library already knows about it.
The value is not the search box; TMDB, IMDb and JustWatch all search better than we will.
The value is that none of them know what you have watched, saved, rated, or are following.

**In scope.**

- A page at `/find` with two distinct modes: text lookup and faceted browse.
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
- Existing routes stay the owners of their writes. `/find` does not gain its own save, rating or archive-add logic.
- `get_watch_providers()` keeps its current flatrate-only meaning, because `query_engine` depends on it meaning "on my subscription". Wider availability data goes through a new path. See section 6.
- `search_by_filters()` keeps its current hydrating behaviour, because the recommend pipeline depends on it. See section 6.

**Constraints and accepted risks.**

- TMDB's API does not permit combining free text with facets. Verified in section 4.
- TMDB has no theatrical showtime data. Availability means streaming, rental or purchase only.
- **The home server exposes this UI on the LAN with authentication off.** Verified on the running host, not inferred:
  `STREAMLINE_HOST=0.0.0.0` and `STREAMLINE_PORT=5050` in `~/streamline/.env`; `STREAMLINE_PASSWORD` unset, so `_check_auth` (`web.py:610`) returns `None` for every request; `ss -ltnp` confirms `LISTEN 0.0.0.0:5050`.
  Repository defaults differ and are not what runs: `recommend-web:14-15` defaults to `127.0.0.1:5051`, and Basic auth is available but disabled by the missing password.
  `/find` adds no new auth surface, but it does add an endpoint that spends TMDB quota and is reachable by anyone on the LAN. Accepted, not remediated here.
- Availability failures render as blank rather than as an error. Accepted; see section 7 for the one exception.
- The availability cache grows one file per `(content_type, region, tmdb_id)`. Bounded by query volume, not catalogue size. Accepted.

**What would count as unauthorized scope expansion.**

- Adding an LLM call anywhere in this path.
- Ranking by taste profile, or reusing `query_engine`'s ranking.
- Writing a new save, rating, or archive-add path instead of calling the existing routes.
- Changing `get_watch_providers()`'s return shape or its flatrate meaning, or `search_by_filters()`'s behaviour.
- Building facets beyond the list in section 5, on the grounds that TMDB exposes 39 of them.
- Fixing the identity-matching inconsistency in section 9 as part of this work without a separate decision.
- Repairing the stale committed systemd unit noted in section 9.

## 2. What already exists

This is mostly assembly. The delta is a page, a route, and three new client methods.

| Capability | Where | Fit |
|---|---|---|
| Text search across both content types, deduped by `(content_type, tmdb_id)`, ranked, with per-type failure flags | `tmdb_client.get_disambiguation_candidates()` | Direct fit. Truncates to 5 in three places, so it needs a limit that reaches all three. |
| A result row carrying id, type, title, year, poster, overview, original title, original language, vote average, vote count | `tmdb_client.DisambiguationCandidate` | Everything a search row needs. No detail fetch required. |
| Discover with genre, origin country, original language, year range | `tmdb_client.search_by_filters()` | **Not** a fit. See section 6. |
| Cached flatrate provider names | `tmdb_client.get_watch_providers()` | Fit for the "on my subscription" meaning only. Not wide enough for this page. See section 6. |
| Watched check by typed TMDB identity, falling back to normalized title | `watch_index.is_watched()` | Direct fit. |
| Watchlist, ratings, manual archive, dismissed | `user_state.UserStateIndex` | Fit, with two caveats: it does not read `show_tracking` (section 5), and the identity caveat in section 9. |
| Follow and ignore state | `show_tracking`, via `user_store.list_show_tracking()` | Must be loaded once and indexed by id. Not part of `UserStateIndex`. |
| An annotated search-results picker UI | `templates/_archive_disambiguate.html` | Precedent for layout and row actions. |
| Save to watchlist, add to archive | `POST /watchlist/save`, `POST /archive/add` | Reused unchanged. |

## 3. The two reframes this design rests on

**Annotate, do not hide.**
Hiding by default is lossy and untrustworthy.
If a search returns nothing, the user cannot distinguish "TMDB does not have it" from "my filters excluded it" from "I watched it in 2024."
That breaks the product philosophy's requirement that a user can always understand what happened and recover.

So: every row shows its state, and hiding is a control that names its own effect and reveals the hidden rows when used.
It defaults to on. Section 5 defines exactly what it does to paging.

**Two modes, not one box.**
TMDB cannot answer "British crime drama with 'bay' in the title."
A single input pretending to accept both text and facets produces queries the user cannot reason about and the backend cannot honestly serve.
TMDB's own UI separates Search from Discover for this reason.

## 4. The API constraint, verified

Checked against TMDB's published reference, not from memory.

| Endpoint | Free text | Facets |
|---|---|---|
| `/3/search/movie` | `query`, required | None. Accepts only `query`, `include_adult`, `language`, `primary_release_year`, `page`, `region`, `year`. No genre, rating, vote count, or sort. |
| `/3/discover/movie` | None | 39 parameters, including `with_genres`, `vote_average.gte`/`.lte`, `vote_count.gte`/`.lte`, `sort_by`, `release_date.gte`/`.lte`, `with_original_language`, `with_origin_country`, `with_watch_providers`, `with_watch_monetization_types`, `watch_region`. |

The codebase records this constraint independently.
`tmdb_client._score_candidate` carries the comment: "TMDB's /search endpoint has no with_original_language filter (that's discover-only and discover has no free-text query)", and compensates with a ranking bonus.

Two further verified facts that shape the design:

- **Attribution is mandatory.** TMDB: *"In order to use this data you must attribute the source of the data as JustWatch."* And: *"If we find any usage not complying with these terms we will revoke access to the API."*
- **The rate limit is about 40 requests per second**: *"They sit somewhere in the 40 requests per second range."*

## 5. The design

### Text mode

Input: a query string.

There is **no content-type control**. The backing method searches both types and always will, so a control implying otherwise would be a lie. Films and TV appear together, labelled.

Backend: `get_disambiguation_candidates(query, hint, hints=None, limit=5)`.
The default stays 5 so the manual-add picker is unchanged; `/find` passes 20.
The limit must reach all three existing truncation points — `tmdb_client.py:209` and `:218` per endpoint, and `:267` after the merge and rank. A limit applied only at the last of these still discards rows two steps earlier.
No detail hydration: TMDB search rows already carry poster, year, overview and vote data.

An empty query renders the page and issues no request.

### Browse mode

Input: the facets below. Backend: `discover_page()`, section 6.

| Facet | TMDB parameter | Why it earns its place |
|---|---|---|
| Content type | endpoint choice | Already how every surface is keyed. |
| Genre | `with_genres` | Already mapped in `TV_GENRE_IDS` / `MOVIE_GENRE_IDS`. |
| Year range | `first_air_date`/`primary_release_date` `.gte`/`.lte` | Already built in `search_by_filters`. |
| Original language, origin country | `with_original_language`, `with_origin_country` | Already built. Load-bearing for this library's taste. |
| Minimum rating and vote count | `vote_average.gte`, `vote_count.gte` | Hardcoded today to `vote_average.desc` and `vote_count.gte=100`. Becomes a control. |
| Sort | `sort_by` | Required once the user can page. Without it, paging is meaningless. |
| Available on my platforms | `with_watch_providers` + `watch_region` | Deliberately **not** constrained to `flatrate`. See below. |

Everything else TMDB offers is out of scope. Thirty-nine parameters is not a target.

`sort_by` reaches TMDB, so it is not taken on trust from the query string. It is validated against the offered list and falls back to the first entry when unrecognised.

Paging is bounded by the `total_pages` TMDB reports. No link is offered past it.

### Availability: what it means here

The requirement is "what is out there, and where it is showing; rental is fine, unknown is fine."
So availability is **not** narrowed to subscriptions. `with_watch_monetization_types` is left unset, which is what makes rental and purchase results appear, and each row labels the terms it found:

| Label | TMDB `watch/providers` key |
|---|---|
| Stream | `flatrate` |
| Free | `free` |
| With ads | `ads` |
| Rent | `rent` |
| Buy | `buy` |

A popular title can be on twenty services. Naming them all is a wall of text that buries the fact the user wanted, so each label shows at most three providers and counts the remainder.

**Theatrical is not available.** TMDB's watch-providers data is streaming and transactional only. The nearest thing, `release_dates`, tells you a film *had* a theatrical release and when, not that it is playing near you. A label that never populates is worse than no label, so there is none.

Availability is off unless requested, because it costs one lookup per row on a cold cache.

### Annotation, both modes

Each row is annotated from state loaded **once per request** — one `UserStateIndex.load()`, the in-memory `WatchIndex`, and one `list_show_tracking()` indexed by TMDB id. No per-row database or network I/O.

| State | Source | Row shows |
|---|---|---|
| Watched | `watch_index.is_watched()` or `is_manually_watched()` | `Watched`, plus rating if present |
| In watchlist | `is_in_watchlist()` | `In watchlist` |
| Following | `show_tracking.state == "following"`, TV only | `Following from S<n>` |
| Ignored | `show_tracking.state == "ignored"`, TV only | `Ignored` |
| No tracking decision | absent from `show_tracking`, TV only | nothing |
| Not watched | — | a watchlist action and an archive action |

`Ignored` and "no decision" are different states and must render differently. Collapsing them throws away the distinction the tracker exists to keep.

**There is no Follow action on these rows.** `POST /shows/follow-title` requires the title to be a watched TV entry in the archive and returns 404 otherwise, and most search results are not in the archive. Following belongs on the title page, where that precondition is checkable and already checked. Each row links there, so the path is one click away.

**Two row actions, not one.** "I want to see this" and "I have seen this" are different claims, so they are separate controls posting to `POST /watchlist/save` and `POST /archive/add` respectively. Both are offered on any row that is not already watched. Neither route changes.

### Hiding and paging

TMDB pages server-side; watched state is applied locally afterwards. These cannot be reconciled without holding TMDB's ordering across requests and reconstructing pages, which is a subsystem for a cosmetic gain.

**Filter within the page, and say so.** The control reads `N of M hidden`, and the page states that pages are not backfilled. A page can legitimately come back nearly empty; that is information about the library, not a bug, and the count makes it legible.

The count renders even when it is zero. A control that disappears at zero makes its own presence a signal, which has to be learned rather than read.

A page whose every row is hidden says so in those terms. It must never say "no matches" — that conflation of hiding with absence is the lossy failure this design exists to prevent.

Local over-fetching and page reconstruction are rejected, not deferred.

Every control that changes one aspect of the view — hiding, availability, paging — carries the full facet set, so toggling one never silently discards the filters the user set.

## 6. Three new client methods, and why not to reuse the existing ones

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

`DiscoverPage` carries rows plus `page`, `total_pages`, `total_results`, and a `failed` flag.
Rows are `DiscoverRow`, deliberately not `TmdbMetadata`: hydrating one would reintroduce the per-row request this method exists to avoid. It carries the same fields a search candidate does, so the annotation code reads either without knowing which it has.

`search_by_filters` is left untouched. The recommend pipeline depends on its hydrating behaviour and this change must not reach into that path.

### `get_availability()` rather than widening `get_watch_providers()`

`get_watch_providers()` returns flatrate names only, and `query_engine.py:811-819` filters `rec.streaming_providers` against the configured platform list to mean *on my subscription*. Widening its return would silently change which recommendations survive that filter.

Two further reasons it cannot be edited in place:

- **Cache schema.** Existing entries are `{"providers": ["Netflix", ...]}` — flatrate names, no monetization types. Widening the shape in place would make every cached file read as "no rental data" rather than "not fetched yet", which is a silent wrong answer.
- **Strict reader.** `query_engine` is the only consumer and reads the list by name membership.

So:

```
get_availability(tmdb_id, content_type, region, cache_dir) -> dict
```

returning `{stream, free, ads, rent, buy, link, unknown}`, cached under a path of its own — `config.AVAILABILITY_CACHE_DIR` — leaving the existing `providers/` cache and its method untouched.

### `get_provider_options()` for the provider facet

`with_watch_providers` takes numeric TMDB provider ids, while the rest of this app speaks provider names, and no map between them exists in the repository.

```
get_provider_options(content_type, region, cache_dir, limit=24) -> list[dict]
```

fetches the region's provider list once, caches it, and orders it by TMDB's own `display_priority` to decide which providers are prominent enough to offer. Failure returns an empty list, which renders as no facet rather than as an error.

A hardcoded name-to-id map is rejected: it would be a second authority for data TMDB already publishes, and it would drift.

Because the facet is populated from TMDB rather than from `streaming_platforms`, it does not require that setting to be filled in. `streaming_platforms` continues to govern the recommend pipeline's platform filter only.

## 7. Ordering, boundaries and cost

**Knowledge timing.** Every input arrives in the request. Nothing is read before validation. There is no mutation on the read path: the only writes are the existing watchlist and archive routes, on explicit user action. A cold cache is a miss, never an error.

**API calls per interaction.**

| Interaction | Calls | Note |
|---|---|---|
| Text search | 2 | One per content type. |
| Text search with availability | 2 + up to 20 | Cached per title after first sight. Requested, not automatic. |
| Browse page | 1 | No hydration. The provider filter is a parameter, not a loop. |
| Browse page with availability | 1 + up to 20 | Same cache. |
| First browse page in a region | +1 | Provider options, cached thereafter. |

TMDB's limit is around 40 requests per second and `TmdbRateLimitError` already exists in the client. No interaction above approaches it.

**Failure paths.**

- A search or discover error must render as an error, not as zero results. `get_disambiguation_candidates` distinguishes these with `hinted_type_failed` and `alternate_type_failed`, and `_archive_disambiguate.html` already renders that distinction. `discover_page` must carry the same flag. Silently showing "no matches" on a network failure is the most likely defect in this feature and the one to test hardest.
- Availability failures render as blank, matching `get_watch_providers()`'s existing behaviour. A missing label is not a wrong answer.
- **One exception.** `get_watch_providers()` catches every exception including `TmdbRateLimitError` and returns `[]` (`tmdb_client.py:841-843`), so a 20-row batch that hits a 429 would fire 19 more requests into a rate-limited API. `get_availability()` must let `TmdbRateLimitError` reach the caller, and the caller must stop the batch and say so. This protects the API key, not the display.

**Resource bounds.** One `UserStateIndex.load()`, one `WatchIndex` read, one `list_show_tracking()` per request. No background job. One new cache directory. Growth as described in section 1.

## 8. Operational wiring

| Path | Requirement |
|---|---|
| Nav | A `/find` entry in `base.html`, desktop rail and mobile, with an `on_find` flag matching the existing pattern. |
| Attribution | Visible JustWatch credit wherever availability appears, and only there. Required by TMDB's terms. |
| Config | `watch_region` already exists and is used. `AVAILABILITY_CACHE_DIR` is new. `streaming_platforms` is not read by this surface. |
| Help | `/help` states that Find is a lookup tool, does not use the taste profile, and costs no tokens; what hiding does to paging; and that cinema listings are unavailable. Without this the two surfaces are indistinguishable to a user. |
| Settings | No change. |
| CLI | No change. `/find` is web-only by design; the CLI's job is querying the recommender. |
| Logs | Query string and facet set at debug level, consistent with existing TMDB logging. |

## 9. Discovered work, classified

**Required for correctness, inside this feature.**

- `discover_page()` reports request failure separately from zero results.
- The hide control states its count and its page-local scope, including at zero.
- `get_availability()` uses a cache path of its own so existing flatrate entries are never misread.
- `get_availability()` lets a rate limit stop the batch.
- Follow state loaded once and indexed by id; `ignored` rendered distinctly from "no decision".
- `sort_by` validated before it reaches TMDB.

**Open question, needs a decision before it is acted on.** `UserStateIndex._match_tmdb_first` matches on `meta.tmdb_id` alone:

```python
if meta.tmdb_id is not None and meta.tmdb_id in tmdb_set:
    return True
```

while `watch_index.is_watched` matches on `(content_type, tmdb_id)`, and `get_disambiguation_candidates` documents the same typed-identity requirement and dedupes by it.
TMDB ids are a separate namespace per content type, so a film and a TV show can share an integer id and `is_in_watchlist` can return a false positive across types.

The inconsistency is internal to `UserStateIndex`: its ratings maps *are* typed, with a comment saying so — "The TMDB key is typed so a movie and TV show sharing a numeric id don't collide" — while its watchlist, archive and dismissed sets are not.

Every existing surface is keyed to one content type at a time, which is why this has never been visible. `/find` shows films and TV in one list, so it is the first surface where the collision can be seen.
Pre-existing defect in shared code. It belongs in its own change with its own tests, and is deliberately not absorbed here.

**Deferred hardening.** No authentication on the LAN-exposed UI. Pre-existing, recorded in section 1.

**Unrelated, explicitly not doing.**

- `search_by_filters`'s hardcoded sort and vote floor could be parameterised for the recommend pipeline too. That path is not being touched.
- The committed `streamline-web.service` runs gunicorn on `127.0.0.1:5050`; the deployed unit runs `python3 -m recommender.web` on `0.0.0.0:5050`. The repository's unit file is stale relative to the server. Real, unrelated, needs its own change.

## 10. Implementation plan

Each slice is independently reviewable and leaves the app working. Proofs are the focused tests for that slice.

1. **`limit` on `get_disambiguation_candidates`.** Default 5, reaching all three truncation points. Proof: the picker's behaviour is unchanged; a higher limit returns more than five rows, and no endpoint truncates at five.
2. **`/find` text mode, annotated, no hiding yet.** Route, template, both row actions on their existing routes; state loaded once including tracking. Proof: every annotation state renders, `ignored` distinct from "no decision"; a TMDB failure renders an error and not "no matches".
3. **The hide control.** Proof: the count is correct including zero, states its page-local scope, reveals on use, and a fully hidden page does not claim absence.
4. **`get_availability()` and JustWatch attribution.** Own cache path, monetization labels capped at three, batch stops on rate limit. Proof: labels map correctly; existing flatrate entries are never misread; a rate limit stops the batch; attribution renders only with availability.
5. **`discover_page()`.** Proof: parameters map correctly; TMDB order preserved; failure distinguishable from empty; exactly one request and no hydration.
6. **Browse mode.** Proof: each facet reaches the right TMDB parameter; an unoffered sort is rejected; paging bounded by `total_pages`.
7. **The provider facet.** Proof: options fetched once and cached, ordered by `display_priority`; selection reaches `with_watch_providers`.

Slices 1 to 3 are the useful minimum. Stop there and the feature is worth having.

## 11. Verification

How the code is to be checked against this document.

- Every slice: the existing suite green, plus that slice's focused tests.
- Every numbered invariant in section 12 has at least one test asserting it.
- Driven in the running app against the real cache and live TMDB, not only in tests: both modes, every annotation state, the provider facet, paging bounds, availability labelling and attribution, and both row actions with their writes confirmed in `data/streamline.db` and then undone.
- Explicitly tested, because they are the likeliest defects: a TMDB error rendering as an empty result set, and a widened availability cache misreading existing flatrate entries.
- Cross-contract review against current source before implementation. Green tests do not substitute for it.

## 12. Conformance criteria

The normative summary. These are the statements the implementation must satisfy and a reviewer should attempt to falsify.

### Surfaces

| Path | Required behaviour |
|---|---|
| `GET /find` | Text lookup. Empty `q` renders the page and issues no request. |
| `GET /find?mode=browse` | Faceted browse: `type`, `genre`, `year_from`, `year_to`, `language`, `country`, `min_rating`, `min_votes`, `sort`, repeated `provider`, `page`. |
| `watched=show` | Reveals watched rows; absent means hidden. Both modes. |
| `where=1` | Adds availability; absent means off. Both modes. |

No new write routes. Row actions post to `POST /watchlist/save` and `POST /archive/add`, both unchanged.

### Client methods

| Method | Required contract |
|---|---|
| `get_disambiguation_candidates(..., limit=5)` | Default 5 leaves the manual-add picker unchanged. The limit reaches all three truncation points. |
| `discover_page(...)` | Exactly one request. No hydration. TMDB's order preserved. Reports `total_pages`. `failed` distinguishes a broken request from an empty catalogue. |
| `get_availability(...)` | All five monetization buckets. Its own cache path. `TmdbRateLimitError` reaches the caller; other failures return empty buckets with `unknown=True`. |
| `get_provider_options(...)` | Region's providers ordered by `display_priority`, cached; `[]` on failure. |
| `get_watch_providers(...)` | Unchanged: flatrate-only, still what `query_engine` filters against. |
| `search_by_filters(...)` | Unchanged: still the recommender's hydrating candidate generator. |

### Invariants

1. A TMDB failure never renders as an empty result set, in either mode.
2. A fully hidden page never says "no matches"; it says everything on it is already in the library.
3. The hide control states its count even at zero.
4. Hiding is page-local and the page says so. Pages are not backfilled.
5. Availability is off unless requested, and a rate limit stops the batch rather than finishing it.
6. Each availability label names at most three providers and counts the rest.
7. JustWatch is credited whenever availability is shown, and only then.
8. `sort` never reaches TMDB unvalidated.
9. Local state is read once per request, never once per row.
10. Every view control carries the full facet set, so a toggle never silently drops a filter.
11. `Ignored` and "no tracking decision" render differently.
12. No LLM call and no taste-profile read anywhere in this path.

### Deliberately absent

- Theatrical showtimes. TMDB has no such data.
- A Follow action on Find rows. It would 404; section 5.
- A content-type control in text mode. The backend always searches both.
- Local over-fetching to backfill hidden rows. Rejected, not deferred.
- Any fix to the `UserStateIndex` identity inconsistency, or to the stale systemd unit. Section 9.

## Revision history

**Revision 1.** Initial proposal.

**Revision 2**, after an external source-and-contract review. All eight of its findings identified something real:

| Finding | Resolution |
|---|---|
| `with_watch_monetization_types` omitted, so "streaming" could include rentals | Confirmed as an undefined monetization scope, but the proposed fix was inverted. Rentals and purchases are wanted, so availability is widened rather than constrained, with per-row labels. Theatrical documented as unobtainable. |
| JustWatch attribution missing | Confirmed. Added to scope, wiring and conformance. |
| Hide-watched paging semantics undefined | Confirmed. Defined as page-local filtering with a stated count; over-fetching explicitly rejected. |
| Content-type restriction had no backend contract | Confirmed. The control is dropped rather than specified. |
| Provider failures silently reported as no availability | Masking accepted, with one exception: the batch stops on `TmdbRateLimitError`. Rate-limit figure corrected from 50 to about 40 per second. |
| State-loading boundary omitted show tracking | Confirmed. Tracking loaded once and indexed; `ignored` and "no decision" render distinctly. |
| Search-limit default contradicted itself | Confirmed, and the review was incomplete: there are three truncation points, not two. |
| Deployment-risk statement conflicted with the repository | The review's repository facts were right and revision 1 failed to cite the runtime. The risk statement itself was correct and stands, now verified against the live host. This also surfaced the stale committed systemd unit, logged in section 9. |

**Revision 3.** Three further design decisions, folded into the sections above rather than appended: no Follow action on Find rows, because the route it would call requires an archive entry; availability labels capped at three providers with a count; `sort_by` validated against the offered list before reaching TMDB. Also specifies the two row actions as distinct claims, adds `get_provider_options` as the third new method, and adds section 12 as the normative summary to verify against.
