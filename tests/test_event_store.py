import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

from recommender.ingestion.base import WatchEvent
from recommender.event_store import _connect, init_db, replace_provider_events, load_events, get_import_info


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

    # Use _connect to verify the helper itself enforces FK constraints
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


def _make_event(platform="netflix", title="Succession S1E1", content_type="tv",
                series_name="Succession", duration_secs=3600, timestamp=None,
                profile="user1"):
    return WatchEvent(
        platform=platform,
        title=title,
        content_type=content_type,
        series_name=series_name,
        watched_duration=timedelta(seconds=duration_secs),
        total_duration=None,
        timestamp=timestamp or datetime(2026, 1, 15, 20, 0, 0),
        profile=profile,
    )


def test_replace_provider_events_inserts_and_returns_count(tmp_path):
    db_path = str(tmp_path / "test.db")
    init_db(db_path)

    events = [
        _make_event(title="Succession S1E1", timestamp=datetime(2026, 1, 1)),
        _make_event(title="Succession S1E2", timestamp=datetime(2026, 1, 2)),
    ]
    count = replace_provider_events(db_path, "netflix", events, "/path/to/export.zip", "abc123sha")
    assert count == 2


def test_replace_provider_events_zero_events_persists_import_row(tmp_path):
    db_path = str(tmp_path / "test.db")
    init_db(db_path)

    count = replace_provider_events(db_path, "netflix", [], "/path/to/export.zip", "abc123sha")
    assert count == 0

    conn = sqlite3.connect(db_path)
    rows = conn.execute("SELECT provider FROM imports").fetchall()
    conn.close()
    assert len(rows) == 1
    assert rows[0][0] == "netflix"


def test_replace_provider_events_replaces_on_reimport(tmp_path):
    db_path = str(tmp_path / "test.db")
    init_db(db_path)

    events_v1 = [_make_event(title="Old Title", timestamp=datetime(2026, 1, 1))]
    replace_provider_events(db_path, "netflix", events_v1, "/v1.zip", "sha_v1")

    events_v2 = [
        _make_event(title="New Title A", timestamp=datetime(2026, 2, 1)),
        _make_event(title="New Title B", timestamp=datetime(2026, 2, 2)),
    ]
    count = replace_provider_events(db_path, "netflix", events_v2, "/v2.zip", "sha_v2")
    assert count == 2

    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")
    event_rows = conn.execute("SELECT title FROM watch_events ORDER BY title").fetchall()
    import_rows = conn.execute("SELECT source_sha256 FROM imports WHERE provider='netflix'").fetchall()
    conn.close()

    assert len(event_rows) == 2
    assert event_rows[0][0] == "New Title A"
    assert import_rows[0][0] == "sha_v2"


def test_replace_provider_events_isolation(tmp_path):
    db_path = str(tmp_path / "test.db")
    init_db(db_path)

    netflix_events = [_make_event(platform="netflix", title="Netflix Show", timestamp=datetime(2026, 1, 1))]
    prime_events = [_make_event(platform="prime", title="Prime Show", timestamp=datetime(2026, 1, 2))]
    replace_provider_events(db_path, "netflix", netflix_events, "/netflix.zip", "sha_n")
    replace_provider_events(db_path, "prime", prime_events, "/prime.zip", "sha_p")

    # Replace netflix only
    new_netflix = [_make_event(platform="netflix", title="New Netflix", timestamp=datetime(2026, 3, 1))]
    replace_provider_events(db_path, "netflix", new_netflix, "/netflix2.zip", "sha_n2")

    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")
    all_events = conn.execute("SELECT provider, title FROM watch_events ORDER BY provider, title").fetchall()
    conn.close()

    assert len(all_events) == 2
    assert all_events[0] == ("netflix", "New Netflix")
    assert all_events[1] == ("prime", "Prime Show")


def test_replace_provider_events_dedup_by_source_hash(tmp_path):
    db_path = str(tmp_path / "test.db")
    init_db(db_path)

    # Two events with identical dedup fields
    ts = datetime(2026, 1, 1, 20, 0, 0)
    dup_event = _make_event(title="Same Title", timestamp=ts, duration_secs=3600)
    events = [dup_event, dup_event]
    count = replace_provider_events(db_path, "netflix", events, "/export.zip", "sha1")
    assert count == 1  # one deduplicated away


def test_load_events_returns_watch_events_ordered(tmp_path):
    db_path = str(tmp_path / "test.db")
    init_db(db_path)

    events = [
        _make_event(title="Later", timestamp=datetime(2026, 3, 1)),
        _make_event(title="Earlier", timestamp=datetime(2026, 1, 1)),
    ]
    replace_provider_events(db_path, "netflix", events, "/export.zip", "sha1")

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
        "/nf.zip", "sha_nf")
    replace_provider_events(db_path, "prime",
        [_make_event(platform="prime", title="Prime Show", timestamp=datetime(2026, 1, 2))],
        "/prime.zip", "sha_p")

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


def test_load_events_reconstructs_watch_event_fields(tmp_path):
    db_path = str(tmp_path / "test.db")
    init_db(db_path)

    original = _make_event(
        platform="prime", title="The Expanse S1E1", content_type="tv",
        series_name="The Expanse", duration_secs=2700,
        timestamp=datetime(2026, 6, 15, 14, 30, 0), profile="main",
    )
    replace_provider_events(db_path, "prime", [original], "/prime.zip", "sha1")

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


def test_get_import_info_returns_per_provider_metadata(tmp_path):
    db_path = str(tmp_path / "test.db")
    init_db(db_path)

    replace_provider_events(db_path, "netflix",
        [_make_event(title="Show A", timestamp=datetime(2026, 1, 1)),
         _make_event(title="Show B", timestamp=datetime(2026, 1, 2))],
        "/nf.zip", "sha_nf")
    replace_provider_events(db_path, "prime", [], "/prime.zip", "sha_p")

    info = get_import_info(db_path)
    assert "netflix" in info
    assert info["netflix"]["source_path"] == "/nf.zip"
    assert info["netflix"]["source_sha256"] == "sha_nf"
    assert info["netflix"]["event_count"] == 2
    assert "imported_at" in info["netflix"]

    assert "prime" in info
    assert info["prime"]["event_count"] == 0


def test_get_import_info_empty_db(tmp_path):
    db_path = str(tmp_path / "test.db")
    init_db(db_path)
    info = get_import_info(db_path)
    assert info == {}
