import gzip
from datetime import date, datetime, timedelta, timezone

import pytest
import requests

from recommender import language_catalog as lc
from recommender.imdb_ratings import ImdbRating, refresh
from recommender.tmdb_client import TmdbRateLimitError

TODAY = date(2026, 10, 3)


def _imdb_db(path, rows):
    def download(dest):
        with gzip.open(dest, "wt", encoding="utf-8") as f:
            f.write("tconst\taverageRating\tnumVotes\n")
            for imdb_id, rating, votes in rows:
                f.write(f"{imdb_id}\t{rating}\t{votes}\n")
    refresh(path, download=download)


def _item(tmdb_id, content_type="movie", popularity=10.0, release="2026-05-01", genres=(18,)):
    date_key = "first_air_date" if content_type == "tv" else "release_date"
    name_key = "name" if content_type == "tv" else "title"
    return {"id": tmdb_id, name_key: f"Title {tmdb_id}", date_key: release, "popularity": popularity,
            "genre_ids": list(genres), "vote_average": 6.0, "vote_count": 3,
            "poster_path": f"/p{tmdb_id}.jpg", "overview": "o"}


class FakeTmdb:
    def __init__(self, pages, imdb_ids, errors=None):
        self.pages = pages          # {(content_type, page): [items]}
        self.imdb_ids = imdb_ids    # {tmdb_id: "tt..."}
        self.errors = errors or {}  # {tmdb_id: [exceptions to raise, in order]}
        self.discover_calls = []

    def discover_language_page(self, content_type, language, start, end, page=1):
        """Serve each scripted page only to the window holding its items' dates."""
        self.discover_calls.append((content_type, language, start, end, page))
        date_key = "first_air_date" if content_type == "tv" else "release_date"

        def in_window(items):
            return [i for i in items if start <= date.fromisoformat(i[date_key]) <= end]
        pages = {p: in_window(items) for (ct, p), items in self.pages.items() if ct == content_type}
        pages = {p: items for p, items in pages.items() if items}
        total = max(pages, default=0)
        return list(pages.get(page, [])), total

    def get_imdb_id(self, tmdb_id, content_type):
        pending = self.errors.get(tmdb_id)
        if pending:
            raise pending.pop(0)
        return self.imdb_ids.get(tmdb_id)


@pytest.fixture
def imdb_db(tmp_path):
    db = tmp_path / "imdb.db"
    _imdb_db(db, [("tt0000001", 8.2, 151638), ("tt0000002", 5.0, 24367), ("tt0000003", 7.9, 17042)])
    return db


def test_build_reads_every_page_for_movies_and_tv_and_keeps_rated_titles(tmp_path, imdb_db):
    tmdb = FakeTmdb(
        pages={("movie", 1): [_item(1), _item(2)], ("movie", 2): [_item(4)],
               ("tv", 1): [_item(3, "tv")]},
        imdb_ids={1: "tt0000001", 2: "tt0000002", 3: "tt0000003", 4: None},
    )

    assert lc.build(tmdb, "hi", imdb_db, tmp_path, today=TODAY) == 3

    saved = lc.load(tmp_path, "hi")
    assert {(t.title.content_type, t.title.tmdb_id) for t in saved.titles} == {
        ("movie", 1), ("movie", 2), ("tv", 3)}
    first = next(t for t in saved.titles if t.title.tmdb_id == 1)
    assert first.imdb == ImdbRating(8.2, 151638)
    assert first.release_date == date(2026, 5, 1)
    assert first.genre_ids == frozenset({18})
    reads = [(c[0], c[2], c[4]) for c in tmdb.discover_calls if c[4] > 1 or c[0] == "movie"]
    assert ("movie", date(2025, 10, 3), 2) in reads    # second page of the window holding May 2026
    windows = [(c[2], c[3]) for c in tmdb.discover_calls if c[0] == "movie" and c[4] == 1]
    assert windows[0] == (date(2016, 10, 3), date(2017, 10, 2))
    # Ten one-year windows plus today itself, since the range includes both ends.
    assert windows[-2] == (date(2025, 10, 3), date(2026, 10, 2))
    assert windows[-1] == (TODAY, TODAY)
    assert len(windows) == 11


def test_build_keeps_the_first_sighting_of_a_title_seen_on_two_pages(tmp_path, imdb_db):
    tmdb = FakeTmdb(pages={("movie", 1): [_item(1, popularity=9)], ("movie", 2): [_item(1, popularity=3)]},
                    imdb_ids={1: "tt0000001"})

    assert lc.build(tmdb, "hi", imdb_db, tmp_path, today=TODAY) == 1
    assert lc.load(tmp_path, "hi").titles[0].popularity == 9


def test_build_waits_out_rate_limits(tmp_path, imdb_db, monkeypatch):
    monkeypatch.setattr(lc.time, "sleep", lambda s: None)
    tmdb = FakeTmdb(pages={("movie", 1): [_item(1)]}, imdb_ids={1: "tt0000001"},
                    errors={1: [TmdbRateLimitError(0.1), TmdbRateLimitError(None)]})

    assert lc.build(tmdb, "hi", imdb_db, tmp_path, today=TODAY) == 1


def test_build_treats_a_title_removed_from_tmdb_as_unrated(tmp_path, imdb_db):
    gone = requests.Response()
    gone.status_code = 404
    tmdb = FakeTmdb(pages={("movie", 1): [_item(1), _item(2)]}, imdb_ids={1: "tt0000001"},
                    errors={2: [requests.HTTPError("gone", response=gone)]})

    assert lc.build(tmdb, "hi", imdb_db, tmp_path, today=TODAY) == 1


def test_build_retries_timeouts(tmp_path, imdb_db, monkeypatch):
    monkeypatch.setattr(lc.time, "sleep", lambda s: None)
    tmdb = FakeTmdb(pages={("movie", 1): [_item(1)]}, imdb_ids={1: "tt0000001"},
                    errors={1: [requests.ConnectionError("ReadTimeout")]})

    assert lc.build(tmdb, "hi", imdb_db, tmp_path, today=TODAY) == 1


def test_failed_build_keeps_the_previous_list(tmp_path, imdb_db, monkeypatch):
    monkeypatch.setattr(lc.time, "sleep", lambda s: None)
    good = FakeTmdb(pages={("movie", 1): [_item(1)]}, imdb_ids={1: "tt0000001"})
    lc.build(good, "hi", imdb_db, tmp_path, today=TODAY)
    broken = FakeTmdb(pages={("movie", 1): [_item(1)]}, imdb_ids={},
                      errors={1: [requests.ConnectionError("down")] * 5})

    with pytest.raises(requests.ConnectionError):
        lc.build(broken, "hi", imdb_db, tmp_path, today=TODAY)

    assert [t.title.tmdb_id for t in lc.load(tmp_path, "hi").titles] == [1]
    assert sorted(p.name for p in tmp_path.iterdir() if p.name.startswith(".language")) == []


def test_build_refuses_without_imdb_ratings(tmp_path):
    with pytest.raises(RuntimeError):
        lc.build(FakeTmdb({}, {}), "hi", tmp_path / "missing.db", tmp_path, today=TODAY)


def test_build_reports_progress(tmp_path, imdb_db):
    items = [_item(i) for i in range(1, 121)]
    tmdb = FakeTmdb(pages={("movie", 1): items}, imdb_ids={})
    seen = []

    lc.build(tmdb, "hi", imdb_db, tmp_path, today=TODAY, progress=lambda d, t: seen.append((d, t)))

    assert seen[-1] == (120, 120)
    assert (50, 120) in seen and (100, 120) in seen


def test_load_and_build_is_due(tmp_path, imdb_db):
    assert lc.load(tmp_path, "hi") is None
    assert lc.build_is_due(tmp_path, "hi") is True

    lc.build(FakeTmdb({("movie", 1): [_item(1)]}, {1: "tt0000001"}), "hi", imdb_db, tmp_path, today=TODAY)
    built = lc.load(tmp_path, "hi").built_at

    assert lc.build_is_due(tmp_path, "hi", now=built + timedelta(hours=23)) is False
    assert lc.build_is_due(tmp_path, "hi", now=built + timedelta(hours=24)) is True


def test_unreadable_list_loads_as_missing(tmp_path):
    lc.list_path(tmp_path, "hi").write_text("{not json")
    assert lc.load(tmp_path, "hi") is None
    assert lc.build_is_due(tmp_path, "hi", now=datetime.now(timezone.utc)) is True


def test_year_windows_cover_the_range_without_gaps_or_overlap():
    windows = lc._year_windows(date(2016, 10, 3), TODAY)
    assert windows[0][0] == date(2016, 10, 3) and windows[-1][1] == TODAY
    for (_, end), (next_start, _) in zip(windows, windows[1:]):
        assert (next_start - end).days == 1


def test_build_refuses_to_publish_when_a_year_exceeds_tmdbs_page_limit(tmp_path, imdb_db):
    lc.build(FakeTmdb({("movie", 1): [_item(1)]}, {1: "tt0000001"}), "hi", imdb_db, tmp_path, today=TODAY)

    class Huge(FakeTmdb):
        def discover_language_page(self, content_type, language, start, end, page=1):
            return [_item(page)], 600

    with pytest.raises(RuntimeError, match="500-page limit"):
        lc.build(Huge({}, {}), "hi", imdb_db, tmp_path, today=TODAY)
    assert [t.title.tmdb_id for t in lc.load(tmp_path, "hi").titles] == [1]
