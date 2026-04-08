# Shortlist: Save Search Results for Later

**Issue:** #10 (reframed from "add to external lists" to "local shortlist with optional IMDb export")

**Motivation:** When a user finds good candidates across multiple searches, they need a local place for those results to accumulate. The shortlist is a holding area for intent-to-investigate, not a judgment or rating. IMDb export is a secondary convenience, not the primary model.

**Philosophy alignment:** The shortlist is local state the user owns. Saving is explicit (per-title button). Export is a separate, observable action (CSV download). No external integrations run automatically.

## Data Model

File: `recommender/cache/shortlist.json`

```json
[
  {
    "title": "Shetland",
    "content_type": "tv",
    "tmdb_id": 67592,
    "added_at": "2026-04-07T14:30:00+00:00",
    "source_query": "good British crime drama"
  }
]
```

- Dedup by `tmdb_id` (preferred) or `(title.lower().strip(), content_type)` when no TMDB ID
- `source_query` is informational only — records which search produced the title
- No ordering beyond insertion order; pages render newest-first
- Independent from feedback system — no interaction with liked/disliked ratings or watch history

## Module: `recommender/shortlist.py`

Pure-function module following the `feedback.py` pattern. All functions take the list as a parameter and mutate/return it; callers handle load/save.

### Functions

- `load(path: str) -> list[dict]` — load from JSON file; returns empty list if file missing
- `save(items: list[dict], path: str) -> None` — write JSON to file, create parent dirs if needed
- `add(items, title, content_type, tmdb_id, source_query) -> None` — append to list with dedup (by tmdb_id if present, else by `title.lower().strip()` + content_type). Sets `added_at` to current UTC time.
- `remove(items, tmdb_id=None, title=None, content_type=None) -> bool` — remove matching entry. Prefer tmdb_id match; fall back to title + content_type. Returns True if an item was removed.
- `contains(items, tmdb_id=None, title=None, content_type=None) -> bool` — check if title is already in the list. Same match logic as remove.
- `export_imdb_csv(items, cache_dir) -> str` — generate CSV string with columns `Position`, `Const`, `Title`, `Type`. Looks up `imdb_id` from TMDB cache files (`{cache_dir}/{content_type}/{tmdb_id}.json`). Titles without a cached `imdb_id` get an empty `Const` field.

### Path Convention

The shortlist path follows the existing cache convention. Added to `config.py` as `SHORTLIST_PATH`, defaulting to `recommender/cache/shortlist.json`. No `config.yaml` entry needed.

## Web Integration

### Routes

| Method | Path | Purpose |
|--------|------|---------|
| `GET` | `/shortlist` | Shortlist page — grid of saved titles with posters |
| `POST` | `/shortlist/add` | HTMX: add title, returns updated button state |
| `DELETE` | `/shortlist/remove` | HTMX: remove title, returns updated button state or removes card |
| `GET` | `/shortlist/export` | Download IMDb CSV file |

All POST/DELETE routes are CSRF-protected (existing middleware handles this).

### Nav Badge

A context processor `_inject_shortlist_count` loads the shortlist and injects `shortlist_count` into all templates. The nav bar shows a count badge next to the "Shortlist" link when `shortlist_count > 0`.

### Result Cards (`_results.html`)

Each recommendation card gets a save/unsave toggle button:

- On render, check `contains()` against the current shortlist to set initial state
- "Save" button: `hx-post="/shortlist/add"` with title, content_type, tmdb_id, source_query as form params. Returns the "Saved" button variant (swap via `hx-swap="outerHTML"`).
- "Saved" button: `hx-delete="/shortlist/remove"` with tmdb_id. Returns the "Save" button variant.

No full page reload for either action.

### Shortlist Page (`shortlist.html`)

- Grid of poster cards matching the watch history visual style
- Each card shows: poster, title, content_type badge, source query, added date
- Each card has a remove button (`hx-delete`, removes the card from DOM)
- "Export to IMDb CSV" button at top, disabled when list is empty
- Empty state message when no items saved

### CSV Export Response

```
Content-Type: text/csv
Content-Disposition: attachment; filename=streamline-shortlist.csv
```

The CSV uses the IMDb list import format:
```csv
Position,Const,Title,Type
1,tt1234567,Shetland,tvSeries
2,,Unknown Title,movie
```

`Type` maps `content_type`: "tv" -> "tvSeries", "movie" -> "movie".

## Testing

New file: `tests/test_shortlist.py`

Unit tests for the module functions:

- **add**: basic add, dedup by tmdb_id, dedup by title+content_type, add without tmdb_id
- **remove**: by tmdb_id, by title+content_type fallback, returns False when not found
- **contains**: positive and negative cases for both match strategies
- **export_imdb_csv**: with valid imdb_ids from cache, with missing imdb_ids, with empty list
- **load**: missing file returns empty list, valid file returns contents

No web route tests. The module is pure functions; the web layer is thin HTMX wiring verified manually.

## Files Changed

| File | Change |
|------|--------|
| `recommender/shortlist.py` | New module |
| `recommender/web.py` | 4 new routes, 1 context processor, shortlist loading in result builder |
| `recommender/templates/_results.html` | Save/unsave toggle button per card |
| `recommender/templates/shortlist.html` | New page template |
| `recommender/templates/base.html` | Nav link with badge |
| `config.py` | `SHORTLIST_PATH` constant |
| `tests/test_shortlist.py` | New test file |

## Out of Scope

- Named/multiple lists — single flat shortlist only
- Feedback integration (liked/disliked on remove) — shortlist is independent
- Letterboxd/Trakt export — IMDb CSV only
- CLI shortlist access — web UI only for now
- Automatic cleanup or expiry of shortlist items
