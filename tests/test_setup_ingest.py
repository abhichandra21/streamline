"""Tests for multi-export ingest orchestration, _dedup_events, and runtime fallback."""
import sqlite3
from datetime import datetime, timedelta

import pytest

from recommender.ingestion.base import WatchEvent
from recommender.setup import _dedup_events


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_event(
    platform="netflix",
    title="Succession S1E1",
    content_type="tv",
    series_name="Succession",
    duration_secs=3600,
    timestamp=None,
    profile="user1",
):
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


# ---------------------------------------------------------------------------
# _dedup_events unit tests
# ---------------------------------------------------------------------------

def test_dedup_events_removes_exact_duplicate():
    """Two identical events collapse to one."""
    e = _make_event()
    deduped, dup_count = _dedup_events([e, e])
    assert len(deduped) == 1
    assert dup_count == 1


def test_dedup_events_different_profile_same_event_counts_as_duplicate():
    """Same event watched by two profiles -> dedup treats them as one."""
    ts = datetime(2026, 1, 1, 20, 0, 0)
    e1 = _make_event(title="Show X", timestamp=ts, profile="alice")
    e2 = _make_event(title="Show X", timestamp=ts, profile="bob")
    deduped, dup_count = _dedup_events([e1, e2])
    assert len(deduped) == 1
    assert dup_count == 1


def test_dedup_events_order_independence():
    """Regardless of input order, the dedup count and final set are the same."""
    ts = datetime(2026, 3, 10, 18, 0, 0)
    shared = _make_event(title="Shared", timestamp=ts, profile="user")
    unique_a = _make_event(title="Only A", timestamp=datetime(2026, 1, 1), profile="user")
    unique_b = _make_event(title="Only B", timestamp=datetime(2026, 2, 1), profile="user")

    order1, dups1 = _dedup_events([shared, unique_a, shared, unique_b])
    order2, dups2 = _dedup_events([unique_b, shared, unique_a, shared])

    assert dups1 == dups2 == 1
    titles1 = {e.title for e in order1}
    titles2 = {e.title for e in order2}
    assert titles1 == titles2 == {"Shared", "Only A", "Only B"}


def test_dedup_events_no_false_dedup_different_timestamps():
    """Events with different timestamps are NOT deduplicated."""
    e1 = _make_event(title="Show Z", timestamp=datetime(2026, 1, 1, 20, 0, 0))
    e2 = _make_event(title="Show Z", timestamp=datetime(2026, 1, 2, 20, 0, 0))
    deduped, dup_count = _dedup_events([e1, e2])
    assert len(deduped) == 2
    assert dup_count == 0


def test_dedup_events_no_false_dedup_different_duration():
    """Events with different watch durations are NOT deduplicated."""
    ts = datetime(2026, 5, 1, 10, 0, 0)
    e1 = _make_event(title="Film", content_type="movie", series_name="Film",
                     duration_secs=3600, timestamp=ts)
    e2 = _make_event(title="Film", content_type="movie", series_name="Film",
                     duration_secs=5400, timestamp=ts)
    deduped, dup_count = _dedup_events([e1, e2])
    assert len(deduped) == 2
    assert dup_count == 0


def test_dedup_events_empty_list():
    deduped, dup_count = _dedup_events([])
    assert deduped == []
    assert dup_count == 0


# ---------------------------------------------------------------------------
# Setup ingest: multiple files merge into one provider snapshot
# ---------------------------------------------------------------------------

def test_ingest_two_paths_for_one_provider_merges_events(monkeypatch, tmp_path):
    """Configuring two paths for a provider parses both and merges their events."""
    import config
    import recommender.setup as setup

    ts1 = datetime(2026, 1, 1, 20, 0, 0)
    ts2 = datetime(2026, 2, 1, 20, 0, 0)

    events_file1 = [_make_event(title="File1 Show", timestamp=ts1)]
    events_file2 = [_make_event(title="File2 Show", timestamp=ts2)]

    call_order = []

    def fake_parser(path):
        call_order.append(path)
        if "file1" in path:
            return events_file1
        return events_file2

    monkeypatch.setattr(config, "PLATFORM_PATHS", {
        "netflix": ["/fake/file1.zip", "/fake/file2.zip"],
        "prime": [],
        "apple_tv": [],
    })
    monkeypatch.setattr(config, "MANUAL_TV_PATH", None)
    monkeypatch.setattr(config, "MANUAL_MOVIES_PATH", None)
    monkeypatch.setattr(config, "EVENT_DB_PATH", str(tmp_path / "streamline.db"))
    monkeypatch.setattr(setup, "_PLATFORM_PARSERS", [("netflix", fake_parser)])
    monkeypatch.setattr(setup, "_compute_file_sha256", lambda _path: "fakesha256")

    setup.ingest_providers(fail_on_error=True)

    # Both paths were parsed
    assert "/fake/file1.zip" in call_order
    assert "/fake/file2.zip" in call_order

    # Both events are persisted
    from recommender.event_store import load_events
    db_path = str(tmp_path / "streamline.db")
    loaded = load_events(db_path)
    titles = {e.title for e in loaded}
    assert "File1 Show" in titles
    assert "File2 Show" in titles


# ---------------------------------------------------------------------------
# Setup ingest: dedup result is order-independent
# ---------------------------------------------------------------------------

def test_ingest_dedup_is_order_independent(monkeypatch, tmp_path):
    """Two event lists with overlapping events produce the same dedup count regardless of order."""
    import config
    import recommender.setup as setup
    from recommender.event_store import load_events, init_db

    ts_shared = datetime(2026, 3, 10, 12, 0, 0)
    shared = _make_event(title="Shared Show", timestamp=ts_shared)
    unique_a = _make_event(title="Unique A", timestamp=datetime(2026, 1, 1))
    unique_b = _make_event(title="Unique B", timestamp=datetime(2026, 2, 1))

    def run_ingest(file1_events, file2_events, db_path):
        def fake_parser(path):
            if "file1" in path:
                return file1_events
            return file2_events

        monkeypatch.setattr(config, "PLATFORM_PATHS", {
            "netflix": ["/fake/file1.zip", "/fake/file2.zip"],
            "prime": [],
            "apple_tv": [],
        })
        monkeypatch.setattr(config, "MANUAL_TV_PATH", None)
        monkeypatch.setattr(config, "MANUAL_MOVIES_PATH", None)
        monkeypatch.setattr(config, "EVENT_DB_PATH", str(db_path))
        monkeypatch.setattr(setup, "_PLATFORM_PARSERS", [("netflix", fake_parser)])
        monkeypatch.setattr(setup, "_compute_file_sha256", lambda _path: "sha")
        setup.ingest_providers(fail_on_error=True)
        return {e.title for e in load_events(str(db_path))}

    db1 = tmp_path / "db1.db"
    db2 = tmp_path / "db2.db"

    # Order 1: file1=[shared, unique_a], file2=[shared, unique_b]
    titles1 = run_ingest([shared, unique_a], [shared, unique_b], db1)

    # Order 2: file1=[shared, unique_b], file2=[unique_a, shared]
    titles2 = run_ingest([shared, unique_b], [unique_a, shared], db2)

    assert titles1 == titles2 == {"Shared Show", "Unique A", "Unique B"}


# ---------------------------------------------------------------------------
# Setup ingest: one bad file aborts that provider without deleting previous data
# ---------------------------------------------------------------------------

def test_ingest_bad_file_aborts_provider_preserves_previous_snapshot(monkeypatch, tmp_path):
    """A bad file on re-ingest fails gracefully; prior provider snapshot is intact."""
    import config
    import recommender.setup as setup
    from recommender.event_store import load_events

    db_path = str(tmp_path / "streamline.db")

    # --- Step 1: successful first ingest with one good file ---
    good_event = _make_event(title="Good Show", timestamp=datetime(2026, 1, 1))

    monkeypatch.setattr(config, "PLATFORM_PATHS", {
        "netflix": ["/fake/good.zip"],
        "prime": [],
        "apple_tv": [],
    })
    monkeypatch.setattr(config, "MANUAL_TV_PATH", None)
    monkeypatch.setattr(config, "MANUAL_MOVIES_PATH", None)
    monkeypatch.setattr(config, "EVENT_DB_PATH", db_path)
    monkeypatch.setattr(setup, "_PLATFORM_PARSERS", [("netflix", lambda _path: [good_event])])
    monkeypatch.setattr(setup, "_compute_file_sha256", lambda _path: "sha_good")

    setup.ingest_providers(fail_on_error=False)

    loaded_after_first = {e.title for e in load_events(db_path)}
    assert "Good Show" in loaded_after_first

    # --- Step 2: reconfigure with one good + one bad file; bad file raises ---
    def flaky_parser(path):
        if "bad" in path:
            raise ValueError("Corrupt export file")
        return [good_event]

    monkeypatch.setattr(config, "PLATFORM_PATHS", {
        "netflix": ["/fake/good.zip", "/fake/bad.zip"],
        "prime": [],
        "apple_tv": [],
    })
    monkeypatch.setattr(setup, "_PLATFORM_PARSERS", [("netflix", flaky_parser)])
    monkeypatch.setattr(setup, "_compute_file_sha256", lambda _path: "sha_v2")

    # Should fail gracefully (not crash the process with fail_on_error=False)
    with pytest.raises(SystemExit):
        setup.ingest_providers(fail_on_error=True)

    # Previous snapshot is still intact
    loaded_after_fail = {e.title for e in load_events(db_path)}
    assert "Good Show" in loaded_after_fail


# ---------------------------------------------------------------------------
# Runtime fallback: load_platform_events_from_exports handles multi-path config
# ---------------------------------------------------------------------------

def test_load_platform_events_from_exports_multi_path(monkeypatch, tmp_path):
    """load_platform_events_from_exports returns merged deduped events from multiple paths."""
    import config
    import recommender.setup as setup

    ts_shared = datetime(2026, 6, 1, 12, 0, 0)
    shared = _make_event(title="Shared", timestamp=ts_shared)
    file1_unique = _make_event(title="File1 Only", timestamp=datetime(2026, 1, 1))
    file2_unique = _make_event(title="File2 Only", timestamp=datetime(2026, 2, 1))

    def fake_parser(path):
        if "file1" in path:
            return [shared, file1_unique]
        return [shared, file2_unique]

    monkeypatch.setattr(config, "PLATFORM_PATHS", {
        "netflix": ["/fake/file1.zip", "/fake/file2.zip"],
        "prime": [],
        "apple_tv": [],
    })
    monkeypatch.setattr(setup, "_PLATFORM_PARSERS", [("netflix", fake_parser)])
    monkeypatch.setattr(setup, "_compute_file_sha256", lambda _path: "sha")

    events = setup.load_platform_events_from_exports(fail_on_error=False)

    titles = {e.title for e in events}
    assert titles == {"Shared", "File1 Only", "File2 Only"}
    # Shared appears only once (deduped)
    assert len([e for e in events if e.title == "Shared"]) == 1


# ---------------------------------------------------------------------------
# Event store migration: user-store tables preserved
# ---------------------------------------------------------------------------

def test_init_db_migration_preserves_user_store_tables(tmp_path):
    """After schema migration, saved_titles and title_ratings rows survive."""
    from recommender.event_store import init_db

    db_path = str(tmp_path / "test.db")

    # Bootstrap legacy schema with user-store tables and data
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
        CREATE TABLE saved_titles (
            id    INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL
        );
        CREATE TABLE title_ratings (
            id     INTEGER PRIMARY KEY AUTOINCREMENT,
            title  TEXT NOT NULL,
            rating TEXT NOT NULL
        );
        INSERT INTO saved_titles (title) VALUES ('Watchlisted Show');
        INSERT INTO title_ratings (title, rating) VALUES ('Rated Show', 'liked');
    """)
    conn.close()

    # Migration must succeed
    init_db(db_path)

    conn = sqlite3.connect(db_path)

    # New schema present
    cols = {row[1] for row in conn.execute("PRAGMA table_info(imports)").fetchall()}
    assert "source_manifest_json" in cols

    # saved_titles rows preserved
    saved = conn.execute("SELECT title FROM saved_titles").fetchall()
    assert ("Watchlisted Show",) in saved

    # title_ratings rows preserved
    ratings = conn.execute("SELECT title, rating FROM title_ratings").fetchall()
    assert ("Rated Show", "liked") in ratings

    conn.close()


# ---------------------------------------------------------------------------
# Runtime fallback: all-or-nothing per provider on bad file
# ---------------------------------------------------------------------------

def test_load_platform_events_from_exports_bad_file_skips_provider(monkeypatch):
    """If one file for a provider fails to parse, that provider is entirely skipped."""
    import config
    from recommender import setup

    good_event = _make_event(title="Good Event", timestamp=datetime(2026, 1, 1))

    def bad_parser(path):
        if "bad" in path:
            raise FileNotFoundError(f"Not found: {path}")
        return [good_event]

    monkeypatch.setattr(config, "PLATFORM_PATHS", {
        "netflix": ["/fake/good.zip", "/fake/bad.zip"],
        "prime": [],
        "apple_tv": [],
    })
    monkeypatch.setattr(setup, "_PLATFORM_PARSERS", [("netflix", bad_parser)])
    monkeypatch.setattr(setup, "_compute_file_sha256", lambda _path: "sha")

    events = setup.load_platform_events_from_exports(fail_on_error=False)

    # Netflix should be entirely skipped, no partial results
    assert events == []
