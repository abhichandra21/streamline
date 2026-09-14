"""Find page collector: unwatched titles for a small set of criteria, sorted as asked, in batches.

Deterministic and LLM-free. Reads TMDB Discover pages in the requested sort order, drops
anything already watched (imported history or manual archive), and returns one
batch plus a cursor that says where to resume. The only extra TMDB read is the
US now-playing list, used for a display-only "In theaters" badge on Movies.
"""

import logging
import re
from dataclasses import dataclass
from datetime import date, datetime
from zoneinfo import ZoneInfo

from dateutil.relativedelta import relativedelta

from recommender.tmdb_client import CatalogTitle, TmdbClient, TmdbRateLimitError

log = logging.getLogger("recommender.catalog_finder")

LOCAL_TZ = ZoneInfo("America/Chicago")
DEFAULT_PERIOD = "6m"
DEFAULT_LIMIT = 10

# Cursor = "<tmdb_page>.<row offset within that page>", both non-negative, page >= 1.
_CURSOR_RE = re.compile(r"^([1-9]\d*)\.(\d+)$")

# (key, label). Keys are what the form sends; keep them stable.
PERIOD_OPTIONS = (
    ("30d", "30 days"), ("3m", "3 months"), ("6m", "6 months"),
    ("1y", "1 year"), ("2y", "2 years"), ("5y", "5 years"),
    ("10y", "10 years"),
)

# (key, label). "newest" resolves to the content type's date field in discover_sort_by().
SORT_OPTIONS = (
    ("rating", "Rating"), ("newest", "Newest"),
    ("popular", "Most popular"), ("votes", "Most voted"),
)
DEFAULT_SORT = "rating"

_SORT_BY: dict[str, str] = {
    "rating": "vote_average.desc",
    "popular": "popularity.desc",
    "votes": "vote_count.desc",
}

# (key, label, TMDB vote_average.gte or None for no floor).
RATING_OPTIONS = (
    ("any", "Any", None), ("6", "6+", 6.0), ("7", "7+", 7.0),
    ("7.5", "7.5+", 7.5), ("8", "8+", 8.0),
)

_PERIOD_DELTAS: dict[str, relativedelta] = {
    "30d": relativedelta(days=30),
    "3m": relativedelta(months=3),
    "6m": relativedelta(months=6),
    "1y": relativedelta(years=1),
    "2y": relativedelta(years=2),
    "5y": relativedelta(years=5),
    "10y": relativedelta(years=10),
}


@dataclass(frozen=True)
class FindCriteria:
    content_type: str = "movie"
    period: str = DEFAULT_PERIOD
    genre: str | None = None
    keyword: str | None = None
    min_rating: float | None = None
    sort: str = DEFAULT_SORT


@dataclass(frozen=True)
class FindRow:
    title: CatalogTitle
    # True/False only when the full US now-playing list was read. None means
    # the list was unavailable (or not applicable, for TV): unknown, not "no".
    in_theaters: bool | None = None


@dataclass(frozen=True)
class FindResults:
    criteria: FindCriteria
    release_start: date
    release_end: date
    rows: tuple[FindRow, ...] = ()
    keyword_name: str | None = None
    # The user asked for a keyword TMDB does not know. No Discover call was made.
    keyword_missing: bool = False
    # TMDB rate limited the now-playing read. Rows are complete; cinema status is unknown.
    now_playing_rate_limited: bool = False
    pages_read: int = 0
    catalog_exhausted: bool = False
    # Where the next batch starts, or None when TMDB has nothing more.
    next_cursor: str | None = None


def chicago_today() -> date:
    return datetime.now(LOCAL_TZ).date()


def release_window(period: str, today: date | None = None) -> tuple[date, date]:
    """Inclusive (start, end) release-date window ending today for a period key."""
    end = today or chicago_today()
    delta = _PERIOD_DELTAS.get(period)
    if delta is None:
        log.debug("Unknown Find period %r, using %s", period, DEFAULT_PERIOD)
        delta = _PERIOD_DELTAS[DEFAULT_PERIOD]
    return end - delta, end


def parse_cursor(cursor: str | None) -> tuple[int, int]:
    """Return (tmdb_page, offset). Anything malformed restarts from the top."""
    match = _CURSOR_RE.match(cursor or "")
    if not match:
        return 1, 0
    return int(match.group(1)), int(match.group(2))


def discover_sort_by(sort: str, content_type: str) -> str:
    """Map a sort key to TMDB's sort_by. Unknown keys fall back to rating."""
    if sort == "newest":
        return "first_air_date.desc" if content_type == "tv" else "primary_release_date.desc"
    return _SORT_BY.get(sort, _SORT_BY[DEFAULT_SORT])


def find_unwatched_titles(
    tmdb: TmdbClient,
    watch_index,
    user_state,
    criteria: FindCriteria,
    cache_dir: str,
    region: str = "US",
    today: date | None = None,
    limit: int = DEFAULT_LIMIT,
    cursor: str | None = None,
    exclude: frozenset[int] = frozenset(),
) -> FindResults:
    """One batch of unwatched titles.

    exclude holds TMDB ids already shown in earlier batches. TMDB may reorder
    between requests, so the cursor alone cannot stop a title being served
    twice; the caller carries the recently shown ids forward (see web.py's
    FIND_SHOWN_MAX for the bound on that window).
    """
    release_start, release_end = release_window(criteria.period, today)

    keyword_id: int | None = None
    keyword_name: str | None = None
    if criteria.keyword:
        match = tmdb.search_keyword_exact(criteria.keyword)
        if match is None:
            # Never silently broaden to "no keyword": that would answer a different question.
            return FindResults(criteria=criteria, release_start=release_start,
                               release_end=release_end, keyword_missing=True)
        keyword_id, keyword_name = match

    sort_by = discover_sort_by(criteria.sort, criteria.content_type)
    titles: list[CatalogTitle] = []
    seen: set[int] = set(exclude)
    page, offset = parse_cursor(cursor)
    pages_read = 0
    next_cursor: str | None = None
    while len(titles) < limit:
        result = tmdb.discover_catalog_page(
            criteria.content_type, release_start, release_end,
            genre=criteria.genre, keyword_id=keyword_id, min_rating=criteria.min_rating,
            page=page, region=region, sort_by=sort_by,
        )
        pages_read += 1
        if not result.rows or result.page > result.total_pages:
            break
        rows = result.rows
        index = offset
        while index < len(rows) and len(titles) < limit:
            row = rows[index]
            index += 1
            if row.tmdb_id in seen:
                continue
            seen.add(row.tmdb_id)
            if watch_index.is_watched(row) or user_state.is_manually_watched(row):
                continue
            titles.append(row)
        offset = 0
        if index < len(rows):
            next_cursor = f"{result.page}.{index}"
            break
        if result.page >= result.total_pages:
            break
        page += 1
        if len(titles) >= limit:
            next_cursor = f"{page}.0"
            break
    exhausted = next_cursor is None

    rows, rate_limited = _annotate_cinema(tmdb, titles, criteria.content_type, region, cache_dir)
    return FindResults(
        criteria=criteria,
        release_start=release_start,
        release_end=release_end,
        rows=tuple(rows),
        keyword_name=keyword_name,
        now_playing_rate_limited=rate_limited,
        pages_read=pages_read,
        catalog_exhausted=exhausted,
        next_cursor=next_cursor,
    )


def _annotate_cinema(
    tmdb: TmdbClient,
    titles: list[CatalogTitle],
    content_type: str,
    region: str,
    cache_dir: str,
) -> tuple[list[FindRow], bool]:
    """Attach cinema status to the batch rows (Movies only, one cached read).

    On a TMDB rate limit, keep every row with cinema status unknown and return
    the flag so the page can say so.
    """
    now_playing: set[int] | None = None
    rate_limited = False
    if content_type == "movie" and titles:
        try:
            now_playing = tmdb.get_now_playing_ids(region, cache_dir)
        except TmdbRateLimitError as exc:
            log.warning("TMDB rate limited the now-playing list (retry after %s s)", exc.retry_after_seconds)
            rate_limited = True
    rows = [
        FindRow(title=title, in_theaters=None if now_playing is None else title.tmdb_id in now_playing)
        for title in titles
    ]
    return rows, rate_limited
