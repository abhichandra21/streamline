import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

from recommender.ingestion.base import WatchEvent
from recommender.event_store import (
    _connect, _compute_source_hash, init_db, replace_provider_events,
    load_events, get_import_info,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_MANIFEST = [{"path": "/exports/export.zip", "sha256": "abc123"}]
_SNAP_SHA = "snapshot_sha256_value"


def _make_event(platform="netflix", title="Succession S1E1", content_type="tv",
                series_name="Succession", duration_secs=3600, timestamp=None,
                profile="user1", release_year_hint=None, language_hint=None):
    return WatchEvent(
        platform=platform,
        title=title,
        content_type=content_type,
        series_name=series_name,
        watched_duration=timedelta(seconds=duration_secs),
        total_duration=None,
        timestamp=timestamp or datetime(2026, 1, 15, 20, 0, 0),
        profile=profile,
        release_year_hint=release_year_hint,
        language_hint=language_hint,
    )


# ---------------------------------------------------------------------------
# init_db
# ---------------------------------------------------------------------------

def test_init_db_creates_tables(tmp_path):
    db_path = str(tmp_path / "test.db")
    init_db(db_path)

    conn = sqlite3.connect(db_path)
    tables = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    ).fetchall()
    conn.close()

    table_names = [t[0] for t in tables]
    assert "imports" in table_names
    assert "watch_events" in table_names


def test_init_db_idempotent(tmp_path):
    db_path = str(tmp_path / "test.db")
    init_db(db_path)
    init_db(db_path)  # should not raise

    conn = sqlite3.connect(db_path)
    tables = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    ).fetchall()
    conn.close()
    assert len([t for t in tables if t[0] in ("imports", "watch_events")]) == 2


def test_init_db_creates_parent_dirs(tmp_path):
    db_path = str(tmp_path / "nested" / "dir" / "test.db")
    init_db(db_path)
    assert Path(db_path).exists()


def test_init_db_foreign_keys_enabled(tmp_path):
    db_path = str(tmp_path / "test.db")
    init_db(db_path)

    # Use _connect to verify the helper itself enforces FK constraints.
    conn = _connect(db_path)
    try:
        conn.execute(
            "INSERT INTO watch_events (provider, title, content_type, series_name, "
            "watched_duration_seconds, timestamp_iso, profile, import_id, source_hash) "
            "VALUES ('test', 'title', 'tv', 'series', 3600, '2026-01-01T00:00:00', "
            "'user', 999, 'hash')"
        )
        assert False, "Should have raised IntegrityError"
    except sqlite3.IntegrityError:
        pass
    conn.close()


def test_init_db_imports_has_new_columns(tmp_path):
    db_path = str(tmp_path / "test.db")
    init_db(db_path)

    conn = sqlite3.connect(db_path)
    cols = {row[1] for row in conn.execute("PRAGMA table_info(imports)").fetchall()}
    conn.close()

    assert "source_manifest_json" in cols
    assert "snapshot_sha256" in cols
    assert "source_path" not in cols


def test_init_db_migrates_legacy_schema(tmp_path):
    """If the DB has the old source_path schema, init_db should migrate it in place."""
    db_path = str(tmp_path / "test.db")

    # Bootstrap legacy schema manually.
    conn = sqlite3.connect(db_path)
    conn.executescript("""
        CREATE TABLE imports (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            provider      TEXT NOT NULL UNIQUE,
            source_path   TEXT NOT NULL,
            source_sha256 TEXT NOT NULL,
            imported_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
        );
        CREATE TABLE watch_events (
            id                       INTEGER PRIMARY KEY AUTOINCREMENT,
            provider                 TEXT NOT NULL,
            title                    TEXT NOT NULL,
            content_type             TEXT NOT NULL,
            series_name              TEXT NOT NULL,
            watched_duration_seconds INTEGER NOT NULL,
            total_duration_seconds   INTEGER,
            timestamp_iso            TEXT NOT NULL,
            profile                  TEXT NOT NULL,
            import_id                INTEGER NOT NULL,
            source_hash              TEXT NOT NULL UNIQUE
        );
        -- Simulate a user-store table that must survive migration.
        CREATE TABLE saved_titles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL
        );
        INSERT INTO saved_titles (title) VALUES ('Preserved Title');
        INSERT INTO imports (provider, source_path, source_sha256)
            VALUES ('netflix', '/old/export.zip', 'oldsha');
        INSERT INTO watch_events
            (provider, title, content_type, series_name,
             watched_duration_seconds, timestamp_iso, profile, import_id, source_hash)
            VALUES ('netflix', 'Old Show', 'movie', '', 7200, '2026-01-01T00:00:00',
                    'user', 1, 'oldhash');
    """)
    conn.close()

    # Migration should run without error.
    init_db(db_path)

    conn = sqlite3.connect(db_path)
    # New schema in place.
    cols = {row[1] for row in conn.execute("PRAGMA table_info(imports)").fetchall()}
    assert "source_manifest_json" in cols
    assert "source_path" not in cols

    # Legacy event data preserved (in-place migration, not drop+recreate).
    events = conn.execute("SELECT * FROM watch_events").fetchall()
    assert len(events) == 1
    assert events[0][1] == "netflix"  # provider
    assert events[0][2] == "Old Show"  # title

    # imports row preserved with new columns defaulted.
    imports = conn.execute("SELECT provider, source_manifest_json, snapshot_sha256 FROM imports").fetchall()
    assert len(imports) == 1
    assert imports[0][0] == "netflix"
    assert imports[0][1] == "{}"  # default from migration
    assert imports[0][2] == "oldsha"  # renamed from source_sha256

    # User-store table preserved.
    rows = conn.execute("SELECT title FROM saved_titles").fetchall()
    assert rows == [("Preserved Title",)]

    conn.close()


def test_init_db_watch_events_has_hint_columns(tmp_path):
    db_path = str(tmp_path / "test.db")
    init_db(db_path)

    conn = sqlite3.connect(db_path)
    cols = {row[1] for row in conn.execute("PRAGMA table_info(watch_events)").fetchall()}
    conn.close()

    assert "release_year_hint" in cols
    assert "language_hint" in cols


def test_init_db_backfills_hint_columns_onto_existing_table(tmp_path):
    """A watch_events table created before these columns existed must be
    backfilled in place, preserving existing rows."""
    db_path = str(tmp_path / "test.db")

    conn = sqlite3.connect(db_path)
    conn.executescript("""
        CREATE TABLE imports (
            id                   INTEGER PRIMARY KEY AUTOINCREMENT,
            provider             TEXT NOT NULL UNIQUE,
            source_manifest_json TEXT NOT NULL,
            snapshot_sha256      TEXT NOT NULL,
            imported_at          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
        );
        CREATE TABLE watch_events (
            id                       INTEGER PRIMARY KEY AUTOINCREMENT,
            provider                 TEXT NOT NULL,
            title                    TEXT NOT NULL,
            content_type             TEXT NOT NULL,
            series_name              TEXT NOT NULL,
            watched_duration_seconds INTEGER NOT NULL,
            total_duration_seconds   INTEGER,
            timestamp_iso            TEXT NOT NULL,
            profile                  TEXT NOT NULL,
            import_id                INTEGER NOT NULL,
            source_hash              TEXT NOT NULL UNIQUE
        );
        INSERT INTO imports (provider, source_manifest_json, snapshot_sha256)
            VALUES ('netflix', '{}', 'sha');
        INSERT INTO watch_events
            (provider, title, content_type, series_name,
             watched_duration_seconds, timestamp_iso, profile, import_id, source_hash)
            VALUES ('netflix', 'Old Show', 'movie', '', 7200, '2026-01-01T00:00:00',
                    'user', 1, 'oldhash');
    """)
    conn.close()

    init_db(db_path)

    conn = sqlite3.connect(db_path)
    cols = {row[1] for row in conn.execute("PRAGMA table_info(watch_events)").fetchall()}
    rows = conn.execute("SELECT title, release_year_hint, language_hint FROM watch_events").fetchall()
    conn.close()

    assert "release_year_hint" in cols
    assert "language_hint" in cols
    assert rows == [("Old Show", None, None)]


# ---------------------------------------------------------------------------
# _compute_source_hash
# ---------------------------------------------------------------------------

def test_compute_source_hash_is_deterministic():
    """Same event, different profile -> same hash."""
    h1 = _compute_source_hash("netflix", "tv", "Succession", "Succession S1E1",
                               "2026-01-01T00:00:00", 3600)
    h2 = _compute_source_hash("netflix", "tv", "Succession", "Succession S1E1",
                               "2026-01-01T00:00:00", 3600)
    assert h1 == h2


def test_compute_source_hash_differs_by_content_type():
    h1 = _compute_source_hash("netflix", "tv", "S", "T", "2026-01-01T00:00:00", 3600)
    h2 = _compute_source_hash("netflix", "movie", "S", "T", "2026-01-01T00:00:00", 3600)
    assert h1 != h2


def test_compute_source_hash_differs_by_series_name():
    h1 = _compute_source_hash("netflix", "tv", "Series A", "Ep1", "2026-01-01T00:00:00", 3600)
    h2 = _compute_source_hash("netflix", "tv", "Series B", "Ep1", "2026-01-01T00:00:00", 3600)
    assert h1 != h2


# ---------------------------------------------------------------------------
# replace_provider_events
# ---------------------------------------------------------------------------

def test_replace_provider_events_returns_tuple(tmp_path):
    db_path = str(tmp_path / "test.db")
    init_db(db_path)

    events = [
        _make_event(title="Succession S1E1", timestamp=datetime(2026, 1, 1)),
        _make_event(title="Succession S1E2", timestamp=datetime(2026, 1, 2)),
    ]
    result = replace_provider_events(db_path, "netflix", events, _MANIFEST, _SNAP_SHA)
    assert result == (2, 2)


def test_replace_provider_events_zero_events_persists_import_row(tmp_path):
    db_path = str(tmp_path / "test.db")
    init_db(db_path)

    persisted, total = replace_provider_events(db_path, "netflix", [], _MANIFEST, _SNAP_SHA)
    assert persisted == 0
    assert total == 0

    conn = sqlite3.connect(db_path)
    rows = conn.execute("SELECT provider FROM imports").fetchall()
    conn.close()
    assert len(rows) == 1
    assert rows[0][0] == "netflix"


def test_replace_provider_events_replaces_on_reimport(tmp_path):
    db_path = str(tmp_path / "test.db")
    init_db(db_path)

    manifest_v1 = [{"path": "/v1.zip", "sha256": "sha_v1"}]
    events_v1 = [_make_event(title="Old Title", timestamp=datetime(2026, 1, 1))]
    replace_provider_events(db_path, "netflix", events_v1, manifest_v1, "snap_v1")

    manifest_v2 = [{"path": "/v2.zip", "sha256": "sha_v2"}]
    events_v2 = [
        _make_event(title="New Title A", timestamp=datetime(2026, 2, 1)),
        _make_event(title="New Title B", timestamp=datetime(2026, 2, 2)),
    ]
    persisted, total = replace_provider_events(db_path, "netflix", events_v2, manifest_v2, "snap_v2")
    assert persisted == 2
    assert total == 2

    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")
    event_rows = conn.execute("SELECT title FROM watch_events ORDER BY title").fetchall()
    import_rows = conn.execute(
        "SELECT snapshot_sha256 FROM imports WHERE provider='netflix'"
    ).fetchall()
    conn.close()

    assert len(event_rows) == 2
    assert event_rows[0][0] == "New Title A"
    assert import_rows[0][0] == "snap_v2"


def test_replace_provider_events_isolation(tmp_path):
    db_path = str(tmp_path / "test.db")
    init_db(db_path)

    netflix_events = [_make_event(platform="netflix", title="Netflix Show", timestamp=datetime(2026, 1, 1))]
    prime_events = [_make_event(platform="prime", title="Prime Show", timestamp=datetime(2026, 1, 2))]
    replace_provider_events(db_path, "netflix", netflix_events,
                            [{"path": "/netflix.zip", "sha256": "sha_n"}], "snap_n")
    replace_provider_events(db_path, "prime", prime_events,
                            [{"path": "/prime.zip", "sha256": "sha_p"}], "snap_p")

    # Replace netflix only.
    new_netflix = [_make_event(platform="netflix", title="New Netflix", timestamp=datetime(2026, 3, 1))]
    replace_provider_events(db_path, "netflix", new_netflix,
                            [{"path": "/netflix2.zip", "sha256": "sha_n2"}], "snap_n2")

    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")
    all_events = conn.execute(
        "SELECT provider, title FROM watch_events ORDER BY provider, title"
    ).fetchall()
    conn.close()

    assert len(all_events) == 2
    assert all_events[0] == ("netflix", "New Netflix")
    assert all_events[1] == ("prime", "Prime Show")


def test_replace_provider_events_dedup_by_source_hash(tmp_path):
    db_path = str(tmp_path / "test.db")
    init_db(db_path)

    # Two events with identical dedup fields (same provider/content_type/series/title/ts/duration).
    ts = datetime(2026, 1, 1, 20, 0, 0)
    dup_event = _make_event(title="Same Title", timestamp=ts, duration_secs=3600)
    events = [dup_event, dup_event]
    persisted, total = replace_provider_events(db_path, "netflix", events, _MANIFEST, _SNAP_SHA)
    assert total == 2
    assert persisted == 1


def test_replace_provider_events_profile_not_in_dedup_key(tmp_path):
    """Same event watched by two profiles -> dedup treats them as one event."""
    db_path = str(tmp_path / "test.db")
    init_db(db_path)

    ts = datetime(2026, 1, 1, 20, 0, 0)
    e1 = _make_event(title="Show X", timestamp=ts, profile="alice")
    e2 = _make_event(title="Show X", timestamp=ts, profile="bob")
    persisted, total = replace_provider_events(db_path, "netflix", [e1, e2], _MANIFEST, _SNAP_SHA)
    assert total == 2
    assert persisted == 1


def test_replace_provider_events_stores_manifest_json(tmp_path):
    db_path = str(tmp_path / "test.db")
    init_db(db_path)

    manifest = [{"path": "/a.zip", "sha256": "sha_a"}, {"path": "/b.zip", "sha256": "sha_b"}]
    replace_provider_events(db_path, "netflix", [], manifest, "snap_multi")

    conn = sqlite3.connect(db_path)
    row = conn.execute(
        "SELECT source_manifest_json, snapshot_sha256 FROM imports WHERE provider='netflix'"
    ).fetchone()
    conn.close()

    stored_manifest = json.loads(row[0])
    assert stored_manifest == manifest
    assert row[1] == "snap_multi"


# ---------------------------------------------------------------------------
# load_events
# ---------------------------------------------------------------------------

def test_load_events_returns_watch_events_ordered(tmp_path):
    db_path = str(tmp_path / "test.db")
    init_db(db_path)

    events = [
        _make_event(title="Later", timestamp=datetime(2026, 3, 1)),
        _make_event(title="Earlier", timestamp=datetime(2026, 1, 1)),
    ]
    replace_provider_events(db_path, "netflix", events, _MANIFEST, _SNAP_SHA)

    loaded = load_events(db_path)
    assert len(loaded) == 2
    assert isinstance(loaded[0], WatchEvent)
    assert loaded[0].title == "Earlier"
    assert loaded[1].title == "Later"


def test_load_events_provider_filter(tmp_path):
    db_path = str(tmp_path / "test.db")
    init_db(db_path)

    replace_provider_events(db_path, "netflix",
        [_make_event(platform="netflix", title="NF Show", timestamp=datetime(2026, 1, 1))],
        [{"path": "/nf.zip", "sha256": "sha_nf"}], "snap_nf")
    replace_provider_events(db_path, "prime",
        [_make_event(platform="prime", title="Prime Show", timestamp=datetime(2026, 1, 2))],
        [{"path": "/prime.zip", "sha256": "sha_p"}], "snap_p")

    netflix_only = load_events(db_path, provider="netflix")
    assert len(netflix_only) == 1
    assert netflix_only[0].platform == "netflix"


def test_load_events_missing_db_returns_empty(tmp_path):
    db_path = str(tmp_path / "nonexistent" / "test.db")
    loaded = load_events(db_path)
    assert loaded == []


def test_load_events_empty_db_returns_empty(tmp_path):
    db_path = str(tmp_path / "test.db")
    init_db(db_path)
    loaded = load_events(db_path)
    assert loaded == []


def test_load_events_round_trips_release_year_and_language_hints(tmp_path):
    db_path = str(tmp_path / "test.db")
    init_db(db_path)

    original = _make_event(
        title="Don", timestamp=datetime(2026, 1, 1),
        release_year_hint=2006, language_hint="hi",
    )
    replace_provider_events(db_path, "netflix", [original], _MANIFEST, _SNAP_SHA)

    loaded = load_events(db_path)
    assert len(loaded) == 1
    assert loaded[0].release_year_hint == 2006
    assert loaded[0].language_hint == "hi"


def test_load_events_hints_default_to_none(tmp_path):
    db_path = str(tmp_path / "test.db")
    init_db(db_path)

    replace_provider_events(db_path, "netflix", [_make_event()], _MANIFEST, _SNAP_SHA)

    loaded = load_events(db_path)
    assert loaded[0].release_year_hint is None
    assert loaded[0].language_hint is None


def test_load_events_reconstructs_watch_event_fields(tmp_path):
    db_path = str(tmp_path / "test.db")
    init_db(db_path)

    original = _make_event(
        platform="prime", title="The Expanse S1E1", content_type="tv",
        series_name="The Expanse", duration_secs=2700,
        timestamp=datetime(2026, 6, 15, 14, 30, 0), profile="main",
    )
    replace_provider_events(db_path, "prime", [original],
                            [{"path": "/prime.zip", "sha256": "sha1"}], "snap1")

    loaded = load_events(db_path)
    assert len(loaded) == 1
    e = loaded[0]
    assert e.platform == "prime"
    assert e.title == "The Expanse S1E1"
    assert e.content_type == "tv"
    assert e.series_name == "The Expanse"
    assert e.watched_duration == timedelta(seconds=2700)
    assert e.total_duration is None
    assert e.timestamp == datetime(2026, 6, 15, 14, 30, 0)
    assert e.profile == "main"


# ---------------------------------------------------------------------------
# get_import_info
# ---------------------------------------------------------------------------

def test_get_import_info_returns_per_provider_metadata(tmp_path):
    db_path = str(tmp_path / "test.db")
    init_db(db_path)

    nf_manifest = [{"path": "/nf.zip", "sha256": "sha_nf"}]
    replace_provider_events(db_path, "netflix",
        [_make_event(title="Show A", timestamp=datetime(2026, 1, 1)),
         _make_event(title="Show B", timestamp=datetime(2026, 1, 2))],
        nf_manifest, "snap_nf")
    replace_provider_events(db_path, "prime", [],
                            [{"path": "/prime.zip", "sha256": "sha_p"}], "snap_p")

    info = get_import_info(db_path)
    assert "netflix" in info
    assert info["netflix"]["source_manifest"] == nf_manifest
    assert info["netflix"]["snapshot_sha256"] == "snap_nf"
    assert info["netflix"]["event_count"] == 2
    assert "imported_at" in info["netflix"]

    assert "prime" in info
    assert info["prime"]["event_count"] == 0


def test_get_import_info_multi_file_manifest(tmp_path):
    db_path = str(tmp_path / "test.db")
    init_db(db_path)

    manifest = [
        {"path": "/jan.zip", "sha256": "sha_jan"},
        {"path": "/feb.zip", "sha256": "sha_feb"},
    ]
    replace_provider_events(db_path, "netflix", [], manifest, "snap_multi")

    info = get_import_info(db_path)
    assert info["netflix"]["source_manifest"] == manifest
    assert info["netflix"]["snapshot_sha256"] == "snap_multi"


def test_get_import_info_empty_db(tmp_path):
    db_path = str(tmp_path / "test.db")
    init_db(db_path)
    info = get_import_info(db_path)
    assert info == {}
