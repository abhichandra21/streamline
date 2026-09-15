"""Transport, promotion and refresh for user-state sync.

The server side is exercised through a directory-backed transport, so the whole
flow runs without ssh or a real home server.
"""

import json
import sqlite3

import pytest

from recommender import history as query_history
from recommender import user_store
from tools.sync_diff import State, fingerprint
from tools.sync_state import (
    Cancelled,
    LocalMoved,
    MissingState,
    SchemaMismatch,
    ServerMoved,
    Sync,
    DirTransport,
    read_snapshot,
    write_baseline,
)

# Tables that share the file but are not user state. They must never move.
FOREIGN_SCHEMA = """
CREATE TABLE imports (id INTEGER PRIMARY KEY, provider TEXT);
CREATE TABLE watch_events (id INTEGER PRIMARY KEY, title TEXT, import_id INTEGER);
"""


def make_db(path, *, saved=(), ratings=(), archive=(), tracking=(), history=(),
            foreign=True):
    conn = sqlite3.connect(str(path))
    conn.executescript(user_store._SCHEMA)
    conn.executescript(user_store._INDEXES)
    conn.executescript(query_history._SCHEMA)
    if foreign:
        conn.executescript(FOREIGN_SCHEMA)
        conn.execute("INSERT INTO imports (id, provider) VALUES (1, 'netflix')")
        conn.executemany("INSERT INTO watch_events (title, import_id) VALUES (?, 1)",
                         [(f"event {i}",) for i in range(25)])
    for title, ct, tmdb, status in saved:
        conn.execute("INSERT INTO saved_titles (title, normalized_title, content_type,"
                     " tmdb_id, status, saved_at, updated_at) VALUES (?,?,?,?,?,'t','t')",
                     (title, title.lower(), ct, tmdb, status))
    for title, ct, tmdb, value in ratings:
        conn.execute("INSERT INTO title_ratings (title, normalized_title, content_type,"
                     " tmdb_id, rating, rated_at, updated_at) VALUES (?,?,?,?,?,'t','t')",
                     (title, title.lower(), ct, tmdb, value))
    for title, ct, tmdb, watched_at in archive:
        conn.execute("INSERT INTO manual_archive_entries (title, normalized_title,"
                     " content_type, tmdb_id, watched_at, source) VALUES (?,?,?,?,?,'web')",
                     (title, title.lower(), ct, tmdb, watched_at))
    for tmdb, title, state in tracking:
        conn.execute("INSERT INTO show_tracking (tmdb_id, title, state, created_at,"
                     " updated_at) VALUES (?,?,?,'t','t')", (tmdb, title, state))
    for ts, query in history:
        conn.execute("INSERT INTO query_history (timestamp, entry) VALUES (?,?)",
                     (ts, json.dumps({"timestamp": ts, "query": query})))
    conn.commit()
    conn.close()
    return path


def rows_of(path, table):
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(f"SELECT * FROM {table}")]
    finally:
        conn.close()


def count(path, table):
    conn = sqlite3.connect(str(path))
    try:
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    finally:
        conn.close()


def titles(path, table="saved_titles"):
    return sorted(r["title"] for r in rows_of(path, table))


def build(tmp_path, local_kw=None, server_kw=None, baseline=None,
          chooser=None, confirm=True):
    """A Sync wired to two real databases and a directory transport."""
    local = make_db(tmp_path / "local.db", **(local_kw or {}))
    server = make_db(tmp_path / "server.db", **(server_kw or {}))
    base_path = tmp_path / "baseline.db"
    if baseline is not None:
        write_baseline(base_path, read_snapshot(make_db(tmp_path / "b.src", **baseline)))
    sync = Sync(
        local_db=local,
        baseline_db=base_path,
        transport=DirTransport(server),
        chooser=chooser if chooser is not None else (lambda changes: []),
        confirm=(lambda *a, **k: confirm),
    )
    return sync, local, server


def tick(*wanted):
    """A chooser that ticks the named titles."""
    def chooser(changes):
        return [c for c in changes if c.offered and c.title in wanted]
    return chooser


# ── preflight ────────────────────────────────────────────────────────────────

def test_missing_local_database_is_refused(tmp_path):
    """A missing file must never read as empty state: that would wipe the server."""
    server = make_db(tmp_path / "server.db", saved=[("Dune", "movie", 1, "watchlist")])
    sync = Sync(local_db=tmp_path / "absent.db", baseline_db=tmp_path / "b.db",
                transport=DirTransport(server), chooser=lambda c: [],
                confirm=lambda *a, **k: True)
    with pytest.raises(MissingState):
        sync.run()
    assert titles(server) == ["Dune"]


def test_schema_mismatch_is_refused(tmp_path):
    sync, local, server = build(tmp_path)
    conn = sqlite3.connect(str(server))
    conn.execute("ALTER TABLE saved_titles ADD COLUMN extra TEXT")
    conn.commit()
    conn.close()
    with pytest.raises(SchemaMismatch):
        sync.run()


# ── the imported watch history never moves ───────────────────────────────────

def test_watch_events_and_imports_are_untouched_by_a_full_run(tmp_path):
    sync, local, server = build(
        tmp_path,
        local_kw={"saved": [("Fargo", "tv", None, "watchlist")]},
        server_kw={"saved": [("Dune", "movie", 1, "watchlist")]},
        chooser=tick("Fargo"),
    )
    before = {p: (count(p, "watch_events"), count(p, "imports")) for p in (local, server)}
    sync.run()
    after = {p: (count(p, "watch_events"), count(p, "imports")) for p in (local, server)}
    assert before == after


# ── promotion ────────────────────────────────────────────────────────────────

def test_only_ticked_titles_reach_the_server(tmp_path):
    sync, local, server = build(
        tmp_path,
        local_kw={"saved": [("Fargo", "tv", None, "watchlist"),
                            ("Test Movie", "movie", None, "watchlist")]},
        chooser=tick("Fargo"),
    )
    sync.run()
    assert titles(server) == ["Fargo"]


def test_nothing_ticked_sends_nothing_and_local_matches_the_server(tmp_path):
    sync, local, server = build(
        tmp_path,
        local_kw={"saved": [("Test Movie", "movie", None, "watchlist")]},
        server_kw={"saved": [("Dune", "movie", 1, "watchlist")]},
    )
    sync.run()
    assert titles(server) == ["Dune"]
    assert titles(local) == ["Dune"]


def test_a_local_removal_is_applied_to_the_server_when_ticked(tmp_path):
    shared = {"saved": [("Fargo", "tv", 500, "watchlist")]}
    sync, local, server = build(tmp_path, local_kw={}, server_kw=shared,
                                baseline=shared, chooser=tick("Fargo"))
    sync.run()
    assert titles(server) == []


def test_promoting_a_title_the_server_holds_under_the_other_identity_leaves_one_row(tmp_path):
    """_reconcile_identity can promote a null-tmdb row; both identities must not survive."""
    sync, local, server = build(
        tmp_path,
        local_kw={"saved": [("Fargo", "tv", 12345, "watchlist")]},
        server_kw={"saved": [("Fargo", "tv", None, "watchlist")]},
        chooser=tick("Fargo"),
    )
    sync.run()
    rows = rows_of(server, "saved_titles")
    assert len(rows) == 1
    assert rows[0]["tmdb_id"] == 12345


def test_promoting_a_title_whose_text_changed_does_not_collide_on_its_tmdb_id(tmp_path):
    """The server may hold the same tmdb_id under a different normalized title.

    Deleting only by normalized title would miss it, and the insert would then
    hit the unique index on (content_type, tmdb_id).
    """
    sync, local, server = build(
        tmp_path,
        local_kw={"saved": [("Bear", "tv", 500, "watchlist")]},
        server_kw={"saved": [("The Bear", "tv", 500, "watchlist")]},
        chooser=tick("Bear"),
    )
    sync.run()
    rows = rows_of(server, "saved_titles")
    assert len(rows) == 1
    assert rows[0]["title"] == "Bear"


def test_a_whole_title_moves_together(tmp_path):
    """Marking watched writes three tables; half of it must not land."""
    sync, local, server = build(
        tmp_path,
        local_kw={"archive": [("Sinners", "movie", 7, "2026-09-01")],
                  "ratings": [("Sinners", "movie", 7, "more")]},
        server_kw={"saved": [("Sinners", "movie", 7, "watchlist")]},
        chooser=tick("Sinners"),
    )
    sync.run()
    assert titles(server, "manual_archive_entries") == ["Sinners"]
    assert titles(server, "title_ratings") == ["Sinners"]
    assert titles(server, "saved_titles") == []


# ── staleness ────────────────────────────────────────────────────────────────

def test_server_changing_a_ticked_title_mid_run_refuses_and_writes_nothing(tmp_path):
    sync, local, server = build(
        tmp_path,
        local_kw={"ratings": [("Andor", "tv", 9, "less")]},
        server_kw={"ratings": [("Andor", "tv", 9, "more")]},
        chooser=tick("Andor"),
    )

    def meddle():
        conn = sqlite3.connect(str(server))
        conn.execute("UPDATE title_ratings SET rating = 'neutral' WHERE tmdb_id = 9")
        conn.commit()
        conn.close()

    sync.transport.before_promote = meddle
    with pytest.raises(ServerMoved):
        sync.run()
    assert rows_of(server, "title_ratings")[0]["rating"] == "neutral"
    assert rows_of(local, "title_ratings")[0]["rating"] == "less"


def test_an_unrelated_server_change_does_not_block_the_push(tmp_path):
    """The editor may sit open for minutes against a live app."""
    sync, local, server = build(
        tmp_path,
        local_kw={"saved": [("Fargo", "tv", None, "watchlist")]},
        chooser=tick("Fargo"),
    )

    def meddle():
        conn = sqlite3.connect(str(server))
        conn.execute("INSERT INTO saved_titles (title, normalized_title, content_type,"
                     " tmdb_id, status, saved_at, updated_at)"
                     " VALUES ('Dune','dune','movie',1,'watchlist','t','t')")
        conn.commit()
        conn.close()

    sync.transport.before_promote = meddle
    sync.run()
    assert titles(server) == ["Dune", "Fargo"]


def test_local_changing_during_the_review_aborts_before_any_write(tmp_path):
    """The local web UI can be open in a browser tab while the checklist is in an editor."""
    sync, local, server = build(
        tmp_path,
        local_kw={"saved": [("Fargo", "tv", None, "watchlist")]},
        server_kw={"saved": [("Dune", "movie", 1, "watchlist")]},
    )

    def chooser(changes):
        conn = sqlite3.connect(str(local))
        conn.execute("INSERT INTO saved_titles (title, normalized_title, content_type,"
                     " tmdb_id, status, saved_at, updated_at)"
                     " VALUES ('Late Pick','late pick','tv',77,'watchlist','t','t')")
        conn.commit()
        conn.close()
        return []

    sync.chooser = chooser
    with pytest.raises(LocalMoved):
        sync.run()
    assert titles(server) == ["Dune"]
    assert "Late Pick" in titles(local)


# ── cancelling ───────────────────────────────────────────────────────────────

def test_a_cancelled_review_writes_nothing_on_either_side(tmp_path):
    sync, local, server = build(
        tmp_path,
        local_kw={"saved": [("Fargo", "tv", None, "watchlist")]},
        server_kw={"saved": [("Dune", "movie", 1, "watchlist")]},
    )
    sync.chooser = lambda changes: (_ for _ in ()).throw(Cancelled("editor quit"))
    with pytest.raises(Cancelled):
        sync.run()
    assert titles(server) == ["Dune"]
    assert titles(local) == ["Fargo"]


def test_declining_the_confirmation_writes_nothing(tmp_path):
    sync, local, server = build(
        tmp_path,
        local_kw={"saved": [("Fargo", "tv", None, "watchlist")]},
        server_kw={"saved": [("Dune", "movie", 1, "watchlist")]},
        chooser=tick("Fargo"),
        confirm=False,
    )
    with pytest.raises(Cancelled):
        sync.run()
    assert titles(server) == ["Dune"]
    assert titles(local) == ["Fargo"]


def test_a_failed_promotion_leaves_the_server_exactly_as_it_was(tmp_path):
    sync, local, server = build(
        tmp_path,
        local_kw={"saved": [("Fargo", "tv", None, "watchlist"),
                            ("Sinners", "movie", 7, "watchlist")]},
        chooser=tick("Fargo", "Sinners"),
    )
    sync.transport.fail_promote = True
    with pytest.raises(RuntimeError):
        sync.run()
    assert titles(server) == []


# ── refresh ──────────────────────────────────────────────────────────────────

def test_local_ends_up_matching_the_server(tmp_path):
    sync, local, server = build(
        tmp_path,
        local_kw={"saved": [("Test Movie", "movie", None, "watchlist")]},
        server_kw={"saved": [("Dune", "movie", 1, "watchlist")],
                   "ratings": [("Andor", "tv", 9, "more")]},
        chooser=tick(),
    )
    sync.run()
    assert fingerprint(read_snapshot(local).rows) == fingerprint(read_snapshot(server).rows)


def test_history_arrives_in_the_servers_order(tmp_path):
    """Order is defined by row id, and timestamps are not unique."""
    same = "2026-09-01T00:00:00+00:00"
    sync, local, server = build(
        tmp_path,
        local_kw={"history": [(same, "local search")]},
        server_kw={"history": [(same, "first"), (same, "second"), (same, "third")]},
    )
    sync.run()
    got = [json.loads(r["entry"])["query"] for r in rows_of(local, "query_history")]
    assert got == ["first", "second", "third"]


def test_history_reads_back_identically_through_the_history_module(tmp_path, monkeypatch):
    """The contract that matters: load() returns the same sequence on both sides."""
    # load() consults this module global and would import the real install's
    # legacy file into these test databases. Same guard as tests/test_history.py.
    monkeypatch.setattr(query_history, "LEGACY_HISTORY_PATH",
                        tmp_path / "absent_query_history.json")
    sync, local, server = build(
        tmp_path,
        local_kw={"history": [("2026-08-01T00:00:00+00:00", "stale local")]},
        server_kw={"history": [(f"2026-09-0{i}T00:00:00+00:00", f"search {i}")
                               for i in range(1, 6)]},
    )
    sync.run()
    assert (query_history.load(db_path=str(local))
            == query_history.load(db_path=str(server)))


def test_local_searches_are_never_sent_up(tmp_path):
    sync, local, server = build(
        tmp_path,
        local_kw={"history": [("t1", "local only search")]},
        chooser=lambda changes: [c for c in changes if c.offered],
    )
    sync.run()
    assert rows_of(server, "query_history") == []


def test_the_baseline_is_recorded_so_the_next_run_can_classify(tmp_path):
    sync, local, server = build(
        tmp_path, server_kw={"saved": [("Dune", "movie", 1, "watchlist")]})
    sync.run()
    assert sync.baseline_db.exists()
    assert titles(sync.baseline_db) == ["Dune"]


# ── first run ────────────────────────────────────────────────────────────────

def test_first_run_offers_differences_instead_of_discarding_them(tmp_path):
    """There is no baseline on a fresh install, and that is where local work sits."""
    seen = []
    sync, local, server = build(
        tmp_path,
        local_kw={"saved": [("Fargo", "tv", None, "watchlist")]},
        server_kw={"saved": [("Dune", "movie", 1, "watchlist")]},
        chooser=lambda changes: (seen.extend(changes), [c for c in changes if c.title == "Fargo"])[1],
    )
    sync.run()
    assert {c.state for c in seen} == {State.UNCLASSIFIED}
    assert titles(server) == ["Dune", "Fargo"]


# ── status ───────────────────────────────────────────────────────────────────

def test_status_reports_differences_without_writing(tmp_path):
    sync, local, server = build(
        tmp_path,
        local_kw={"saved": [("Fargo", "tv", None, "watchlist")]},
        server_kw={"saved": [("Dune", "movie", 1, "watchlist")]},
    )
    changes = sync.status()
    assert sorted(c.title for c in changes) == ["Dune", "Fargo"]
    assert titles(local) == ["Fargo"]
    assert titles(server) == ["Dune"]
    assert not sync.baseline_db.exists()
