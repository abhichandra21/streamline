import sqlite3
from pathlib import Path

import pytest


def test_init_creates_tables(tmp_path):
    db = str(tmp_path / "test.db")
    from recommender.user_store import init_db

    init_db(db)

    conn = sqlite3.connect(db)
    tables = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    conn.close()
    assert "saved_titles" in tables
    assert "title_ratings" in tables
    assert "manual_archive_entries" in tables
    assert "user_store_meta" in tables


def test_init_is_idempotent(tmp_path):
    db = str(tmp_path / "test.db")
    from recommender.user_store import init_db

    init_db(db)
    init_db(db)  # should not raise


def test_save_title_creates_watchlist_entry(tmp_path):
    db = str(tmp_path / "test.db")
    from recommender.user_store import init_db, save_title, list_saved_titles

    init_db(db)
    save_title(db, "Breaking Bad", "tv", tmdb_id=1396)

    items = list_saved_titles(db, status="watchlist")
    assert len(items) == 1
    assert items[0]["title"] == "Breaking Bad"
    assert items[0]["content_type"] == "tv"
    assert items[0]["tmdb_id"] == 1396
    assert items[0]["status"] == "watchlist"


def test_save_title_without_tmdb_id(tmp_path):
    db = str(tmp_path / "test.db")
    from recommender.user_store import init_db, save_title, list_saved_titles

    init_db(db)
    save_title(db, "Some Obscure Show", "tv")

    items = list_saved_titles(db)
    assert len(items) == 1
    assert items[0]["tmdb_id"] is None


def test_save_title_upserts_on_tmdb_id(tmp_path):
    db = str(tmp_path / "test.db")
    from recommender.user_store import init_db, save_title, list_saved_titles

    init_db(db)
    save_title(db, "Breaking Bad", "tv", tmdb_id=1396)
    save_title(db, "Breaking Bad (2008)", "tv", tmdb_id=1396)  # same tmdb_id

    items = list_saved_titles(db)
    assert len(items) == 1
    assert items[0]["title"] == "Breaking Bad (2008)"  # updated


def test_save_title_upserts_on_normalized_title_when_no_tmdb(tmp_path):
    db = str(tmp_path / "test.db")
    from recommender.user_store import init_db, save_title, list_saved_titles

    init_db(db)
    save_title(db, "Some Show", "tv")
    save_title(db, "Some Show", "tv")  # same normalized title

    items = list_saved_titles(db)
    assert len(items) == 1


def test_dismiss_title(tmp_path):
    db = str(tmp_path / "test.db")
    from recommender.user_store import init_db, save_title, dismiss_title, list_saved_titles

    init_db(db)
    save_title(db, "Breaking Bad", "tv", tmdb_id=1396)
    dismiss_title(db, "Breaking Bad", "tv", tmdb_id=1396)

    items = list_saved_titles(db, status="watchlist")
    assert len(items) == 0
    dismissed = list_saved_titles(db, status="dismissed")
    assert len(dismissed) == 1


def test_save_dismissed_title_flips_to_watchlist(tmp_path):
    db = str(tmp_path / "test.db")
    from recommender.user_store import init_db, dismiss_title, save_title, list_saved_titles

    init_db(db)
    dismiss_title(db, "Breaking Bad", "tv", tmdb_id=1396)
    save_title(db, "Breaking Bad", "tv", tmdb_id=1396)

    items = list_saved_titles(db)
    assert len(items) == 1
    assert items[0]["status"] == "watchlist"


def test_remove_saved_title(tmp_path):
    db = str(tmp_path / "test.db")
    from recommender.user_store import init_db, save_title, remove_saved_title, list_saved_titles

    init_db(db)
    save_title(db, "Breaking Bad", "tv", tmdb_id=1396)
    remove_saved_title(db, "Breaking Bad", "tv")

    assert list_saved_titles(db) == []


def test_list_saved_titles_filters_by_status(tmp_path):
    db = str(tmp_path / "test.db")
    from recommender.user_store import init_db, save_title, dismiss_title, list_saved_titles

    init_db(db)
    save_title(db, "Show A", "tv")
    dismiss_title(db, "Show B", "tv")

    assert len(list_saved_titles(db, status="watchlist")) == 1
    assert len(list_saved_titles(db, status="dismissed")) == 1
    assert len(list_saved_titles(db)) == 2


def test_rate_title(tmp_path):
    db = str(tmp_path / "test.db")
    from recommender.user_store import init_db, rate_title, load_ratings

    init_db(db)
    rate_title(db, "Breaking Bad", "tv", "liked", tmdb_id=1396)

    ratings = load_ratings(db)
    assert len(ratings) == 1
    assert ratings[0]["title"] == "Breaking Bad"
    assert ratings[0]["rating"] == "liked"


def test_rate_title_replaces_previous(tmp_path):
    db = str(tmp_path / "test.db")
    from recommender.user_store import init_db, rate_title, load_ratings

    init_db(db)
    rate_title(db, "Breaking Bad", "tv", "liked", tmdb_id=1396)
    rate_title(db, "Breaking Bad", "tv", "disliked", tmdb_id=1396)

    ratings = load_ratings(db)
    assert len(ratings) == 1
    assert ratings[0]["rating"] == "disliked"


def test_rate_title_clear_removes_rating(tmp_path):
    db = str(tmp_path / "test.db")
    from recommender.user_store import init_db, rate_title, load_ratings

    init_db(db)
    rate_title(db, "Breaking Bad", "tv", "liked", tmdb_id=1396)
    rate_title(db, "Breaking Bad", "tv", "clear", tmdb_id=1396)

    assert load_ratings(db) == []


def test_get_disliked_titles(tmp_path):
    db = str(tmp_path / "test.db")
    from recommender.user_store import init_db, rate_title, get_disliked_titles

    init_db(db)
    rate_title(db, "Show A", "tv", "liked")
    rate_title(db, "Show B", "tv", "disliked")
    rate_title(db, "Movie C", "movie", "disliked")

    disliked = get_disliked_titles(db)
    assert sorted(disliked) == ["Movie C", "Show B"]


def test_apply_rating_multipliers(tmp_path):
    db = str(tmp_path / "test.db")
    from recommender.user_store import init_db, rate_title, load_ratings, apply_rating_multipliers

    init_db(db)
    rate_title(db, "Show A", "tv", "liked")
    rate_title(db, "Show B", "tv", "disliked")

    scores = {"Show A": 0.8, "Show B": 0.8, "Show C": 0.5}
    ratings = load_ratings(db)
    result = apply_rating_multipliers(scores, ratings)
    assert result["Show A"] == min(1.0, 0.8 * 1.3)
    assert result["Show B"] == 0.8 * 0.5
    assert result["Show C"] == 0.5  # unrated, unchanged


def test_add_to_archive(tmp_path):
    db = str(tmp_path / "test.db")
    from recommender.user_store import init_db, add_to_archive, list_manual_archive

    init_db(db)
    add_to_archive(db, "The Bear", "tv", tmdb_id=194583, source="web")

    items = list_manual_archive(db)
    assert len(items) == 1
    assert items[0]["title"] == "The Bear"
    assert items[0]["source"] == "web"


def test_add_to_archive_upserts_on_tmdb_id(tmp_path):
    db = str(tmp_path / "test.db")
    from recommender.user_store import init_db, add_to_archive, list_manual_archive

    init_db(db)
    add_to_archive(db, "The Bear", "tv", tmdb_id=194583)
    add_to_archive(db, "The Bear (2022)", "tv", tmdb_id=194583)

    items = list_manual_archive(db)
    assert len(items) == 1
    assert items[0]["title"] == "The Bear (2022)"


def test_remove_saved_title_does_not_affect_dismissed(tmp_path):
    db = str(tmp_path / "test.db")
    from recommender.user_store import init_db, dismiss_title, remove_saved_title, list_saved_titles

    init_db(db)
    dismiss_title(db, "Breaking Bad", "tv", tmdb_id=1396)
    remove_saved_title(db, "Breaking Bad", "tv")  # should be no-op

    dismissed = list_saved_titles(db, status="dismissed")
    assert len(dismissed) == 1  # dismissed entry still present


def test_add_to_archive_without_tmdb_id(tmp_path):
    db = str(tmp_path / "test.db")
    from recommender.user_store import init_db, add_to_archive, list_manual_archive

    init_db(db)
    add_to_archive(db, "Obscure Film", "movie")

    items = list_manual_archive(db)
    assert len(items) == 1
    assert items[0]["tmdb_id"] is None
