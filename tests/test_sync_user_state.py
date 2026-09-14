"""Tests for tools/sync_user_state.py: status, pull, push with a baseline and a staleness check."""

import json
import shutil
from pathlib import Path

import pytest

from recommender.user_store import (
    add_to_archive, init_db, rate_title, remove_saved_title, save_title,
)
from tools import sync_user_state as sync
from tools.sync_user_state import DirTransport, StaleRemote, Sync, diff, snapshot


def _history(path: Path, *entries):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps([
        {"query": q, "timestamp": ts, "provider": "anthropic"} for q, ts in entries
    ]))


def _site(root: Path) -> tuple[Path, Path]:
    """Create one Streamline install layout under root; return (db, history)."""
    db = root / "data" / "streamline.db"
    history = root / "recommender" / "cache" / "query_history.json"
    db.parent.mkdir(parents=True, exist_ok=True)
    init_db(str(db))
    _history(history, ("first query", "2026-09-01T00:00:00+00:00"))
    return db, history


@pytest.fixture
def sites(tmp_path):
    """A local install, a fake remote install, and a Sync wired between them."""
    local_root = tmp_path / "local"
    remote_root = tmp_path / "remote"
    ldb, lhist = _site(local_root)
    rdb, rhist = _site(remote_root)
    transport = DirTransport(remote_root)
    s = Sync(root=local_root, base_dir=local_root / "data" / "sync", transport=transport)
    return {"local_root": local_root, "remote_root": remote_root, "ldb": ldb, "lhist": lhist,
            "rdb": rdb, "rhist": rhist, "sync": s, "transport": transport}


# ── Snapshot and diff ─────────────────────────────────────────────────────────

def test_snapshot_is_logical_not_byte_level(tmp_path):
    db, hist = _site(tmp_path / "a")
    save_title(str(db), "Show A", "tv", tmdb_id=1)
    before = snapshot(db, hist)
    # A vacuum rewrites the file but changes nothing logical.
    import sqlite3
    sqlite3.connect(str(db)).execute("VACUUM")
    assert snapshot(db, hist) == before


def test_snapshot_ignores_row_ids_and_keys_by_identity(tmp_path):
    a_db, a_hist = _site(tmp_path / "a")
    b_db, b_hist = _site(tmp_path / "b")
    # Same logical rows inserted in different order get different autoincrement ids.
    save_title(str(a_db), "Show A", "tv", tmdb_id=1)
    save_title(str(a_db), "Show B", "tv", tmdb_id=2)
    save_title(str(b_db), "Show B", "tv", tmdb_id=2)
    save_title(str(b_db), "Show A", "tv", tmdb_id=1)
    sa, sb = snapshot(a_db, a_hist), snapshot(b_db, b_hist)
    # Timestamps differ by microseconds; compare keys and non-time fields.
    assert set(sa["saved_titles"]) == set(sb["saved_titles"])
    assert all("id" not in row for row in sa["saved_titles"].values())


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


def test_diff_ignores_timestamp_only_changes_but_reports_them_as_when(tmp_path):
    a_db, a_hist = _site(tmp_path / "a")
    b_db, b_hist = _site(tmp_path / "b")
    rate_title(str(a_db), "Rated", "movie", "more", tmdb_id=3)
    rate_title(str(b_db), "Rated", "movie", "more", tmdb_id=3)
    d = diff(snapshot(a_db, a_hist), snapshot(b_db, b_hist))
    assert d.empty


def test_describe_uses_central_time(tmp_path):
    a_db, a_hist = _site(tmp_path / "a")
    b_db, b_hist = _site(tmp_path / "b")
    add_to_archive(str(b_db), "Watched Tonight", "tv", tmdb_id=9)
    import sqlite3
    sqlite3.connect(str(b_db)).execute(
        "update manual_archive_entries set watched_at='2026-09-14T01:46:52+00:00'").connection.commit()
    d = diff(snapshot(a_db, a_hist), snapshot(b_db, b_hist))
    text = "\n".join(sync.describe(d))
    assert "Watched Tonight" in text
    assert "2026-09-13 20:46 CDT" in text


# ── status ────────────────────────────────────────────────────────────────────

def test_status_without_baseline_shows_both_sides_and_says_no_baseline(sites, capsys):
    save_title(str(sites["ldb"]), "Only Local", "tv", tmdb_id=1)
    save_title(str(sites["rdb"]), "Only Remote", "tv", tmdb_id=2)
    report = sites["sync"].status()
    assert [c.title for c in report.local_vs_remote.removed] == ["Only Local"]
    assert [c.title for c in report.local_vs_remote.added] == ["Only Remote"]
    assert report.baseline is None
    text = sync.render_status(report)
    assert "no baseline" in text.lower()
    assert "Only Local" in text and "Only Remote" in text


def test_status_with_baseline_attributes_changes_to_a_side(sites):
    s = sites["sync"]
    save_title(str(sites["rdb"]), "Shared", "tv", tmdb_id=1)
    s.pull()   # local == remote == baseline
    remove_saved_title(str(sites["rdb"]), "Shared", "tv", tmdb_id=1)   # server removed it
    rate_title(str(sites["ldb"]), "Local Film", "movie", "more", tmdb_id=5)   # Mac rated something
    report = s.status()
    assert [c.title for c in report.remote_since_base.removed] == ["Shared"]
    assert [c.title for c in report.local_since_base.added] == ["Local Film"]
    text = sync.render_status(report)
    assert "server" in text.lower() and "Shared" in text
    assert "local" in text.lower() and "Local Film" in text
    assert "both" in text.lower()


# ── pull ──────────────────────────────────────────────────────────────────────

def test_pull_replaces_local_with_remote_backs_up_and_saves_baseline(sites):
    s = sites["sync"]
    save_title(str(sites["ldb"]), "Stale Local", "tv", tmdb_id=1)
    save_title(str(sites["rdb"]), "Fresh Remote", "tv", tmdb_id=2)
    _history(sites["rhist"], ("first query", "2026-09-01T00:00:00+00:00"), ("remote search", "2026-09-05T00:00:00+00:00"))

    s.pull()

    local = snapshot(sites["ldb"], sites["lhist"])
    assert [r["title"] for r in local["saved_titles"].values()] == ["Fresh Remote"]
    assert any(e["query"] == "remote search" for e in local["query_history"].values())
    backups = list(sites["ldb"].parent.glob("streamline.db.bak-*"))
    assert len(backups) == 1
    assert "Stale Local" in str(snapshot(backups[0], sites["lhist"])["saved_titles"])
    assert (s.base_dir / "streamline.db").exists() and (s.base_dir / "query_history.json").exists()
    assert snapshot(s.base_dir / "streamline.db", s.base_dir / "query_history.json") == local
    assert list(sites["lhist"].parent.glob("query_history.json.bak-*"))


def test_pull_never_touches_the_remote(sites):
    s = sites["sync"]
    save_title(str(sites["rdb"]), "Remote", "tv", tmdb_id=2)
    before = snapshot(sites["rdb"], sites["rhist"])
    s.pull()
    assert snapshot(sites["rdb"], sites["rhist"]) == before
    assert sites["transport"].puts == [] and sites["transport"].restarts == 0


# ── push ──────────────────────────────────────────────────────────────────────

def test_push_refuses_without_a_baseline(sites):
    with pytest.raises(StaleRemote) as info:
        sites["sync"].push()
    assert "baseline" in str(info.value).lower()
    assert sites["transport"].puts == []


def test_push_after_pull_copies_local_up_backs_up_remote_restarts_and_moves_baseline(sites):
    s = sites["sync"]
    save_title(str(sites["rdb"]), "Shared", "tv", tmdb_id=1)
    s.pull()
    rate_title(str(sites["ldb"]), "Local Film", "movie", "more", tmdb_id=5)
    _history(sites["lhist"], ("first query", "2026-09-01T00:00:00+00:00"), ("local search", "2026-09-06T00:00:00+00:00"))

    s.push()

    remote = snapshot(sites["rdb"], sites["rhist"])
    assert [r["title"] for r in remote["title_ratings"].values()] == ["Local Film"]
    assert any(e["query"] == "local search" for e in remote["query_history"].values())
    assert sites["transport"].restarts == 1
    assert list(sites["rdb"].parent.glob("streamline.db.predeploy-*")), "remote backed up before overwrite"
    assert snapshot(s.base_dir / "streamline.db", s.base_dir / "query_history.json") == remote


def test_push_refuses_when_remote_moved_since_baseline(sites):
    """Tonight's case: synced, then the server was used. Push must not undo that."""
    s = sites["sync"]
    save_title(str(sites["rdb"]), "The Day of the Jackal", "tv", tmdb_id=222766)
    s.pull()
    remove_saved_title(str(sites["rdb"]), "The Day of the Jackal", "tv", tmdb_id=222766)
    add_to_archive(str(sites["rdb"]), "The Day of the Jackal", "tv", tmdb_id=222766)

    with pytest.raises(StaleRemote) as info:
        s.push()
    msg = str(info.value)
    assert "The Day of the Jackal" in msg
    assert "pull" in msg.lower()
    assert sites["transport"].puts == [] and sites["transport"].restarts == 0
    # Baseline unchanged, so the next attempt sees the same refusal.
    assert [r["title"] for r in snapshot(s.base_dir / "streamline.db", s.base_dir / "query_history.json")["saved_titles"].values()] == ["The Day of the Jackal"]


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
    remote = snapshot(sites["rdb"], sites["rhist"])
    assert [r["title"] for r in remote["saved_titles"].values()] == ["Local Only"]
    backup = next(sites["rdb"].parent.glob("streamline.db.predeploy-*"))
    assert "Remote Only" in str(snapshot(backup, sites["rhist"])["saved_titles"])


def test_push_with_nothing_to_push_is_a_noop(sites):
    s = sites["sync"]
    s.pull()
    result = s.push()
    assert result.noop is True
    assert sites["transport"].puts == [] and sites["transport"].restarts == 0


def test_failed_put_leaves_baseline_untouched(sites):
    s = sites["sync"]
    s.pull()
    base_before = snapshot(s.base_dir / "streamline.db", s.base_dir / "query_history.json")
    save_title(str(sites["ldb"]), "Local Only", "tv", tmdb_id=2)
    sites["transport"].fail_put = True
    with pytest.raises(RuntimeError):
        s.push()
    assert snapshot(s.base_dir / "streamline.db", s.base_dir / "query_history.json") == base_before
    assert sites["transport"].restarts == 0


# ── CLI ───────────────────────────────────────────────────────────────────────

def test_cli_status_exit_codes(sites, monkeypatch, capsys):
    s = sites["sync"]
    monkeypatch.setattr(sync, "_build_sync", lambda args: s)
    assert sync.main(["status"]) == 0
    save_title(str(sites["rdb"]), "Remote Only", "tv", tmdb_id=1)
    assert sync.main(["status"]) == 1, "differences exit 1 so scripts can notice"
    out = capsys.readouterr().out
    assert "Remote Only" in out


def test_cli_push_reports_refusal_without_traceback(sites, monkeypatch, capsys):
    s = sites["sync"]
    monkeypatch.setattr(sync, "_build_sync", lambda args: s)
    s.pull()
    save_title(str(sites["rdb"]), "Remote Only", "tv", tmdb_id=1)
    rc = sync.main(["push"])
    assert rc == 2
    out = capsys.readouterr().out
    assert "Remote Only" in out and "refus" in out.lower()
