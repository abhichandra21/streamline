import sqlite3

import pytest

from recommender.user_store import init_db, save_title
from tools.merge_user_state import merge_db


def _create_user_db(path):
    init_db(str(path))


def _insert_tracking_row(path, values):
    with sqlite3.connect(path) as conn:
        conn.execute(
            "INSERT INTO show_tracking "
            "(tmdb_id, title, state, tracking_from_season, caught_up_season, "
            "caught_up_episode, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            values,
        )


def test_merge_db_copies_show_tracking_once_and_is_idempotent(tmp_path):
    local_path = tmp_path / "local.db"
    other_path = tmp_path / "other.db"
    _create_user_db(local_path)
    _create_user_db(other_path)
    source_row = (
        95480,
        "Slow Horses",
        "following",
        5,
        5,
        3,
        "2026-08-01T12:00:00+00:00",
        "2026-08-02T12:00:00+00:00",
    )
    _insert_tracking_row(other_path, source_row)

    merge_db(str(local_path), str(other_path))
    merge_db(str(local_path), str(other_path))

    with sqlite3.connect(local_path) as conn:
        rows = conn.execute(
            "SELECT tmdb_id, title, state, tracking_from_season, "
            "caught_up_season, caught_up_episode, created_at, updated_at "
            "FROM show_tracking"
        ).fetchall()
    assert rows == [source_row]


def test_merge_db_applies_newer_show_tracking_row_atomically(tmp_path):
    local_path = tmp_path / "local.db"
    other_path = tmp_path / "other.db"
    _create_user_db(local_path)
    _create_user_db(other_path)
    _insert_tracking_row(local_path, (
        95480,
        "Slow Horses",
        "following",
        5,
        5,
        3,
        "2026-07-01T12:00:00+00:00",
        "2026-08-02T12:00:00+00:00",
    ))
    _insert_tracking_row(other_path, (
        95480,
        "Slow Horses: London Rules",
        "ignored",
        6,
        None,
        None,
        "2026-06-01T12:00:00+00:00",
        "2026-08-03T12:00:00+00:00",
    ))

    merge_db(str(local_path), str(other_path))

    with sqlite3.connect(local_path) as conn:
        row = conn.execute(
            "SELECT title, state, tracking_from_season, caught_up_season, "
            "caught_up_episode, created_at, updated_at FROM show_tracking "
            "WHERE tmdb_id = 95480"
        ).fetchone()
    assert row == (
        "Slow Horses: London Rules",
        "ignored",
        6,
        None,
        None,
        "2026-07-01T12:00:00+00:00",
        "2026-08-03T12:00:00+00:00",
    )


@pytest.mark.parametrize(
    "source_updated_at",
    ["2026-08-01T12:00:00+00:00", "2026-08-02T12:00:00+00:00"],
)
def test_merge_db_keeps_local_show_tracking_when_source_is_not_newer(
    tmp_path, source_updated_at,
):
    local_path = tmp_path / "local.db"
    other_path = tmp_path / "other.db"
    _create_user_db(local_path)
    _create_user_db(other_path)
    local_row = (
        95480,
        "Slow Horses",
        "following",
        5,
        5,
        3,
        "2026-07-01T12:00:00+00:00",
        "2026-08-02T12:00:00+00:00",
    )
    _insert_tracking_row(local_path, local_row)
    _insert_tracking_row(other_path, (
        95480,
        "Stale title",
        "ignored",
        6,
        None,
        None,
        "2026-06-01T12:00:00+00:00",
        source_updated_at,
    ))

    merge_db(str(local_path), str(other_path))

    with sqlite3.connect(local_path) as conn:
        row = conn.execute(
            "SELECT tmdb_id, title, state, tracking_from_season, "
            "caught_up_season, caught_up_episode, created_at, updated_at "
            "FROM show_tracking WHERE tmdb_id = 95480"
        ).fetchone()
    assert row == local_row


def test_merge_db_accepts_source_database_without_show_tracking(tmp_path):
    local_path = tmp_path / "local.db"
    other_path = tmp_path / "other.db"
    _create_user_db(local_path)
    _create_user_db(other_path)
    with sqlite3.connect(other_path) as conn:
        conn.execute("DROP TABLE show_tracking")
    save_title(
        str(other_path),
        "The Bear",
        "tv",
        tmdb_id=136315,
    )

    merge_db(str(local_path), str(other_path))

    with sqlite3.connect(local_path) as conn:
        saved = conn.execute(
            "SELECT title, content_type, tmdb_id FROM saved_titles"
        ).fetchall()
        tracking = conn.execute("SELECT * FROM show_tracking").fetchall()
    assert saved == [("The Bear", "tv", 136315)]
    assert tracking == []


def test_merge_db_preserves_existing_saved_title_conflict_behavior(tmp_path):
    local_path = tmp_path / "local.db"
    other_path = tmp_path / "other.db"
    _create_user_db(local_path)
    _create_user_db(other_path)
    save_title(str(local_path), "The Bear", "tv", tmdb_id=136315)
    save_title(
        str(other_path),
        "The Bear",
        "tv",
        tmdb_id=136315,
        status="dismissed",
    )
    with sqlite3.connect(local_path) as conn:
        conn.execute(
            "UPDATE saved_titles SET saved_at = ?, updated_at = ?",
            (
                "2026-07-01T12:00:00+00:00",
                "2026-08-01T12:00:00+00:00",
            ),
        )
    with sqlite3.connect(other_path) as conn:
        conn.execute(
            "UPDATE saved_titles SET saved_at = ?, updated_at = ?",
            (
                "2026-07-02T12:00:00+00:00",
                "2026-08-02T12:00:00+00:00",
            ),
        )

    merge_db(str(local_path), str(other_path))

    with sqlite3.connect(local_path) as conn:
        row = conn.execute(
            "SELECT status, saved_at, updated_at FROM saved_titles"
        ).fetchone()
    assert row == (
        "dismissed",
        "2026-07-01T12:00:00+00:00",
        "2026-08-02T12:00:00+00:00",
    )


def test_merge_db_rejects_incompatible_show_tracking_without_partial_merge(tmp_path):
    local_path = tmp_path / "local.db"
    other_path = tmp_path / "other.db"
    _create_user_db(local_path)
    _create_user_db(other_path)
    local_tracking_row = (
        95480,
        "Slow Horses",
        "following",
        5,
        5,
        3,
        "2026-07-01T12:00:00+00:00",
        "2026-08-01T12:00:00+00:00",
    )
    _insert_tracking_row(local_path, local_tracking_row)
    save_title(
        str(other_path),
        "The Bear",
        "tv",
        tmdb_id=136315,
    )
    with sqlite3.connect(other_path) as conn:
        conn.execute("DROP TABLE show_tracking")
        conn.executescript("""
            CREATE TABLE show_tracking (
                tmdb_id                INTEGER PRIMARY KEY,
                title                  TEXT NOT NULL,
                state                  TEXT NOT NULL,
                tracking_from_season   INTEGER NOT NULL,
                caught_up_season       INTEGER,
                created_at             TEXT NOT NULL,
                updated_at             TEXT NOT NULL
            );
            INSERT INTO show_tracking VALUES (
                95480,
                'Slow Horses: London Rules',
                'ignored',
                6,
                NULL,
                '2026-06-01T12:00:00+00:00',
                '2026-08-03T12:00:00+00:00'
            );
        """)

    with pytest.raises(sqlite3.OperationalError, match="incompatible show_tracking schema"):
        merge_db(str(local_path), str(other_path))

    with sqlite3.connect(local_path) as conn:
        saved = conn.execute("SELECT * FROM saved_titles").fetchall()
        tracking = conn.execute(
            "SELECT tmdb_id, title, state, tracking_from_season, "
            "caught_up_season, caught_up_episode, created_at, updated_at "
            "FROM show_tracking"
        ).fetchall()
    assert saved == []
    assert tracking == [local_tracking_row]


def test_merge_db_rejects_show_tracking_without_tmdb_id_identity(tmp_path):
    local_path = tmp_path / "local.db"
    other_path = tmp_path / "other.db"
    _create_user_db(local_path)
    _create_user_db(other_path)
    with sqlite3.connect(other_path) as conn:
        conn.execute("DROP TABLE show_tracking")
        conn.executescript("""
            CREATE TABLE show_tracking (
                tmdb_id                INTEGER,
                title                  TEXT NOT NULL,
                state                  TEXT NOT NULL,
                tracking_from_season   INTEGER,
                caught_up_season       INTEGER,
                caught_up_episode      INTEGER,
                created_at             TEXT NOT NULL,
                updated_at             TEXT NOT NULL
            );
            INSERT INTO show_tracking VALUES (
                95480,
                'Slow Horses',
                'following',
                5,
                NULL,
                NULL,
                '2026-08-01T12:00:00+00:00',
                '2026-08-02T12:00:00+00:00'
            );
        """)

    with pytest.raises(sqlite3.OperationalError, match="tmdb_id must be the primary key"):
        merge_db(str(local_path), str(other_path))


def test_merge_db_rejects_composite_show_tracking_identity(tmp_path):
    local_path = tmp_path / "local.db"
    other_path = tmp_path / "other.db"
    _create_user_db(local_path)
    _create_user_db(other_path)
    with sqlite3.connect(other_path) as conn:
        conn.execute("DROP TABLE show_tracking")
        conn.executescript("""
            CREATE TABLE show_tracking (
                tmdb_id                INTEGER NOT NULL,
                title                  TEXT NOT NULL,
                state                  TEXT NOT NULL,
                tracking_from_season   INTEGER,
                caught_up_season       INTEGER,
                caught_up_episode      INTEGER,
                created_at             TEXT NOT NULL,
                updated_at             TEXT NOT NULL,
                PRIMARY KEY (tmdb_id, title)
            );
            INSERT INTO show_tracking VALUES (
                95480,
                'Slow Horses',
                'following',
                5,
                NULL,
                NULL,
                '2026-08-01T12:00:00+00:00',
                '2026-08-02T12:00:00+00:00'
            );
        """)

    with pytest.raises(sqlite3.OperationalError, match="tmdb_id must be the only primary key"):
        merge_db(str(local_path), str(other_path))


def test_merge_db_rolls_back_all_tables_when_show_tracking_row_is_invalid(tmp_path):
    local_path = tmp_path / "local.db"
    other_path = tmp_path / "other.db"
    _create_user_db(local_path)
    _create_user_db(other_path)
    save_title(
        str(other_path),
        "The Bear",
        "tv",
        tmdb_id=136315,
    )
    with sqlite3.connect(other_path) as conn:
        conn.execute("DROP TABLE show_tracking")
        conn.executescript("""
            CREATE TABLE show_tracking (
                tmdb_id                INTEGER PRIMARY KEY,
                title                  TEXT NOT NULL,
                state                  TEXT NOT NULL,
                tracking_from_season   INTEGER,
                caught_up_season       INTEGER,
                caught_up_episode      INTEGER,
                created_at             TEXT NOT NULL,
                updated_at             TEXT NOT NULL
            );
            INSERT INTO show_tracking VALUES (
                95480,
                'Slow Horses',
                'invalid',
                5,
                NULL,
                NULL,
                '2026-08-01T12:00:00+00:00',
                '2026-08-02T12:00:00+00:00'
            );
        """)

    with pytest.raises(sqlite3.IntegrityError):
        merge_db(str(local_path), str(other_path))

    with sqlite3.connect(local_path) as conn:
        saved = conn.execute("SELECT * FROM saved_titles").fetchall()
        tracking = conn.execute("SELECT * FROM show_tracking").fetchall()
    assert saved == []
    assert tracking == []
