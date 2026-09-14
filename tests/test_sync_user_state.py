"""Tests for tools/sync_user_state.py: status, pull, push with a baseline and a compare-and-swap."""

import json
import os
import sqlite3
import stat
import tempfile
from pathlib import Path

import pytest

from recommender import event_store
from recommender.user_store import (
    add_to_archive, init_db, rate_title, remove_saved_title, save_title,
)
from tools import sync_user_state as sync
from tools.sync_user_state import (
    DirTransport, MissingState, StaleRemote, Sync, apply_bundle, backup_snapshot,
    diff, fingerprint, snapshot, snapshot_db,
)

DB_REL = "data/streamline.db"
HIST_REL = "recommender/cache/query_history.json"


def _history(path: Path, *entries):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps([
        {"query": q, "timestamp": ts, "provider": "anthropic"} for q, ts in entries
    ]))


def _site(root: Path) -> tuple:
    """One Streamline install layout under root; return (db, history)."""
    db = root / DB_REL
    history = root / HIST_REL
    db.parent.mkdir(parents=True, exist_ok=True)
    event_store.init_db(str(db))
    init_db(str(db))
    _history(history, ("first query", "2026-09-01T00:00:00+00:00"))
    return db, history


def _seed_watch_events(db: Path, provider: str, count: int) -> None:
    """Insert imported watch history rows, filling NOT NULL columns generically."""
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row

    def dummy(col):
        t = (col["type"] or "").upper()
        return 1 if "INT" in t or "REAL" in t else "x"

    def insert(table, overrides):
        cols = [c for c in conn.execute(f"PRAGMA table_info({table})") if c["name"] != "id"]
        values = {c["name"]: overrides.get(c["name"], dummy(c) if c["notnull"] else None) for c in cols}
        conn.execute(f"INSERT INTO {table} ({', '.join(values)}) VALUES ({', '.join('?' * len(values))})",
                     list(values.values()))
        return conn.execute("SELECT last_insert_rowid()").fetchone()[0]

    import_id = insert("imports", {"provider": provider})
    for i in range(count):
        insert("watch_events", {"provider": provider, "title": f"{provider} event {i}", "content_type": "movie",
                                "import_id": import_id, "timestamp_iso": "2026-01-01T00:00:00+00:00",
                                "source_hash": f"{provider}-{i}"})
    conn.commit()
    conn.close()


def _events(db: Path) -> list:
    conn = sqlite3.connect(str(db))
    try:
        return [r[0] for r in conn.execute("SELECT title FROM watch_events ORDER BY title")]
    finally:
        conn.close()


@pytest.fixture
def sites(tmp_path):
    """A local install, a fake remote install, and a Sync wired between them."""
    local_root = tmp_path / "local"
    remote_root = tmp_path / "remote"
    ldb, lhist = _site(local_root)
    rdb, rhist = _site(remote_root)
    transport = DirTransport(remote_root)
    s = Sync(root=local_root, base_dir=local_root / "data" / "sync", transport=transport,
             db_rel=DB_REL, history_rel=HIST_REL)
    return {"local_root": local_root, "remote_root": remote_root, "ldb": ldb, "lhist": lhist,
            "rdb": rdb, "rhist": rhist, "sync": s, "transport": transport}


# ── Snapshot and diff ─────────────────────────────────────────────────────────

def test_snapshot_is_logical_not_byte_level(tmp_path):
    db, hist = _site(tmp_path / "a")
    save_title(str(db), "Show A", "tv", tmdb_id=1)
    before = snapshot(db, hist)
    sqlite3.connect(str(db)).execute("VACUUM")
    assert snapshot(db, hist) == before


def test_snapshot_ignores_row_ids_and_keys_by_identity(tmp_path):
    a_db, a_hist = _site(tmp_path / "a")
    b_db, b_hist = _site(tmp_path / "b")
    save_title(str(a_db), "Show A", "tv", tmdb_id=1)
    save_title(str(a_db), "Show B", "tv", tmdb_id=2)
    save_title(str(b_db), "Show B", "tv", tmdb_id=2)
    save_title(str(b_db), "Show A", "tv", tmdb_id=1)
    sa, sb = snapshot(a_db, a_hist), snapshot(b_db, b_hist)
    assert set(sa["saved_titles"]) == set(sb["saved_titles"])
    assert all("id" not in row for row in sa["saved_titles"].values())


def test_snapshot_refuses_a_missing_database(tmp_path):
    with pytest.raises(MissingState):
        snapshot_db(tmp_path / "nope.db")
    assert not (tmp_path / "nope.db").exists(), "must not create an empty database"


def test_snapshot_sees_rows_committed_to_the_wal_while_a_connection_is_open(tmp_path):
    db, hist = _site(tmp_path / "a")
    holder = sqlite3.connect(str(db))
    holder.execute("PRAGMA journal_mode = WAL")
    holder.execute("SELECT count(*) FROM saved_titles").fetchone()
    save_title(str(db), "In The WAL", "tv", tmdb_id=1)          # committed by another connection
    assert (db.parent / (db.name + "-wal")).exists(), "scenario requires a live WAL sidecar"
    assert "In The WAL" in str(snapshot_db(db)["saved_titles"])
    out = tmp_path / "snap.db"
    backup_snapshot(db, out)
    assert "In The WAL" in str(snapshot_db(out)["saved_titles"])
    assert not (out.parent / (out.name + "-wal")).exists(), "snapshot is self-contained"
    holder.close()


def test_diff_reports_added_removed_and_changed(tmp_path):
    a_db, a_hist = _site(tmp_path / "a")
    b_db, b_hist = _site(tmp_path / "b")
    save_title(str(a_db), "Kept", "tv", tmdb_id=1)
    save_title(str(a_db), "Removed Later", "tv", tmdb_id=2)
    rate_title(str(a_db), "Rated", "movie", "more", tmdb_id=3)
    save_title(str(b_db), "Kept", "tv", tmdb_id=1)
    save_title(str(b_db), "Added Later", "tv", tmdb_id=4)
    rate_title(str(b_db), "Rated", "movie", "less", tmdb_id=3)
    _history(b_hist, ("first query", "2026-09-01T00:00:00+00:00"), ("second", "2026-09-02T00:00:00+00:00"))

    d = diff(snapshot(a_db, a_hist), snapshot(b_db, b_hist))
    assert [(c.table, c.title) for c in d.added] == [("saved_titles", "Added Later"), ("query_history", "second")]
    assert [(c.table, c.title) for c in d.removed] == [("saved_titles", "Removed Later")]
    assert [(c.table, c.title, c.fields) for c in d.changed] == [("title_ratings", "Rated", {"rating": ("more", "less")})]
    assert not d.empty


def test_diff_ignores_bookkeeping_timestamps(tmp_path):
    a_db, a_hist = _site(tmp_path / "a")
    b_db, b_hist = _site(tmp_path / "b")
    rate_title(str(a_db), "Rated", "movie", "more", tmdb_id=3)
    rate_title(str(b_db), "Rated", "movie", "more", tmdb_id=3)
    assert diff(snapshot(a_db, a_hist), snapshot(b_db, b_hist)).empty


def test_diff_treats_a_rewatch_as_a_change(tmp_path):
    """watched_at orders the archive, so a newer value is user state, not bookkeeping."""
    a_db, a_hist = _site(tmp_path / "a")
    b_db, b_hist = _site(tmp_path / "b")
    for db in (a_db, b_db):
        add_to_archive(str(db), "Seen", "tv", tmdb_id=9)
    conn = sqlite3.connect(str(b_db))
    conn.execute("UPDATE manual_archive_entries SET watched_at = '2026-09-14T01:46:52+00:00'")
    conn.commit()
    d = diff(snapshot(a_db, a_hist), snapshot(b_db, b_hist))
    assert [c.title for c in d.changed] == ["Seen"]
    assert "watched_at" in d.changed[0].fields


def test_describe_uses_central_time(tmp_path):
    a_db, a_hist = _site(tmp_path / "a")
    b_db, b_hist = _site(tmp_path / "b")
    add_to_archive(str(b_db), "Watched Tonight", "tv", tmdb_id=9)
    conn = sqlite3.connect(str(b_db))
    conn.execute("UPDATE manual_archive_entries SET watched_at = '2026-09-14T01:46:52+00:00'")
    conn.commit()
    text = "\n".join(sync.describe(diff(snapshot(a_db, a_hist), snapshot(b_db, b_hist))))
    assert "Watched Tonight" in text and "2026-09-13 20:46 CDT" in text


def test_fingerprint_changes_with_any_user_table_change_and_ignores_events(tmp_path):
    db, hist = _site(tmp_path / "a")
    base = fingerprint(snapshot_db(db))
    _seed_watch_events(db, "netflix", 3)
    assert fingerprint(snapshot_db(db)) == base, "watch events are not user state"
    save_title(str(db), "Show", "tv", tmdb_id=1)
    assert fingerprint(snapshot_db(db)) != base


# ── status ────────────────────────────────────────────────────────────────────

def test_status_without_baseline_shows_both_sides_and_says_no_baseline(sites):
    save_title(str(sites["ldb"]), "Only Local", "tv", tmdb_id=1)
    save_title(str(sites["rdb"]), "Only Remote", "tv", tmdb_id=2)
    report = sites["sync"].status()
    assert [c.title for c in report.local_vs_remote.removed] == ["Only Local"]
    assert [c.title for c in report.local_vs_remote.added] == ["Only Remote"]
    assert report.baseline is None
    text = sync.render_status(report)
    assert "no baseline" in text.lower() and "Only Local" in text and "Only Remote" in text


def test_status_with_baseline_attributes_changes_to_a_side(sites):
    s = sites["sync"]
    save_title(str(sites["rdb"]), "Shared", "tv", tmdb_id=1)
    s.pull()
    remove_saved_title(str(sites["rdb"]), "Shared", "tv", tmdb_id=1)
    rate_title(str(sites["ldb"]), "Local Film", "movie", "more", tmdb_id=5)
    report = s.status()
    assert [c.title for c in report.remote_since_base.removed] == ["Shared"]
    assert [c.title for c in report.local_since_base.added] == ["Local Film"]
    text = sync.render_status(report)
    assert "both" in text.lower() and "Shared" in text and "Local Film" in text


def test_status_treats_a_missing_remote_history_as_empty(sites):
    sites["rhist"].unlink()
    report = sites["sync"].status()
    assert [c.title for c in report.local_vs_remote.removed] == ["first query"]


def test_status_refuses_when_the_local_database_is_missing(sites):
    sites["ldb"].unlink()
    with pytest.raises(MissingState):
        sites["sync"].status()
    assert not sites["ldb"].exists()


def test_temporary_fetches_are_removed(sites):
    tmp_root = Path(tempfile.gettempdir())
    before = {p for p in tmp_root.glob("streamline-sync-*")}
    sites["sync"].status()
    sites["sync"].pull()
    assert {p for p in tmp_root.glob("streamline-sync-*")} == before


# ── pull ──────────────────────────────────────────────────────────────────────

def test_pull_replaces_user_tables_keeps_watch_events_backs_up_and_saves_baseline(sites):
    s = sites["sync"]
    _seed_watch_events(sites["ldb"], "netflix", 4)       # imported locally, must survive
    _seed_watch_events(sites["rdb"], "prime", 2)         # imported on the server, must not arrive
    save_title(str(sites["ldb"]), "Stale Local", "tv", tmdb_id=1)
    save_title(str(sites["rdb"]), "Fresh Remote", "tv", tmdb_id=2)
    _history(sites["rhist"], ("first query", "2026-09-01T00:00:00+00:00"), ("remote search", "2026-09-05T00:00:00+00:00"))

    s.pull()

    local = snapshot(sites["ldb"], sites["lhist"])
    assert [r["title"] for r in local["saved_titles"].values()] == ["Fresh Remote"]
    assert any(e["query"] == "remote search" for e in local["query_history"].values())
    assert _events(sites["ldb"]) == [f"netflix event {i}" for i in range(4)]
    backups = list(sites["ldb"].parent.glob("streamline.db.bak-*"))
    assert len(backups) == 1 and "Stale Local" in str(snapshot_db(backups[0])["saved_titles"])
    assert list(sites["lhist"].parent.glob("query_history.json.bak-*"))
    assert snapshot(s.base_db, s.base_history) == local


def test_pull_never_touches_the_remote(sites):
    s = sites["sync"]
    save_title(str(sites["rdb"]), "Remote", "tv", tmdb_id=2)
    before = snapshot(sites["rdb"], sites["rhist"])
    s.pull()
    assert snapshot(sites["rdb"], sites["rhist"]) == before
    assert sites["transport"].applies == []


def test_pull_with_missing_remote_history_writes_an_empty_local_history(sites):
    sites["rhist"].unlink()
    sites["sync"].pull()
    assert json.loads(sites["lhist"].read_text()) == []


# ── push ──────────────────────────────────────────────────────────────────────

def test_push_refuses_without_a_baseline(sites):
    with pytest.raises(StaleRemote) as info:
        sites["sync"].push()
    assert "baseline" in str(info.value).lower()
    assert sites["transport"].applies == []


def test_push_after_pull_replaces_remote_user_tables_keeps_its_events_and_moves_baseline(sites):
    s = sites["sync"]
    _seed_watch_events(sites["rdb"], "prime", 3)
    save_title(str(sites["rdb"]), "Shared", "tv", tmdb_id=1)
    s.pull()
    rate_title(str(sites["ldb"]), "Local Film", "movie", "more", tmdb_id=5)
    remove_saved_title(str(sites["ldb"]), "Shared", "tv", tmdb_id=1)
    _history(sites["lhist"], ("first query", "2026-09-01T00:00:00+00:00"), ("local search", "2026-09-06T00:00:00+00:00"))

    result = s.push()

    assert result.noop is False
    remote = snapshot(sites["rdb"], sites["rhist"])
    assert [r["title"] for r in remote["title_ratings"].values()] == ["Local Film"]
    assert remote["saved_titles"] == {}, "deletions travel because tables are replaced"
    assert any(e["query"] == "local search" for e in remote["query_history"].values())
    assert _events(sites["rdb"]) == [f"prime event {i}" for i in range(3)]
    assert list(sites["rdb"].parent.glob("streamline.db.predeploy-*")), "remote backed up before overwrite"
    assert snapshot(s.base_db, s.base_history) == remote


def test_push_refuses_when_remote_moved_since_baseline(sites):
    """The Jackal case: synced, then the server was used. Push must not undo that."""
    s = sites["sync"]
    save_title(str(sites["rdb"]), "The Day of the Jackal", "tv", tmdb_id=222766)
    s.pull()
    remove_saved_title(str(sites["rdb"]), "The Day of the Jackal", "tv", tmdb_id=222766)
    add_to_archive(str(sites["rdb"]), "The Day of the Jackal", "tv", tmdb_id=222766)
    with pytest.raises(StaleRemote) as info:
        s.push()
    msg = str(info.value)
    assert "The Day of the Jackal" in msg and "pull" in msg.lower()
    assert sites["transport"].applies == []
    assert [r["title"] for r in snapshot(s.base_db, s.base_history)["saved_titles"].values()] == ["The Day of the Jackal"]


def test_push_refusal_says_pull_is_safe_when_local_is_unchanged(sites):
    s = sites["sync"]
    s.pull()
    save_title(str(sites["rdb"]), "Remote Only", "tv", tmdb_id=1)
    with pytest.raises(StaleRemote) as info:
        s.push()
    assert "nothing changed locally" in str(info.value).lower()


def test_push_refusal_names_both_sides_when_both_changed(sites):
    s = sites["sync"]
    s.pull()
    save_title(str(sites["rdb"]), "Remote Only", "tv", tmdb_id=1)
    save_title(str(sites["ldb"]), "Local Only", "tv", tmdb_id=2)
    with pytest.raises(StaleRemote) as info:
        s.push()
    msg = str(info.value)
    assert "Remote Only" in msg and "Local Only" in msg and "--force" in msg


def test_push_force_overrides_staleness_but_still_backs_up(sites):
    s = sites["sync"]
    s.pull()
    save_title(str(sites["rdb"]), "Remote Only", "tv", tmdb_id=1)
    save_title(str(sites["ldb"]), "Local Only", "tv", tmdb_id=2)
    s.push(force=True)
    assert [r["title"] for r in snapshot(sites["rdb"], sites["rhist"])["saved_titles"].values()] == ["Local Only"]
    backup = next(sites["rdb"].parent.glob("streamline.db.predeploy-*"))
    assert "Remote Only" in str(snapshot_db(backup)["saved_titles"])


def test_push_with_nothing_to_push_is_a_noop(sites):
    s = sites["sync"]
    s.pull()
    assert s.push().noop is True
    assert sites["transport"].applies == []


def test_push_refuses_when_the_local_database_is_missing(sites):
    s = sites["sync"]
    s.pull()
    sites["ldb"].unlink()
    with pytest.raises(MissingState):
        s.push()
    assert sites["transport"].applies == []
    assert not sites["ldb"].exists()


def test_push_with_missing_local_history_empties_the_remote_history(sites):
    s = sites["sync"]
    _history(sites["rhist"], ("first query", "2026-09-01T00:00:00+00:00"), ("remote search", "2026-09-05T00:00:00+00:00"))
    s.pull()
    sites["lhist"].unlink()
    s.push()
    assert json.loads(sites["rhist"].read_text()) == []
    assert s.status().local_vs_remote.empty, "baseline and both sides agree afterwards"


def test_push_is_refused_when_the_server_changes_between_fetch_and_apply(sites):
    """Compare-and-swap: a web request landing mid-push must not be overwritten."""
    s = sites["sync"]
    s.pull()
    save_title(str(sites["ldb"]), "Local Only", "tv", tmdb_id=2)
    sites["transport"].before_apply = lambda: save_title(str(sites["rdb"]), "Landed Mid Push", "tv", tmdb_id=7)
    with pytest.raises(StaleRemote) as info:
        s.push()
    assert "in flight" in str(info.value).lower()
    remote = snapshot(sites["rdb"], sites["rhist"])
    assert [r["title"] for r in remote["saved_titles"].values()] == ["Landed Mid Push"]
    assert "Local Only" not in str(remote["saved_titles"])


def test_failed_apply_leaves_baseline_untouched(sites):
    s = sites["sync"]
    s.pull()
    base_before = snapshot(s.base_db, s.base_history)
    save_title(str(sites["ldb"]), "Local Only", "tv", tmdb_id=2)
    sites["transport"].fail_apply = True
    with pytest.raises(RuntimeError):
        s.push()
    assert snapshot(s.base_db, s.base_history) == base_before


def test_apply_is_atomic_when_the_history_cannot_be_staged(tmp_path):
    """Tables and history change together or not at all."""
    live_db, live_hist = _site(tmp_path / "live")
    bundle_db, bundle_hist = _site(tmp_path / "bundle")
    save_title(str(live_db), "Before", "tv", tmdb_id=1)
    save_title(str(bundle_db), "After", "tv", tmdb_id=2)
    hist_dir = live_hist.parent
    hist_dir.chmod(stat.S_IRUSR | stat.S_IXUSR)      # staging the new history must fail
    try:
        with pytest.raises(OSError):
            apply_bundle(live_db, live_hist, bundle_db, bundle_hist, None, "test")
    finally:
        hist_dir.chmod(stat.S_IRWXU)
    assert [r["title"] for r in snapshot_db(live_db)["saved_titles"].values()] == ["Before"]
    assert json.loads(live_hist.read_text())[0]["query"] == "first query"


def test_apply_refuses_a_column_mismatch(tmp_path):
    live_db, live_hist = _site(tmp_path / "live")
    bundle_db, bundle_hist = _site(tmp_path / "bundle")
    sqlite3.connect(str(bundle_db)).execute("ALTER TABLE saved_titles ADD COLUMN extra TEXT").connection.commit()
    with pytest.raises(RuntimeError) as info:
        apply_bundle(live_db, live_hist, bundle_db, bundle_hist, None, "test")
    assert "column mismatch" in str(info.value)


# ── CLI ───────────────────────────────────────────────────────────────────────

def test_cli_status_exit_codes(sites, monkeypatch, capsys):
    s = sites["sync"]
    monkeypatch.setattr(sync, "_build_sync", lambda args: s)
    assert sync.main(["status"]) == 0
    save_title(str(sites["rdb"]), "Remote Only", "tv", tmdb_id=1)
    assert sync.main(["status"]) == 1
    assert "Remote Only" in capsys.readouterr().out


def test_cli_push_reports_refusal_without_traceback(sites, monkeypatch, capsys):
    s = sites["sync"]
    monkeypatch.setattr(sync, "_build_sync", lambda args: s)
    s.pull()
    save_title(str(sites["rdb"]), "Remote Only", "tv", tmdb_id=1)
    assert sync.main(["push"]) == 2
    out = capsys.readouterr().out
    assert "Remote Only" in out and "refus" in out.lower()


def test_cli_missing_local_db_is_a_refusal(sites, monkeypatch, capsys):
    s = sites["sync"]
    monkeypatch.setattr(sync, "_build_sync", lambda args: s)
    sites["ldb"].unlink()
    assert sync.main(["status"]) == 2
    assert "Refused" in capsys.readouterr().out


def test_cli_requires_a_configured_host(monkeypatch, capsys):
    monkeypatch.setattr(sync.config, "SYNC_HOST", None)
    with pytest.raises(SystemExit) as info:
        sync.main(["status"])
    assert "config.local.yaml" in str(info.value)


def test_helper_snapshot_and_apply_round_trip(tmp_path, monkeypatch):
    """The helper subcommands are what the ssh transport runs on the other machine."""
    root = tmp_path / "remote"
    db, hist = _site(root)
    save_title(str(db), "Remote Row", "tv", tmdb_id=1)
    monkeypatch.setattr(sync, "PROJECT_ROOT", root)
    dest = tmp_path / "snap.db"
    assert sync.main(["helper", "snapshot", DB_REL, str(dest)]) == 0
    assert "Remote Row" in str(snapshot_db(dest)["saved_titles"])

    bundle_db, bundle_hist = _site(tmp_path / "bundle")
    save_title(str(bundle_db), "Pushed Row", "tv", tmdb_id=2)
    expected = fingerprint(snapshot_db(dest))
    assert sync.main(["helper", "apply", DB_REL, HIST_REL, str(bundle_db), str(bundle_hist), expected, "t1"]) == 0
    assert [r["title"] for r in snapshot_db(db)["saved_titles"].values()] == ["Pushed Row"]
    # Same expected fingerprint again is now stale.
    assert sync.main(["helper", "apply", DB_REL, HIST_REL, str(bundle_db), str(bundle_hist), expected, "t2"]) == sync.EXIT_STALE
