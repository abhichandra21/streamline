import gzip
from datetime import datetime, timedelta, timezone

import pytest

from recommender import imdb_ratings
from recommender.imdb_ratings import ImdbRating, lookup, refresh, refresh_is_due, refreshed_at


def _writer(lines: list[str]):
    def download(dest):
        with gzip.open(dest, "wt", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
    return download


SAMPLE = [
    "tconst\taverageRating\tnumVotes",
    "tt33014583\t8.2\t151638",
    "tt29540862\t5.0\t24367",
    "tt0000001\t5.7\t2100",
]


def test_refresh_loads_rows_and_lookup_returns_them(tmp_path):
    db = tmp_path / "imdb_ratings.db"

    assert refresh(db, download=_writer(SAMPLE)) == 3

    assert lookup(db, ["tt33014583", "tt29540862", "tt9999999"]) == {
        "tt33014583": ImdbRating(rating=8.2, votes=151638),
        "tt29540862": ImdbRating(rating=5.0, votes=24367),
    }


def test_lookup_without_database_returns_empty(tmp_path):
    assert lookup(tmp_path / "missing.db", ["tt33014583"]) == {}


def test_lookup_ignores_blank_and_malformed_ids(tmp_path):
    db = tmp_path / "imdb_ratings.db"
    refresh(db, download=_writer(SAMPLE))

    assert lookup(db, ["", None, "nm0000001", "ttabc"]) == {}


def test_lookup_handles_more_ids_than_one_query_chunk(tmp_path, monkeypatch):
    monkeypatch.setattr(imdb_ratings, "_LOOKUP_CHUNK", 2)
    db = tmp_path / "imdb_ratings.db"
    refresh(db, download=_writer(SAMPLE))

    assert set(lookup(db, ["tt33014583", "tt29540862", "tt0000001"])) == {
        "tt33014583", "tt29540862", "tt0000001",
    }


def test_refresh_skips_malformed_lines(tmp_path):
    db = tmp_path / "imdb_ratings.db"
    lines = SAMPLE + ["garbage", "tt123\tnot-a-number\t5", "xx1\t7.0\t10"]

    assert refresh(db, download=_writer(lines)) == 3


def test_failed_download_keeps_previous_copy_and_leaves_no_temp_files(tmp_path):
    db = tmp_path / "imdb_ratings.db"
    refresh(db, download=_writer(SAMPLE))

    def broken(dest):
        raise OSError("network down")

    with pytest.raises(OSError):
        refresh(db, download=broken)

    assert lookup(db, ["tt33014583"]) == {"tt33014583": ImdbRating(8.2, 151638)}
    assert sorted(p.name for p in tmp_path.iterdir()) == ["imdb_ratings.db"]


def test_unexpected_header_is_rejected_and_previous_copy_kept(tmp_path):
    db = tmp_path / "imdb_ratings.db"
    refresh(db, download=_writer(SAMPLE))

    with pytest.raises(ValueError):
        refresh(db, download=_writer(["id\trating\tvotes", "tt1\t5.0\t10"]))

    assert lookup(db, ["tt29540862"]) == {"tt29540862": ImdbRating(5.0, 24367)}


def test_empty_dataset_is_rejected(tmp_path):
    db = tmp_path / "imdb_ratings.db"

    with pytest.raises(ValueError):
        refresh(db, download=_writer(SAMPLE[:1]))

    assert not db.exists()


def test_refresh_is_due_follows_build_time(tmp_path):
    db = tmp_path / "imdb_ratings.db"
    assert refresh_is_due(db) is True

    refresh(db, download=_writer(SAMPLE))
    built = refreshed_at(db)

    assert built is not None
    assert refresh_is_due(db, now=built + timedelta(hours=23)) is False
    assert refresh_is_due(db, now=built + timedelta(hours=24)) is True


def test_refreshed_at_is_none_for_unreadable_file(tmp_path):
    db = tmp_path / "imdb_ratings.db"
    db.write_text("not a database")

    assert refreshed_at(db) is None
    assert lookup(db, ["tt33014583"]) == {}
    assert refresh_is_due(db, now=datetime.now(timezone.utc)) is True


def test_refreshed_database_is_readable_like_other_cache_files(tmp_path):
    db = tmp_path / "imdb_ratings.db"
    refresh(db, download=_writer(SAMPLE))
    assert db.stat().st_mode & 0o777 == 0o644
