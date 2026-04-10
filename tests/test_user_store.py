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
    # With tmdb_id: removal uses TMDB-first matching
    save_title(db, "Breaking Bad", "tv", tmdb_id=1396)
    remove_saved_title(db, "Breaking Bad", "tv", tmdb_id=1396)
    assert list_saved_titles(db) == []

    # Without tmdb_id: removal matches only rows where tmdb_id IS NULL
    save_title(db, "Some Show", "tv")
    remove_saved_title(db, "Some Show", "tv")
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


def test_mark_watched_from_watchlist(tmp_path):
    db = str(tmp_path / "test.db")
    from recommender.user_store import (
        init_db, save_title, mark_watched_from_watchlist,
        list_saved_titles, list_manual_archive, load_ratings,
    )

    init_db(db)
    save_title(db, "Breaking Bad", "tv", tmdb_id=1396)
    mark_watched_from_watchlist(db, "Breaking Bad", "tv", rating="liked", tmdb_id=1396)

    assert list_saved_titles(db) == []
    archive = list_manual_archive(db)
    assert len(archive) == 1
    assert archive[0]["title"] == "Breaking Bad"
    ratings = load_ratings(db)
    assert len(ratings) == 1
    assert ratings[0]["rating"] == "liked"


def test_mark_watched_without_rating(tmp_path):
    db = str(tmp_path / "test.db")
    from recommender.user_store import (
        init_db, save_title, mark_watched_from_watchlist,
        list_saved_titles, list_manual_archive, load_ratings,
    )

    init_db(db)
    save_title(db, "The Wire", "tv")
    mark_watched_from_watchlist(db, "The Wire", "tv")

    assert list_saved_titles(db) == []
    assert len(list_manual_archive(db)) == 1
    assert load_ratings(db) == []


def test_mark_watched_rollback_on_failure(tmp_path):
    """If the rating write fails, the entire transaction rolls back."""
    db = str(tmp_path / "test.db")
    from recommender.user_store import init_db, save_title, list_saved_titles, list_manual_archive
    import recommender.user_store as us

    init_db(db)
    save_title(db, "Test Show", "tv", tmdb_id=999)

    # Corrupt the title_ratings table to force a write failure
    import sqlite3
    conn = sqlite3.connect(db)
    conn.execute("DROP TABLE title_ratings")
    conn.commit()
    conn.close()

    with pytest.raises(Exception):
        us.mark_watched_from_watchlist(db, "Test Show", "tv", rating="liked", tmdb_id=999)

    # Watchlist row must still be present
    assert len(list_saved_titles(db)) == 1
    # No archive entry should exist
    assert list_manual_archive(db) == []


def test_tmdb_id_takes_precedence_over_title(tmp_path):
    """Two entries with same normalized title but different tmdb_ids are separate."""
    db = str(tmp_path / "test.db")
    from recommender.user_store import init_db, save_title, list_saved_titles

    init_db(db)
    save_title(db, "The Office", "tv", tmdb_id=2316)
    save_title(db, "The Office", "tv", tmdb_id=69735)  # US vs UK

    items = list_saved_titles(db)
    assert len(items) == 2


def test_no_tmdb_id_dedupes_by_normalized_title(tmp_path):
    db = str(tmp_path / "test.db")
    from recommender.user_store import init_db, save_title, list_saved_titles

    init_db(db)
    save_title(db, "The Office (US)", "tv")  # normalized: "the office"
    save_title(db, "The Office", "tv")       # same normalized title

    items = list_saved_titles(db)
    assert len(items) == 1


def test_tmdb_id_upgrade_reuses_existing_null_identity(tmp_path):
    """A later TMDB-backed save upgrades the NULL-tmdb row instead of duplicating it."""
    db = str(tmp_path / "test.db")
    from recommender.user_store import init_db, save_title, list_saved_titles

    init_db(db)
    save_title(db, "Severance", "tv")
    save_title(db, "Severance", "tv", tmdb_id=95396)

    items = list_saved_titles(db)
    assert len(items) == 1
    assert items[0]["tmdb_id"] == 95396


import json


def test_ensure_user_store_creates_tables(tmp_path):
    db = str(tmp_path / "test.db")
    feedback_path = str(tmp_path / "feedback.json")
    from recommender.user_store import ensure_user_store

    ensure_user_store(db, feedback_path)

    import sqlite3
    conn = sqlite3.connect(db)
    tables = {
        row[0] for row in
        conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }
    conn.close()
    assert "saved_titles" in tables


def test_ensure_user_store_migrates_feedback_json(tmp_path):
    db = str(tmp_path / "test.db")
    feedback_path = str(tmp_path / "feedback.json")
    Path(feedback_path).write_text(json.dumps({
        "ratings": [
            {"title": "Show A", "rating": "liked", "content_type": "tv",
             "timestamp": "2026-01-15T10:30:00+00:00"},
            {"title": "Show B", "rating": "disliked", "timestamp": "2026-02-01T08:00:00+00:00",
             "content_type": "movie"},
        ],
        "additions": [
            {"title": "Movie X", "content_type": "movie", "timestamp": "2026-03-01T12:00:00+00:00"},
        ],
    }))

    from recommender.user_store import ensure_user_store, load_ratings, list_manual_archive

    ensure_user_store(db, feedback_path)

    ratings = load_ratings(db)
    assert len(ratings) == 2
    assert any(r["title"] == "Show A" and r["rating"] == "liked" for r in ratings)

    archive = list_manual_archive(db)
    assert len(archive) == 1
    assert archive[0]["source"] == "feedback_migration"
    assert archive[0]["watched_at"] == "2026-03-01T12:00:00+00:00"

    # feedback.json should be renamed
    assert not Path(feedback_path).exists()
    assert Path(feedback_path + ".migrated").exists()


def test_ensure_user_store_rejects_untyped_feedback_when_type_cannot_be_inferred(tmp_path):
    db = str(tmp_path / "test.db")
    feedback_path = str(tmp_path / "feedback.json")
    Path(feedback_path).write_text(json.dumps({
        "ratings": [
            {"title": "Unknown Title", "rating": "liked", "timestamp": "2026-01-15T10:30:00+00:00"},
        ],
        "additions": [],
    }))

    from recommender.user_store import ensure_user_store

    with pytest.raises(ValueError, match="content type"):
        ensure_user_store(db, feedback_path)


def test_ensure_user_store_skips_if_already_migrated(tmp_path):
    db = str(tmp_path / "test.db")
    feedback_path = str(tmp_path / "feedback.json")

    from recommender.user_store import ensure_user_store

    # First migration
    Path(feedback_path).write_text(json.dumps({"ratings": [], "additions": []}))
    ensure_user_store(db, feedback_path)

    # Calling again with no feedback.json should be fine
    ensure_user_store(db, feedback_path)


def test_ensure_user_store_errors_if_json_exists_but_tables_have_rows(tmp_path):
    db = str(tmp_path / "test.db")
    feedback_path = str(tmp_path / "feedback.json")

    from recommender.user_store import init_db, rate_title, ensure_user_store

    # Create tables and add a row manually (simulating prior user-store writes)
    init_db(db)
    rate_title(db, "Existing", "tv", "liked")

    # Now place a feedback.json (simulating incomplete migration)
    Path(feedback_path).write_text(json.dumps({
        "ratings": [{"title": "New", "rating": "liked", "content_type": "tv",
                     "timestamp": "2026-01-01T00:00:00+00:00"}],
        "additions": [],
    }))

    with pytest.raises(RuntimeError, match="already contain"):
        ensure_user_store(db, feedback_path)


def test_ensure_user_store_retries_rename_if_marker_set(tmp_path):
    """If migration marker exists but feedback.json was not renamed, retry only the rename."""
    db = str(tmp_path / "test.db")
    feedback_path = str(tmp_path / "feedback.json")

    from recommender.user_store import init_db, ensure_user_store

    init_db(db)

    # Set migration marker manually
    import sqlite3
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO user_store_meta (key, value) VALUES ('feedback_migrated', '1')"
    )
    conn.commit()
    conn.close()

    # feedback.json still exists (rename failed last time)
    Path(feedback_path).write_text(json.dumps({"ratings": [], "additions": []}))
    ensure_user_store(db, feedback_path)

    # Should have renamed without re-importing
    assert not Path(feedback_path).exists()
    assert Path(feedback_path + ".migrated").exists()
