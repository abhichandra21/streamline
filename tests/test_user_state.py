from pathlib import Path

from recommender.user_store import (
    init_db, save_title, dismiss_title, add_to_archive, rate_title,
)


class FakeMeta:
    """Minimal stand-in for TmdbMetadata with the fields UserStateIndex needs."""
    def __init__(self, title, content_type, tmdb_id=None):
        self.title = title
        self.content_type = content_type
        self.tmdb_id = tmdb_id


def test_is_manually_watched_by_tmdb_id(tmp_path):
    db = str(tmp_path / "test.db")
    init_db(db)
    add_to_archive(db, "Breaking Bad", "tv", tmdb_id=1396)

    from recommender.user_state import UserStateIndex
    idx = UserStateIndex.load(db)

    assert idx.is_manually_watched(FakeMeta("Breaking Bad", "tv", tmdb_id=1396))
    assert not idx.is_manually_watched(FakeMeta("Breaking Bad", "tv", tmdb_id=9999))


def test_is_manually_watched_fallback_to_normalized_title(tmp_path):
    db = str(tmp_path / "test.db")
    init_db(db)
    add_to_archive(db, "Some Show (2020)", "tv")

    from recommender.user_state import UserStateIndex
    idx = UserStateIndex.load(db)

    # Normalized: "some show"
    assert idx.is_manually_watched(FakeMeta("Some Show", "tv"))
    assert not idx.is_manually_watched(FakeMeta("Some Show", "movie"))


def test_is_dismissed_by_tmdb_id(tmp_path):
    db = str(tmp_path / "test.db")
    init_db(db)
    dismiss_title(db, "Bad Show", "tv", tmdb_id=555)

    from recommender.user_state import UserStateIndex
    idx = UserStateIndex.load(db)

    assert idx.is_dismissed(FakeMeta("Bad Show", "tv", tmdb_id=555))
    assert not idx.is_dismissed(FakeMeta("Bad Show", "tv", tmdb_id=111))


def test_is_dismissed_fallback_to_normalized_title(tmp_path):
    db = str(tmp_path / "test.db")
    init_db(db)
    dismiss_title(db, "Bad Show", "tv")

    from recommender.user_state import UserStateIndex
    idx = UserStateIndex.load(db)

    assert idx.is_dismissed(FakeMeta("Bad Show", "tv"))
    assert not idx.is_dismissed(FakeMeta("Bad Show", "movie"))


def test_has_rating_and_get_rating(tmp_path):
    db = str(tmp_path / "test.db")
    init_db(db)
    rate_title(db, "Breaking Bad", "tv", "liked", tmdb_id=1396)

    from recommender.user_state import UserStateIndex
    idx = UserStateIndex.load(db)

    meta = FakeMeta("Breaking Bad", "tv", tmdb_id=1396)
    assert idx.has_rating(meta)
    assert idx.get_rating(meta) == "liked"

    assert not idx.has_rating(FakeMeta("Unknown", "tv"))
    assert idx.get_rating(FakeMeta("Unknown", "tv")) is None


def test_snapshot_does_not_see_later_mutations(tmp_path):
    db = str(tmp_path / "test.db")
    init_db(db)

    from recommender.user_state import UserStateIndex
    idx = UserStateIndex.load(db)

    # Mutate after snapshot
    add_to_archive(db, "New Show", "tv")
    assert not idx.is_manually_watched(FakeMeta("New Show", "tv"))

    # New snapshot sees it
    idx2 = UserStateIndex.load(db)
    assert idx2.is_manually_watched(FakeMeta("New Show", "tv"))


def test_get_rating_does_not_fallthrough_on_wrong_tmdb_id(tmp_path):
    db = str(tmp_path / "test.db")
    init_db(db)
    rate_title(db, "Breaking Bad", "tv", "liked")  # no tmdb_id, stored by title

    from recommender.user_state import UserStateIndex
    idx = UserStateIndex.load(db)

    # meta has a tmdb_id that is NOT in the ratings dict
    # should NOT fall through to title-based match
    assert idx.get_rating(FakeMeta("Breaking Bad", "tv", tmdb_id=9999)) is None


def test_is_in_watchlist(tmp_path):
    db = str(tmp_path / "test.db")
    init_db(db)
    save_title(db, "Upcoming Show", "tv", tmdb_id=777)

    from recommender.user_state import UserStateIndex
    idx = UserStateIndex.load(db)

    assert idx.is_in_watchlist(FakeMeta("Upcoming Show", "tv", tmdb_id=777))
    assert not idx.is_in_watchlist(FakeMeta("Other", "tv"))
