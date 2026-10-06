"""Tests for the Find page collector: date windows, watched backfill, availability annotation."""

from datetime import date

import pytest

from recommender import catalog_finder as cf
from recommender.catalog_finder import FindCriteria, FindResults, find_unwatched_titles, release_window
from recommender.tmdb_client import CatalogPage, CatalogTitle, TmdbRateLimitError

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
    def __init__(self, archived_ids=(), saved_ids=(), dismissed_ids=()):
        self.archived = set(archived_ids)
        self.saved = set(saved_ids)
        self.dismissed = set(dismissed_ids)

    def is_manually_watched(self, meta) -> bool:
        return meta.tmdb_id in self.archived

    def is_in_watchlist(self, meta) -> bool:
        return meta.tmdb_id in self.saved

    def is_dismissed(self, meta) -> bool:
        return meta.tmdb_id in self.dismissed


class FakeTmdb:
    """Scripted TmdbClient stand-in. pages maps page number -> list of CatalogTitle."""

    def __init__(self, pages: dict[int, list[CatalogTitle]], keyword=None, now_playing=None):
        self.pages = pages
        self.keyword = keyword
        self.now_playing = now_playing if now_playing is not None else set()
        self.discover_calls: list[dict] = []
        self.keyword_calls: list[str] = []
        self.now_playing_calls = 0

    def search_keyword_exact(self, query):
        self.keyword_calls.append(query)
        return self.keyword

    def discover_catalog_page(self, content_type, release_start, release_end, genre=None,
                              keyword_id=None, min_rating=None, page=1, region="US",
                              sort_by="vote_average.desc"):
        self.discover_calls.append({
            "content_type": content_type, "release_start": release_start, "release_end": release_end,
            "genre": genre, "keyword_id": keyword_id, "min_rating": min_rating, "page": page, "region": region,
            "sort_by": sort_by,
        })
        rows = tuple(self.pages.get(page, []))
        return CatalogPage(rows=rows, page=page, total_pages=max(self.pages) if self.pages else 1)

    def get_now_playing_ids(self, region, cache_dir):
        self.now_playing_calls += 1
        if isinstance(self.now_playing, Exception):
            raise self.now_playing
        return self.now_playing


def _run(tmdb, criteria=FindCriteria(), watch_index=None, user_state=None, **kwargs) -> FindResults:
    return find_unwatched_titles(
        tmdb,
        watch_index or FakeWatchIndex(),
        user_state or FakeUserState(),
        criteria,
        cache_dir="/tmp/unused",
        today=TODAY,
        **kwargs,
    )


# ── Stable inputs ─────────────────────────────────────────────────────────────

def test_period_and_rating_options_are_locked():
    assert [k for k, _ in cf.PERIOD_OPTIONS] == ["30d", "3m", "6m", "1y", "2y", "5y", "10y"]
    assert [k for k, _, _ in cf.RATING_OPTIONS] == ["any", "6", "7", "7.5", "8"]
    assert [v for _, _, v in cf.RATING_OPTIONS] == [None, 6.0, 7.0, 7.5, 8.0]


def test_default_criteria_is_movies_last_six_months_by_rating():
    assert FindCriteria() == FindCriteria(content_type="movie", period="6m", genre=None,
                                          keyword=None, min_rating=None, sort="rating")


def test_sort_options_are_locked():
    assert [k for k, _ in cf.SORT_OPTIONS] == ["rating", "newest", "popular", "votes"]


@pytest.mark.parametrize("sort,content_type,expected", [
    ("rating", "movie", "vote_average.desc"),
    ("rating", "tv", "vote_average.desc"),
    ("newest", "movie", "primary_release_date.desc"),
    ("newest", "tv", "first_air_date.desc"),
    ("popular", "movie", "popularity.desc"),
    ("votes", "tv", "vote_count.desc"),
    ("bogus", "movie", "vote_average.desc"),
])
def test_discover_sort_by(sort, content_type, expected):
    assert cf.discover_sort_by(sort, content_type) == expected


def test_sort_is_passed_to_discover_and_default_is_rating():
    tmdb = FakeTmdb({1: [_title(1, "tv")]})
    _run(tmdb, criteria=FindCriteria(content_type="tv", sort="newest"))
    assert tmdb.discover_calls[0]["sort_by"] == "first_air_date.desc"
    tmdb = FakeTmdb({1: [_title(1)]})
    _run(tmdb)
    assert tmdb.discover_calls[0]["sort_by"] == "vote_average.desc"


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


def test_watchlist_titles_are_excluded():
    tmdb = FakeTmdb({1: [_title(i) for i in range(1, 13)]})
    results = _run(tmdb, user_state=FakeUserState(saved_ids={2, 3}))
    assert [r.title.tmdb_id for r in results.rows] == [1, 4, 5, 6, 7, 8, 9, 10, 11, 12]


def test_dismissed_titles_are_excluded():
    tmdb = FakeTmdb({1: [_title(i) for i in range(1, 13)]})
    results = _run(tmdb, user_state=FakeUserState(dismissed_ids={2, 3}))
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


def test_exclude_ids_are_skipped_so_a_reordered_title_is_not_shown_twice():
    """Between two clicks TMDB reordered: a title shown in batch one now sits after the cursor."""
    page1 = [_title(i) for i in range(1, 21)]
    tmdb = FakeTmdb({1: page1})
    results = _run(tmdb, cursor="1.10", exclude=frozenset({11, 12, 5}))
    assert [r.title.tmdb_id for r in results.rows] == [13, 14, 15, 16, 17, 18, 19, 20]
    assert results.catalog_exhausted is True


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
    assert tmdb.now_playing_calls == 0


# ── Cinema status ─────────────────────────────────────────────────────────────

def test_now_playing_is_read_once_and_marks_only_listed_movies():
    tmdb = FakeTmdb({1: [_title(i) for i in range(1, 13)]}, now_playing={2})
    results = _run(tmdb, watch_index=FakeWatchIndex({4}))
    assert tmdb.now_playing_calls == 1
    by_id = {r.title.tmdb_id: r for r in results.rows}
    assert by_id[2].in_theaters is True
    assert by_id[1].in_theaters is False
    assert results.now_playing_rate_limited is False


def test_cinema_status_never_changes_inclusion_or_order():
    titles = [_title(i) for i in range(1, 11)]
    ids_a = [r.title.tmdb_id for r in _run(FakeTmdb({1: titles}, now_playing=set(range(1, 11)))).rows]
    ids_b = [r.title.tmdb_id for r in _run(FakeTmdb({1: titles}, now_playing=set())).rows]
    assert ids_a == ids_b == list(range(1, 11))


def test_now_playing_is_not_requested_for_tv():
    tmdb = FakeTmdb({1: [_title(1, "tv")]}, now_playing={1})
    results = _run(tmdb, criteria=FindCriteria(content_type="tv"))
    assert tmdb.now_playing_calls == 0
    assert results.rows[0].in_theaters is None


def test_unreadable_now_playing_list_is_unknown_not_negative():
    tmdb = FakeTmdb({1: [_title(1)]})
    tmdb.now_playing = None
    results = _run(tmdb)
    assert results.rows[0].in_theaters is None


def test_rate_limit_on_now_playing_keeps_every_result_and_flags_warning():
    tmdb = FakeTmdb({1: [_title(1), _title(2)]}, now_playing=TmdbRateLimitError(None))
    results = _run(tmdb)
    assert [r.title.tmdb_id for r in results.rows] == [1, 2]
    assert all(r.in_theaters is None for r in results.rows)
    assert results.now_playing_rate_limited is True


def test_results_carry_criteria_and_window():
    tmdb = FakeTmdb({1: [_title(1)]})
    criteria = FindCriteria(period="2y")
    results = _run(tmdb, criteria=criteria)
    assert results.criteria == criteria
    assert results.release_start == date(2024, 9, 12)
    assert results.release_end == TODAY


# ── IMDb ratings (display only) ───────────────────────────────────────────────
def _imdb_db(path, rows):
    import gzip
    from recommender.imdb_ratings import refresh

    def download(dest):
        with gzip.open(dest, "wt", encoding="utf-8") as f:
            f.write("tconst\taverageRating\tnumVotes\n")
            for imdb_id, rating, votes in rows:
                f.write(f"{imdb_id}\t{rating}\t{votes}\n")
    refresh(path, download=download)


class ImdbTmdb(FakeTmdb):
    def __init__(self, pages, imdb_ids, fail_ids=()):
        super().__init__(pages)
        self.imdb_ids = imdb_ids
        self.fail_ids = set(fail_ids)

    def get_imdb_id(self, tmdb_id, content_type):
        if tmdb_id in self.fail_ids:
            raise TmdbRateLimitError(None)
        return self.imdb_ids.get(tmdb_id)


def test_rows_carry_imdb_ratings_without_changing_order_or_membership(tmp_path):
    from recommender.imdb_ratings import ImdbRating
    db = tmp_path / "imdb.db"
    _imdb_db(db, [("tt0000001", 6.1, 900), ("tt0000003", 8.7, 40000)])
    tmdb = ImdbTmdb({1: [_title(1), _title(2), _title(3), _title(4)]},
                    imdb_ids={1: "tt0000001", 3: "tt0000003", 4: "tt0000004"}, fail_ids={2})

    results = _run(tmdb, imdb_db_path=str(db))

    assert [r.title.tmdb_id for r in results.rows] == [1, 2, 3, 4]
    assert [r.imdb for r in results.rows] == [
        ImdbRating(6.1, 900), None, ImdbRating(8.7, 40000), None,
    ]


def test_rows_have_no_imdb_ratings_without_a_local_copy(tmp_path):
    tmdb = ImdbTmdb({1: [_title(1)]}, imdb_ids={1: "tt0000001"})

    results = _run(tmdb, imdb_db_path=str(tmp_path / "missing.db"))

    assert results.rows[0].imdb is None


# ── Language lists (saved, IMDb-rated) ────────────────────────────────────────
def _write_language_list(cache_dir, titles, built_at="2026-09-12T06:00:00+00:00"):
    import json
    from recommender import language_catalog
    rows = []
    for t in titles:
        row = {"content_type": "movie", "title": f"Title {t['tmdb_id']}", "year": 2026,
               "poster_path": None, "overview": "", "vote_average": 6.0, "vote_count": 3,
               "release_date": "2026-06-01", "popularity": 1.0, "genre_ids": [18],
               "imdb_id": f"tt{t['tmdb_id']:07d}", "imdb_rating": 7.0, "imdb_votes": 5000}
        row.update(t)
        rows.append(row)
    language_catalog.list_path(cache_dir, "hi").write_text(
        json.dumps({"language": "hi", "built_at": built_at, "titles": rows}))


def _run_language(tmp_path, criteria=None, **kwargs):
    criteria = criteria or FindCriteria(language="hi")
    return find_unwatched_titles(
        kwargs.pop("tmdb", FakeTmdb({})), kwargs.pop("watch_index", FakeWatchIndex()),
        kwargs.pop("user_state", FakeUserState()), criteria,
        cache_dir=str(tmp_path), today=TODAY, **kwargs,
    )


def test_language_mode_sorts_by_imdb_rating_and_never_calls_discover(tmp_path):
    _write_language_list(tmp_path, [
        {"tmdb_id": 1, "imdb_rating": 6.5}, {"tmdb_id": 2, "imdb_rating": 8.2},
        {"tmdb_id": 3, "imdb_rating": 7.4},
    ])
    tmdb = FakeTmdb({})

    results = _run_language(tmp_path, tmdb=tmdb)

    assert [r.title.tmdb_id for r in results.rows] == [2, 3, 1]
    assert results.rows[0].imdb.rating == 8.2
    assert tmdb.discover_calls == []
    assert results.list_built_at is not None and results.catalog_exhausted


def test_language_mode_filters_type_period_genre_votes_rating_and_watched(tmp_path):
    _write_language_list(tmp_path, [
        {"tmdb_id": 1},                                   # kept
        {"tmdb_id": 2, "content_type": "tv", "first_air_date": "2026-06-01"},
        {"tmdb_id": 3, "release_date": "2025-01-01"},     # outside 6 months
        {"tmdb_id": 4, "genre_ids": [35]},                # not drama
        {"tmdb_id": 5, "imdb_votes": 499},                # below the IMDb vote floor
        {"tmdb_id": 6, "imdb_rating": 6.9},               # below min rating
        {"tmdb_id": 7},                                   # watched
        {"tmdb_id": 8},                                   # manual archive
        {"tmdb_id": 9, "release_date": None},             # no date: cannot place in a period
        {"tmdb_id": 10},                                  # on the watchlist
        {"tmdb_id": 11},                                  # dismissed
    ])
    criteria = FindCriteria(language="hi", genre="drama", min_rating=7.0)

    results = _run_language(tmp_path, criteria, watch_index=FakeWatchIndex({7}),
                            user_state=FakeUserState({8}, saved_ids={10}, dismissed_ids={11}))

    assert [r.title.tmdb_id for r in results.rows] == [1]


def test_language_mode_other_sorts(tmp_path):
    _write_language_list(tmp_path, [
        {"tmdb_id": 1, "release_date": "2026-05-01", "popularity": 9.0, "imdb_votes": 3000},
        {"tmdb_id": 2, "release_date": "2026-08-01", "popularity": 2.0, "imdb_votes": 90000},
        {"tmdb_id": 3, "release_date": "2026-07-01", "popularity": 5.0, "imdb_votes": 8000},
    ])

    def order(sort):
        return [r.title.tmdb_id for r in _run_language(tmp_path, FindCriteria(language="hi", sort=sort)).rows]

    assert order("newest") == [2, 3, 1]
    assert order("popular") == [1, 3, 2]
    assert order("votes") == [2, 3, 1]


def test_language_mode_pages_with_an_offset_cursor(tmp_path):
    _write_language_list(tmp_path, [{"tmdb_id": i, "imdb_rating": 9.0 - i / 10} for i in range(1, 26)])

    first = _run_language(tmp_path)
    second = _run_language(tmp_path, cursor=first.next_cursor)
    third = _run_language(tmp_path, cursor=second.next_cursor)

    assert [r.title.tmdb_id for r in first.rows] == list(range(1, 11))
    assert [r.title.tmdb_id for r in second.rows] == list(range(11, 21))
    assert [r.title.tmdb_id for r in third.rows] == list(range(21, 26))
    assert third.next_cursor is None and third.catalog_exhausted


def test_language_mode_marks_titles_in_theaters(tmp_path):
    _write_language_list(tmp_path, [{"tmdb_id": 1}, {"tmdb_id": 2}])

    results = _run_language(tmp_path, tmdb=FakeTmdb({}, now_playing={2}))

    assert {r.title.tmdb_id: r.in_theaters for r in results.rows} == {1: False, 2: True}


def test_language_mode_without_a_built_list_is_pending(tmp_path):
    results = _run_language(tmp_path)

    assert results.language_pending and results.rows == ()


def test_language_mode_refuses_a_keyword_rather_than_ignoring_it(tmp_path):
    _write_language_list(tmp_path, [{"tmdb_id": 1}])

    results = _run_language(tmp_path, FindCriteria(language="hi", keyword="heist"))

    assert results.keyword_unsupported and results.rows == ()


def test_language_rating_sort_ranks_widely_rated_titles_above_thinly_rated_ones(tmp_path):
    _write_language_list(tmp_path, [
        {"tmdb_id": 1, "imdb_rating": 9.9, "imdb_votes": 5013},     # few fans, near-perfect
        {"tmdb_id": 2, "imdb_rating": 8.3, "imdb_votes": 248733},   # Dangal-like
        {"tmdb_id": 3, "imdb_rating": 6.0, "imdb_votes": 20000},
        {"tmdb_id": 4, "imdb_rating": 5.0, "imdb_votes": 20000},
    ])

    results = _run_language(tmp_path)

    assert [r.title.tmdb_id for r in results.rows] == [2, 1, 3, 4]
    assert results.rows[1].imdb.rating == 9.9   # the shown rating is IMDb's own


def test_weighted_rating_pulls_thin_ratings_toward_the_average():
    assert cf.weighted_rating(9.9, 5000, 6.2) == pytest.approx(7.433, abs=0.001)
    assert cf.weighted_rating(8.3, 250000, 6.2) == pytest.approx(8.219, abs=0.001)


def test_language_show_more_with_shown_ids_continues_without_skipping(tmp_path):
    """web.py sends the shown ids and the cursor together; the cursor must win."""
    _write_language_list(tmp_path, [{"tmdb_id": i, "imdb_rating": 9.0 - i / 100} for i in range(1, 31)])

    first = _run_language(tmp_path)
    shown = frozenset(r.title.tmdb_id for r in first.rows)
    second = _run_language(tmp_path, cursor=first.next_cursor, exclude=shown)
    shown |= {r.title.tmdb_id for r in second.rows}
    third = _run_language(tmp_path, cursor=second.next_cursor, exclude=shown)

    assert [r.title.tmdb_id for r in second.rows] == list(range(11, 21))
    assert [r.title.tmdb_id for r in third.rows] == list(range(21, 31))
    assert third.next_cursor is None


def test_language_show_more_does_not_repeat_a_title_pushed_forward_by_a_rebuild(tmp_path):
    _write_language_list(tmp_path, [{"tmdb_id": i, "imdb_rating": 9.0 - i / 100} for i in range(1, 16)])
    first = _run_language(tmp_path)
    # A rebuild between clicks adds title 0 at the top, so title 10 (already shown)
    # moves to position 10, where the next batch starts.
    _write_language_list(tmp_path, [{"tmdb_id": i, "imdb_rating": 9.0 - i / 100} for i in range(0, 16)])

    second = _run_language(tmp_path, cursor=first.next_cursor,
                           exclude=frozenset(r.title.tmdb_id for r in first.rows))

    assert [r.title.tmdb_id for r in second.rows] == [11, 12, 13, 14, 15]


def test_language_show_more_survives_a_title_marked_watched_between_clicks(tmp_path):
    _write_language_list(tmp_path, [{"tmdb_id": i, "imdb_rating": 9.0 - i / 100} for i in range(1, 16)])
    first = _run_language(tmp_path)

    second = _run_language(tmp_path, cursor=first.next_cursor,
                           exclude=frozenset(r.title.tmdb_id for r in first.rows),
                           watch_index=FakeWatchIndex({1}))

    assert [r.title.tmdb_id for r in second.rows] == [11, 12, 13, 14, 15]


# ── Have you seen these? ──────────────────────────────────────────────────────

def _classics_tmdb():
    """A movie and a show on every page; vote counts fall as the page number rises."""
    class Tmdb(FakeTmdb):
        def discover_catalog_page(self, content_type, release_start, release_end, page=1, sort_by=None, **kw):
            self.discover_calls.append({"content_type": content_type, "page": page, "sort_by": sort_by})
            base = 1000 if content_type == "movie" else 2000
            row = _title(base + page, content_type)
            row = CatalogTitle(**{**row.__dict__, "vote_count": 1_000_000 - base - page})
            return CatalogPage(rows=(row,), page=page, total_pages=99)
    return Tmdb({})


def test_famous_titles_reads_most_voted_pages_and_caches_each_page(tmp_path):
    tmdb = _classics_tmdb()

    first = cf.famous_titles(tmdb, str(tmp_path), today=TODAY)
    again = cf.famous_titles(tmdb, str(tmp_path), today=TODAY)

    assert len(tmdb.discover_calls) == 15      # 10 movie pages + 5 show pages, once
    assert {c["sort_by"] for c in tmdb.discover_calls} == {"vote_count.desc"}
    assert [t.vote_count for t in first] == sorted((t.vote_count for t in first), reverse=True)
    assert again == first


def test_famous_titles_going_deeper_only_fetches_the_new_pages(tmp_path):
    tmdb = _classics_tmdb()
    cf.famous_titles(tmdb, str(tmp_path), steps=1, today=TODAY)

    deeper = cf.famous_titles(tmdb, str(tmp_path), steps=2, today=TODAY)

    assert len(tmdb.discover_calls) == 30
    assert len(deeper) == 30


def test_famous_titles_failed_page_is_not_cached(tmp_path):
    tmdb = _classics_tmdb()
    good = tmdb.discover_catalog_page

    def flaky(content_type, *a, page=1, **k):
        if content_type == "tv" and page == 2:
            raise TmdbRateLimitError(None)
        return good(content_type, *a, page=page, **k)
    tmdb.discover_catalog_page = flaky

    with pytest.raises(TmdbRateLimitError):
        cf.famous_titles(tmdb, str(tmp_path), today=TODAY)
    assert not (tmp_path / "classics" / "tv_2.json").exists()


def test_next_set_skips_answered_and_shown_titles(tmp_path):
    left = cf.next_classics_set(
        _classics_tmdb(), FakeWatchIndex(watched_ids={1001}),
        FakeUserState(archived_ids={1002}, dismissed_ids={1003}),
        str(tmp_path), shown={("movie", 1004)}, size=5, today=TODAY)

    ids = {t.tmdb_id for t in left}
    assert len(left) == 5
    assert not ids & {1001, 1002, 1003, 1004}


def test_next_set_is_a_random_sample_of_the_most_famous_remaining(tmp_path):
    import random
    tmdb = _classics_tmdb()
    a = cf.next_classics_set(tmdb, FakeWatchIndex(), FakeUserState(), str(tmp_path), set(),
                             today=TODAY, rng=random.Random(1))
    b = cf.next_classics_set(tmdb, FakeWatchIndex(), FakeUserState(), str(tmp_path), set(),
                             today=TODAY, rng=random.Random(2))

    assert len(a) == len(b) == cf.CLASSICS_SET_SIZE
    assert a != b
    assert {t.content_type for t in a + b} == {"movie", "tv"}


def test_next_set_reads_deeper_when_too_few_remain(tmp_path):
    tmdb = _classics_tmdb()
    first_step = {("movie", 1000 + p) for p in range(1, 11)} | {("tv", 2000 + p) for p in range(1, 6)}
    first_step -= {("movie", 1001), ("tv", 2001)}      # leave only two unshown in step one

    got = cf.next_classics_set(tmdb, FakeWatchIndex(), FakeUserState(), str(tmp_path),
                               shown=first_step, today=TODAY)

    assert len(got) == cf.CLASSICS_SET_SIZE
    assert max(c["page"] for c in tmdb.discover_calls) > 10


def test_next_set_is_empty_when_everything_is_used_up(tmp_path):
    tmdb = _classics_tmdb()
    everything = {(t.content_type, t.tmdb_id)
                  for t in cf.famous_titles(tmdb, str(tmp_path), steps=cf.CLASSICS_MAX_STEPS, today=TODAY)}

    got = cf.next_classics_set(tmdb, FakeWatchIndex(), FakeUserState(), str(tmp_path),
                               shown=everything, today=TODAY)

    assert got == []
    assert max(c["page"] for c in tmdb.discover_calls) == 50     # stopped at the 1000-movie cap


def test_next_set_never_repeats_a_title_listed_on_two_pages(tmp_path):
    tmdb = _classics_tmdb()
    good = tmdb.discover_catalog_page

    def overlapping(content_type, *a, page=1, **k):
        return good(content_type, *a, page=1 if page == 2 else page, **k)
    tmdb.discover_catalog_page = overlapping

    got = cf.next_classics_set(tmdb, FakeWatchIndex(), FakeUserState(), str(tmp_path), set(), today=TODAY)

    keys = [(t.content_type, t.tmdb_id) for t in got]
    assert len(keys) == len(set(keys))


def test_next_set_mixes_movies_and_shows_even_when_films_have_more_votes(tmp_path):
    got = cf.next_classics_set(_classics_tmdb(), FakeWatchIndex(), FakeUserState(), str(tmp_path),
                               set(), today=TODAY)

    kinds = [t.content_type for t in got]
    assert kinds.count("movie") == kinds.count("tv") == cf.CLASSICS_SET_SIZE // 2
