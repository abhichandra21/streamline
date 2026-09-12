"""Tests for the Find page collector: date windows, watched backfill, availability annotation."""

from datetime import date

import pytest

from recommender import catalog_finder as cf
from recommender.catalog_finder import FindCriteria, FindResults, find_unwatched_titles, release_window
from recommender.tmdb_client import (
    CatalogAvailability,
    CatalogPage,
    CatalogTitle,
    TmdbRateLimitError,
)

TODAY = date(2026, 9, 12)


def _title(tmdb_id: int, content_type: str = "movie") -> CatalogTitle:
    return CatalogTitle(
        tmdb_id=tmdb_id, content_type=content_type, title=f"Title {tmdb_id}", year=2026,
        poster_path=f"/p{tmdb_id}.jpg", overview="", vote_average=8.0, vote_count=200,
    )


class FakeWatchIndex:
    def __init__(self, watched_ids=()):
        self.watched = set(watched_ids)

    def is_watched(self, candidate) -> bool:
        return candidate.tmdb_id in self.watched


class FakeUserState:
    def __init__(self, archived_ids=()):
        self.archived = set(archived_ids)

    def is_manually_watched(self, meta) -> bool:
        return meta.tmdb_id in self.archived


class FakeTmdb:
    """Scripted TmdbClient stand-in. pages maps page number -> list of CatalogTitle."""

    def __init__(self, pages: dict[int, list[CatalogTitle]], keyword=None, now_playing=None,
                 availability=None, availability_errors=None):
        self.pages = pages
        self.keyword = keyword
        self.now_playing = now_playing if now_playing is not None else set()
        self.availability = availability or {}
        self.availability_errors = availability_errors or {}
        self.discover_calls: list[dict] = []
        self.keyword_calls: list[str] = []
        self.availability_calls: list[int] = []
        self.now_playing_calls = 0

    def search_keyword_exact(self, query):
        self.keyword_calls.append(query)
        return self.keyword

    def discover_catalog_page(self, content_type, release_start, release_end, genre=None,
                              keyword_id=None, min_rating=None, page=1, region="US"):
        self.discover_calls.append({
            "content_type": content_type, "release_start": release_start, "release_end": release_end,
            "genre": genre, "keyword_id": keyword_id, "min_rating": min_rating, "page": page, "region": region,
        })
        rows = tuple(self.pages.get(page, []))
        return CatalogPage(rows=rows, page=page, total_pages=max(self.pages) if self.pages else 1)

    def get_now_playing_ids(self, region, cache_dir):
        self.now_playing_calls += 1
        if isinstance(self.now_playing, Exception):
            raise self.now_playing
        return self.now_playing

    def get_catalog_availability(self, tmdb_id, content_type, region, cache_dir):
        self.availability_calls.append(tmdb_id)
        if tmdb_id in self.availability_errors:
            raise self.availability_errors[tmdb_id]
        return self.availability.get(tmdb_id, CatalogAvailability(unknown=True))


def _run(tmdb, criteria=FindCriteria(), watch_index=None, user_state=None, **kwargs) -> FindResults:
    return find_unwatched_titles(
        tmdb,
        watch_index or FakeWatchIndex(),
        user_state or FakeUserState(),
        criteria,
        availability_cache_dir="/tmp/unused",
        today=TODAY,
        **kwargs,
    )


# ── Stable inputs ─────────────────────────────────────────────────────────────

def test_period_and_rating_options_are_locked():
    assert [k for k, _ in cf.PERIOD_OPTIONS] == ["30d", "3m", "6m", "1y", "2y", "5y", "10y"]
    assert [k for k, _, _ in cf.RATING_OPTIONS] == ["any", "6", "7", "7.5", "8"]
    assert [v for _, _, v in cf.RATING_OPTIONS] == [None, 6.0, 7.0, 7.5, 8.0]


def test_default_criteria_is_movies_last_six_months():
    assert FindCriteria() == FindCriteria(content_type="movie", period="6m", genre=None,
                                          keyword=None, min_rating=None)


@pytest.mark.parametrize("period,expected_start", [
    ("30d", date(2026, 8, 13)),
    ("3m", date(2026, 6, 12)),
    ("6m", date(2026, 3, 12)),
    ("1y", date(2025, 9, 12)),
    ("2y", date(2024, 9, 12)),
    ("5y", date(2021, 9, 12)),
    ("10y", date(2016, 9, 12)),
])
def test_release_window_for_every_period(period, expected_start):
    assert release_window(period, today=TODAY) == (expected_start, TODAY)


def test_release_window_unknown_period_falls_back_to_six_months():
    assert release_window("bogus", today=TODAY) == (date(2026, 3, 12), TODAY)


def test_release_window_clamps_month_end():
    assert release_window("6m", today=date(2026, 8, 31)) == (date(2026, 2, 28), date(2026, 8, 31))


def test_release_window_defaults_to_chicago_today():
    start, end = release_window("30d")
    assert (end - start).days == 30
    assert end == cf.chicago_today()


# ── Page loop and watched backfill ────────────────────────────────────────────

def test_backfills_from_page_two_when_page_one_has_watched_titles():
    page1 = [_title(i) for i in range(1, 11)]        # ids 1..10
    page2 = [_title(i) for i in range(11, 21)]       # ids 11..20
    tmdb = FakeTmdb({1: page1, 2: page2})
    watched = FakeWatchIndex({1, 2, 3, 4, 5, 6})

    results = _run(tmdb, watch_index=watched)

    assert [r.title.tmdb_id for r in results.rows] == [7, 8, 9, 10, 11, 12, 13, 14, 15, 16]
    assert [c["page"] for c in tmdb.discover_calls] == [1, 2]
    assert results.pages_read == 2
    assert results.catalog_exhausted is False


def test_stops_at_batch_size_without_reading_extra_pages():
    tmdb = FakeTmdb({1: [_title(i) for i in range(1, 21)], 2: [_title(99)]})
    results = _run(tmdb)
    assert len(results.rows) == 10
    assert [c["page"] for c in tmdb.discover_calls] == [1]
    # Continue from row index 10 on TMDB page 1.
    assert results.next_cursor == "1.10"


def test_manual_archive_entries_are_excluded():
    tmdb = FakeTmdb({1: [_title(i) for i in range(1, 13)]})
    results = _run(tmdb, user_state=FakeUserState({2, 3}))
    assert [r.title.tmdb_id for r in results.rows] == [1, 4, 5, 6, 7, 8, 9, 10, 11, 12]


def test_catalog_exhaustion_returns_fewer_than_ten_and_no_cursor():
    tmdb = FakeTmdb({1: [_title(1), _title(2)], 2: [_title(3)]})
    results = _run(tmdb, watch_index=FakeWatchIndex({2}))
    assert [r.title.tmdb_id for r in results.rows] == [1, 3]
    assert results.catalog_exhausted is True
    assert results.next_cursor is None
    assert [c["page"] for c in tmdb.discover_calls] == [1, 2]


# ── Cursor continuation ───────────────────────────────────────────────────────

def test_cursor_resumes_mid_page_and_carries_on():
    page1 = [_title(i) for i in range(1, 21)]
    page2 = [_title(i) for i in range(21, 41)]
    tmdb = FakeTmdb({1: page1, 2: page2})
    first = _run(tmdb)
    assert [r.title.tmdb_id for r in first.rows] == list(range(1, 11))
    assert first.next_cursor == "1.10"

    tmdb2 = FakeTmdb({1: page1, 2: page2})
    second = _run(tmdb2, cursor=first.next_cursor)
    assert [r.title.tmdb_id for r in second.rows] == list(range(11, 21))
    assert [c["page"] for c in tmdb2.discover_calls] == [1]
    assert second.next_cursor == "2.0", "a batch ending exactly on a page boundary continues on the next page"

    tmdb3 = FakeTmdb({1: page1, 2: page2})
    third = _run(tmdb3, cursor=second.next_cursor)
    assert [r.title.tmdb_id for r in third.rows] == list(range(21, 31))
    assert [c["page"] for c in tmdb3.discover_calls] == [2]
    assert third.next_cursor == "2.10"


def test_cursor_is_exhausted_when_the_last_page_ends_exactly():
    tmdb = FakeTmdb({1: [_title(i) for i in range(1, 11)]})
    results = _run(tmdb)
    assert len(results.rows) == 10
    assert results.next_cursor is None
    assert results.catalog_exhausted is True


def test_cursor_skips_watched_titles_after_the_resume_point():
    page1 = [_title(i) for i in range(1, 21)]
    page2 = [_title(i) for i in range(21, 31)]
    tmdb = FakeTmdb({1: page1, 2: page2})
    results = _run(tmdb, cursor="1.15", watch_index=FakeWatchIndex({16, 17}))
    assert [r.title.tmdb_id for r in results.rows] == [18, 19, 20, 21, 22, 23, 24, 25, 26, 27]
    assert results.next_cursor == "2.7"


def test_invalid_cursor_starts_from_the_beginning():
    tmdb = FakeTmdb({1: [_title(i) for i in range(1, 13)]})
    for bad in ("", "abc", "0.5", "1.-1", "1", "1.2.3", None):
        tmdb.discover_calls.clear()
        results = _run(tmdb, cursor=bad)
        assert [r.title.tmdb_id for r in results.rows] == list(range(1, 11)), bad
        assert tmdb.discover_calls[0]["page"] == 1


def test_cursor_past_total_pages_returns_nothing_more():
    tmdb = FakeTmdb({1: [_title(1)]})
    results = _run(tmdb, cursor="5.0")
    assert results.rows == ()
    assert results.next_cursor is None
    assert results.catalog_exhausted is True


def test_duplicate_tmdb_ids_across_pages_appear_once():
    tmdb = FakeTmdb({1: [_title(1), _title(2), _title(2)], 2: [_title(1), _title(3)]})
    results = _run(tmdb)
    assert [r.title.tmdb_id for r in results.rows] == [1, 2, 3]


def test_empty_page_stops_the_loop():
    tmdb = FakeTmdb({1: [_title(1)], 2: [], 3: [_title(3)]})
    results = _run(tmdb)
    assert [r.title.tmdb_id for r in results.rows] == [1]
    assert [c["page"] for c in tmdb.discover_calls] == [1, 2]


def test_custom_limit_is_honoured():
    tmdb = FakeTmdb({1: [_title(i) for i in range(1, 8)]})
    results = _run(tmdb, limit=3)
    assert len(results.rows) == 3
    assert results.next_cursor == "1.3"


# ── Criteria pass-through ─────────────────────────────────────────────────────

def test_criteria_are_passed_to_discover_with_date_window():
    tmdb = FakeTmdb({1: [_title(1, "tv")]})
    criteria = FindCriteria(content_type="tv", period="1y", genre="crime", min_rating=7.5)
    _run(tmdb, criteria=criteria)
    call = tmdb.discover_calls[0]
    assert call["content_type"] == "tv"
    assert call["release_start"] == date(2025, 9, 12)
    assert call["release_end"] == TODAY
    assert call["genre"] == "crime"
    assert call["min_rating"] == 7.5
    assert call["keyword_id"] is None
    assert call["region"] == "US"
    assert tmdb.keyword_calls == []


def test_exact_keyword_is_resolved_once_and_sent_as_id():
    tmdb = FakeTmdb({1: [_title(1)]}, keyword=(123, "Heist"))
    results = _run(tmdb, criteria=FindCriteria(keyword="heist"))
    assert tmdb.keyword_calls == ["heist"]
    assert tmdb.discover_calls[0]["keyword_id"] == 123
    assert results.keyword_name == "Heist"
    assert results.keyword_missing is False


def test_missing_keyword_returns_no_rows_and_never_broadens():
    tmdb = FakeTmdb({1: [_title(1)]}, keyword=None)
    results = _run(tmdb, criteria=FindCriteria(keyword="nonexistent"))
    assert results.keyword_missing is True
    assert results.rows == ()
    assert tmdb.discover_calls == [], "must not run Discover without the keyword the user asked for"
    assert tmdb.availability_calls == []


# ── Availability annotation ───────────────────────────────────────────────────

def test_availability_is_fetched_only_for_final_rows():
    tmdb = FakeTmdb(
        {1: [_title(i) for i in range(1, 13)]},
        availability={1: CatalogAvailability(stream=("Netflix",)), 3: CatalogAvailability(rent=("Apple TV",))},
        now_playing={2},
    )
    results = _run(tmdb, watch_index=FakeWatchIndex({4}))
    assert tmdb.availability_calls == [1, 2, 3, 5, 6, 7, 8, 9, 10, 11]
    assert tmdb.now_playing_calls == 1
    by_id = {r.title.tmdb_id: r for r in results.rows}
    assert by_id[1].availability.stream == ("Netflix",)
    assert by_id[3].availability.rent == ("Apple TV",)
    assert by_id[2].in_theaters is True
    assert by_id[1].in_theaters is False
    assert by_id[5].availability.unknown is True
    assert results.availability_rate_limited is False


def test_availability_never_changes_inclusion_or_order():
    titles = [_title(i) for i in range(1, 11)]
    tmdb_a = FakeTmdb({1: titles}, availability={i: CatalogAvailability(stream=("Netflix",)) for i in range(1, 11)})
    tmdb_b = FakeTmdb({1: titles}, availability={})   # everything Unknown
    ids_a = [r.title.tmdb_id for r in _run(tmdb_a).rows]
    ids_b = [r.title.tmdb_id for r in _run(tmdb_b).rows]
    assert ids_a == ids_b == list(range(1, 11))


def test_now_playing_is_not_requested_for_tv():
    tmdb = FakeTmdb({1: [_title(1, "tv")]}, now_playing={1})
    results = _run(tmdb, criteria=FindCriteria(content_type="tv"))
    assert tmdb.now_playing_calls == 0
    assert results.rows[0].in_theaters is False


def test_unreadable_now_playing_list_means_no_theater_labels():
    tmdb = FakeTmdb({1: [_title(1)]}, now_playing=None)
    tmdb.now_playing = None
    results = _run(tmdb)
    assert results.rows[0].in_theaters is False


def test_rate_limit_during_availability_keeps_every_result_and_flags_warning():
    tmdb = FakeTmdb(
        {1: [_title(i) for i in range(1, 11)]},
        availability={1: CatalogAvailability(stream=("Netflix",)), 2: CatalogAvailability(buy=("Amazon Video",))},
        availability_errors={3: TmdbRateLimitError(5.0)},
    )
    results = _run(tmdb)
    assert [r.title.tmdb_id for r in results.rows] == list(range(1, 11))
    assert tmdb.availability_calls == [1, 2, 3], "stop calling TMDB once it rate limits"
    assert results.rows[0].availability.stream == ("Netflix",)
    assert results.rows[1].availability.buy == ("Amazon Video",)
    assert all(r.availability.unknown for r in results.rows[2:])
    assert results.availability_rate_limited is True


def test_rate_limit_on_now_playing_skips_availability_and_flags_warning():
    tmdb = FakeTmdb({1: [_title(1), _title(2)]}, now_playing=TmdbRateLimitError(None))
    results = _run(tmdb)
    assert [r.title.tmdb_id for r in results.rows] == [1, 2]
    assert tmdb.availability_calls == []
    assert all(r.availability.unknown and not r.in_theaters for r in results.rows)
    assert results.availability_rate_limited is True


def test_results_carry_criteria_and_window():
    tmdb = FakeTmdb({1: [_title(1)]})
    criteria = FindCriteria(period="2y")
    results = _run(tmdb, criteria=criteria)
    assert results.criteria == criteria
    assert results.release_start == date(2024, 9, 12)
    assert results.release_end == TODAY
