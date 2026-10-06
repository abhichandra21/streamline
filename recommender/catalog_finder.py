"""Find page collector: unwatched titles for a small set of criteria, sorted as asked, in batches.

Deterministic and LLM-free. Reads TMDB Discover pages in the requested sort order, drops
anything already watched (imported history or manual archive), and returns one
batch plus a cursor that says where to resume. The extra TMDB reads are the US
now-playing list, for a display-only "In theaters" badge on Movies, and one
cached IMDb ID lookup per shown title, for its display-only IMDb rating.
"""

import json
import logging
import random
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, replace
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from dateutil.relativedelta import relativedelta

from recommender import imdb_ratings, language_catalog
from recommender.imdb_ratings import ImdbRating
from recommender.tmdb_client import MOVIE_GENRE_IDS, TV_GENRE_IDS, CatalogTitle, TmdbClient, TmdbRateLimitError

log = logging.getLogger("recommender.catalog_finder")

LOCAL_TZ = ZoneInfo("America/Chicago")
DEFAULT_PERIOD = "6m"
DEFAULT_LIMIT = 10
# With a language selected, TMDB's votes are too thin to trust, so a title needs
# this many IMDb votes instead. Kept low so new releases appear within days;
# the rating sort below, not this floor, keeps thinly rated titles off the top.
LANGUAGE_MIN_IMDB_VOTES = 500
# The rating sort ranks by IMDb's Top 250 weighted rating: each title's rating
# is blended with the list average as if it had this many extra votes at that
# average. A 9.9 from 5,000 votes then ranks below an 8.3 from 250,000.
LANGUAGE_RATING_PRIOR_VOTES = 10000

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
    ("rating", "TMDB rating"), ("newest", "Newest"),
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
    # An original-language code from language_catalog.LANGUAGES, or None for TMDB Discover.
    language: str | None = None


@dataclass(frozen=True)
class FindRow:
    title: CatalogTitle
    # True/False only when the full US now-playing list was read. None means
    # the list was unavailable (or not applicable, for TV): unknown, not "no".
    in_theaters: bool | None = None
    # Display only: TMDB's order and rating floor decide which rows appear.
    imdb: ImdbRating | None = None


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
    # Language mode only: the saved list has not been built yet. No rows.
    language_pending: bool = False
    # Language mode only: a keyword was given, which the saved list cannot answer. No rows.
    keyword_unsupported: bool = False
    # Language mode only: when the saved list was built.
    list_built_at: datetime | None = None


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
    imdb_db_path: str | None = None,
) -> FindResults:
    """One batch of titles that are not watched, saved, or marked not interested.

    exclude holds TMDB ids already shown in earlier batches. TMDB may reorder
    between requests, so the cursor alone cannot stop a title being served
    twice; the caller carries the recently shown ids forward (see web.py's
    FIND_SHOWN_MAX for the bound on that window).
    """
    release_start, release_end = release_window(criteria.period, today)
    if criteria.language:
        return _find_in_language_list(
            tmdb, watch_index, user_state, criteria, cache_dir, region,
            release_start, release_end, limit, cursor, exclude,
        )

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
            if (watch_index.is_watched(row) or user_state.is_manually_watched(row)
                    or user_state.is_in_watchlist(row) or user_state.is_dismissed(row)):
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
    if imdb_db_path:
        rows = _annotate_imdb(tmdb, rows, imdb_db_path)
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


# The most-voted titles barely change, so a week-old page is fine.
CLASSICS_TTL_SECONDS = 7 * 86400
# TMDB returns 20 per page. One step is about 200 movies and 100 shows; going
# deeper adds a step at a time, up to the top 1000 movies and 500 shows.
CLASSICS_PAGES_PER_STEP = {"movie": 10, "tv": 5}
CLASSICS_MAX_STEPS = 5
CLASSICS_SET_SIZE = 20
CLASSICS_SAMPLE_POOL = 100


def _classics_page(tmdb: TmdbClient, cache_dir: str, content_type: str, page: int,
                   end: date) -> list[CatalogTitle]:
    """One most-voted Discover page, cached for a week. A failed page raises and saves nothing."""
    path = Path(cache_dir) / "classics" / f"{content_type}_{page}.json"
    cached = TmdbClient._read_fresh_cache(path, CLASSICS_TTL_SECONDS)
    if cached is not None:
        return [CatalogTitle(**row) for row in cached.get("titles", [])]
    result = tmdb.discover_catalog_page(
        content_type, date(1900, 1, 1), end, page=page, sort_by="vote_count.desc")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"titles": [asdict(t) for t in result.rows]}))
    return list(result.rows)


def famous_titles(tmdb: TmdbClient, cache_dir: str, steps: int = 1,
                  today: date | None = None) -> list[CatalogTitle]:
    """The most-voted movies and shows on TMDB, most voted first, `steps` steps deep."""
    end = today or chicago_today()
    titles: list[CatalogTitle] = []
    for content_type, per_step in CLASSICS_PAGES_PER_STEP.items():
        for page in range(1, per_step * steps + 1):
            titles.extend(_classics_page(tmdb, cache_dir, content_type, page, end))
    titles.sort(key=lambda t: t.vote_count, reverse=True)
    return titles


def next_classics_set(
    tmdb: TmdbClient,
    watch_index,
    user_state,
    cache_dir: str,
    shown: set[tuple[str, int]],
    size: int = CLASSICS_SET_SIZE,
    today: date | None = None,
    rng: random.Random | None = None,
) -> list[CatalogTitle]:
    """A random set of famous titles that are unanswered and not shown before.

    Answered means in the watch history, the manual archive, or the dismissed
    list. The set is sampled from the most-voted few of what is left, and the
    list is read deeper only when too few remain. Empty means all are used up.
    """
    rng = rng or random
    for steps in range(1, CLASSICS_MAX_STEPS + 1):
        # Pages are cached at different times, so a title can sit on two of them.
        unique = {(t.content_type, t.tmdb_id): t for t in famous_titles(tmdb, cache_dir, steps, today)}
        remaining = [
            t for key, t in unique.items()
            if key not in shown
            and not (watch_index.is_watched(t) or user_state.is_manually_watched(t)
                     or user_state.is_dismissed(t))
        ]
        if len(remaining) >= size or steps == CLASSICS_MAX_STEPS:
            return _mixed_sample(remaining, size, rng)
    return []


def _mixed_sample(titles: list[CatalogTitle], size: int, rng) -> list[CatalogTitle]:
    """Half movies, half shows, each drawn from the most-voted of its kind.

    Shows get far fewer votes than films, so one combined vote-count pool
    would be all films. A short side is filled from the other.
    """
    pools = {ct: [t for t in titles if t.content_type == ct][:CLASSICS_SAMPLE_POOL // 2]
             for ct in ("movie", "tv")}
    picked = rng.sample(pools["tv"], min(size // 2, len(pools["tv"])))
    picked += rng.sample(pools["movie"], min(size - len(picked), len(pools["movie"])))
    if len(picked) < size:
        rest = [t for t in pools["tv"] if t not in picked]
        picked += rng.sample(rest, min(size - len(picked), len(rest)))
    rng.shuffle(picked)
    return picked


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


# Matches the main pipeline's fan-out for TMDB lookups (query_engine).
_IMDB_ID_CONCURRENCY = 8


def _annotate_imdb(tmdb: TmdbClient, rows: list[FindRow], db_path: str) -> list[FindRow]:
    """Attach IMDb ratings to the batch rows. Best-effort: any failure leaves TMDB only."""
    if not rows or imdb_ratings.refreshed_at(db_path) is None:
        return rows

    def _imdb_id(row: FindRow) -> str | None:
        try:
            return tmdb.get_imdb_id(row.title.tmdb_id, row.title.content_type)
        except Exception as exc:   # rate limit or network: show the row without IMDb
            log.debug("IMDb ID lookup failed for %s: %s", row.title.tmdb_id, type(exc).__name__)
            return None

    with ThreadPoolExecutor(max_workers=min(_IMDB_ID_CONCURRENCY, len(rows))) as pool:
        imdb_ids = list(pool.map(_imdb_id, rows))
    found = imdb_ratings.lookup(db_path, [i for i in imdb_ids if i])
    return [
        replace(row, imdb=found.get(imdb_id)) if imdb_id else row
        for row, imdb_id in zip(rows, imdb_ids)
    ]


def weighted_rating(rating: float, votes: int, average: float,
                    prior_votes: int = LANGUAGE_RATING_PRIOR_VOTES) -> float:
    return (votes * rating + prior_votes * average) / (votes + prior_votes)


def _language_sort_key(sort: str, average: float):
    if sort == "newest":
        return lambda t: (t.release_date or date.min, t.imdb.votes)
    if sort == "popular":
        return lambda t: (t.popularity, t.imdb.votes)
    if sort == "votes":
        return lambda t: (t.imdb.votes, t.imdb.rating)
    return lambda t: (weighted_rating(t.imdb.rating, t.imdb.votes, average), t.imdb.votes)


def _find_in_language_list(
    tmdb: TmdbClient,
    watch_index,
    user_state,
    criteria: FindCriteria,
    cache_dir: str,
    region: str,
    release_start: date,
    release_end: date,
    limit: int,
    cursor: str | None,
    exclude: frozenset[int],
) -> FindResults:
    """One batch from the saved language list, filtered and sorted locally.

    The cursor's offset is a position in the list filtered by the criteria
    alone; its page part is unused. Watched, watchlisted or dismissed titles
    and exclude (repeats after a rebuild) are skipped while walking from the
    offset, not filtered out first, so marking a title watched or saving it
    between clicks cannot shift the offset past titles not yet shown. TMDB mode counts raw Discover rows the same way.
    """
    base = dict(criteria=criteria, release_start=release_start, release_end=release_end)
    if criteria.keyword:
        # Never silently drop the keyword: that would answer a different question.
        return FindResults(**base, keyword_unsupported=True)
    saved = language_catalog.load(cache_dir, criteria.language)
    if saved is None:
        return FindResults(**base, language_pending=True)

    # The average comes from every listed title of this type, not just the
    # filtered ones, so a title's place does not shift when a filter changes.
    eligible = [t for t in saved.titles
                if t.title.content_type == criteria.content_type and t.imdb.votes >= LANGUAGE_MIN_IMDB_VOTES]
    average = sum(t.imdb.rating for t in eligible) / len(eligible) if eligible else 0.0
    genre_map = TV_GENRE_IDS if criteria.content_type == "tv" else MOVIE_GENRE_IDS
    genre_id = genre_map.get(criteria.genre) if criteria.genre else None
    matches = [
        t for t in saved.titles
        if t.title.content_type == criteria.content_type
        and t.release_date is not None and release_start <= t.release_date <= release_end
        and (genre_id is None or genre_id in t.genre_ids)
        and t.imdb.votes >= LANGUAGE_MIN_IMDB_VOTES
        and (criteria.min_rating is None or t.imdb.rating >= criteria.min_rating)
    ]
    matches.sort(key=_language_sort_key(criteria.sort, average), reverse=True)

    _page, offset = parse_cursor(cursor)
    batch: list[language_catalog.LanguageTitle] = []
    position = offset
    while position < len(matches) and len(batch) < limit:
        candidate = matches[position]
        position += 1
        if (candidate.title.tmdb_id not in exclude
                and not watch_index.is_watched(candidate.title)
                and not user_state.is_manually_watched(candidate.title)
                and not user_state.is_in_watchlist(candidate.title)
                and not user_state.is_dismissed(candidate.title)):
            batch.append(candidate)
    next_cursor = f"1.{position}" if position < len(matches) else None
    rows, rate_limited = _annotate_cinema(
        tmdb, [t.title for t in batch], criteria.content_type, region, cache_dir)
    rows = [replace(row, imdb=t.imdb) for row, t in zip(rows, batch)]
    return FindResults(
        **base,
        rows=tuple(rows),
        now_playing_rate_limited=rate_limited,
        catalog_exhausted=next_cursor is None,
        next_cursor=next_cursor,
        list_built_at=saved.built_at,
    )
