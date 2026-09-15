"""Move user state between this install and the live app on the home server.

The server is the source of truth and local is a mirror. Local changes travel
up only when the operator ticks them in a checklist; the refresh then replaces
local's five tables with the server's. The server never gets a wholesale table
replacement, which is what keeps a sync from damaging the live app.

    ./recommend-sync            review, promote what you tick, refresh local
    ./recommend-sync --status   show what differs, write nothing

Imported watch history (watch_events, imports) shares the same SQLite file and
is not user state. It is never read for comparison and never written.

See docs/superpowers/specs/2026-09-14-user-state-sync-design.md.
"""

import argparse
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import config  # noqa: E402
from recommender import history as query_history  # noqa: E402
from recommender import user_store  # noqa: E402
from tools.sync_diff import (  # noqa: E402
    HISTORY_TABLE,
    USER_TABLES,
    ChecklistEdited,
    State,
    build_footprints,
    classify,
    fingerprint,
    normalize_title,
    parse_checklist,
    render_checklist,
)

SYNC_TABLES = (*USER_TABLES, HISTORY_TABLE)
BASELINE_REL = "data/sync/baseline.db"
EXIT_DIFFERS = 1
EXIT_REFUSED = 2


class MissingState(Exception):
    """A database that must exist does not, so nothing can be trusted."""


class SchemaMismatch(Exception):
    """The two sides disagree about a table's columns."""


class ServerMoved(Exception):
    """A ticked title changed on the server mid-run. Nothing was written."""


class LocalMoved(Exception):
    """Local state changed during the review. Nothing was written."""


class Cancelled(Exception):
    """The operator backed out. Nothing was written."""


class TransportError(RuntimeError):
    """The server could not be reached, or the helper failed there."""


@dataclass
class Snapshot:
    rows: dict = field(default_factory=dict)      # table -> list of row dicts
    columns: dict = field(default_factory=dict)   # table -> list of column names


# ── reading ──────────────────────────────────────────────────────────────────

def _connect_ro(db_path) -> sqlite3.Connection:
    """Read-only, which still sees rows committed to a WAL file."""
    path = Path(db_path)
    if not path.is_file():
        raise MissingState(
            f"Database not found: {path}. Refusing to treat a missing file as empty state."
        )
    conn = sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True, timeout=30.0)
    conn.row_factory = sqlite3.Row
    return conn


def _columns(conn: sqlite3.Connection, table: str) -> list:
    return [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]


def _read(conn: sqlite3.Connection) -> Snapshot:
    snap = Snapshot()
    for table in SYNC_TABLES:
        cols = _columns(conn, table)
        snap.columns[table] = cols
        if not cols:
            snap.rows[table] = []
            continue
        # History order is defined by row id, not by timestamp, and timestamps
        # are not unique. Read it in id order so the copy can reproduce it.
        order = " ORDER BY id ASC" if table == HISTORY_TABLE else ""
        snap.rows[table] = [dict(r) for r in conn.execute(f"SELECT * FROM {table}{order}")]
    return snap


def read_snapshot(db_path) -> Snapshot:
    conn = _connect_ro(db_path)
    try:
        return _read(conn)
    finally:
        conn.close()


def check_schemas(local: Snapshot, server: Snapshot) -> None:
    """Refuse when two existing tables disagree about their columns.

    A table missing entirely is not a disagreement. user_store and history
    create their tables lazily on first use, so a checkout where no search has
    run yet has no query_history table at all. Treating that as a code
    mismatch would refuse the first sync on exactly the install that most
    needs one.
    """
    for table in SYNC_TABLES:
        here, there = local.columns.get(table) or [], server.columns.get(table) or []
        if here and there and here != there:
            raise SchemaMismatch(
                f"{table}: this install has {here} and the server has {there}. "
                "Deploy the same code to both sides first."
            )


def ensure_tables(db_path) -> None:
    """Create any of the five tables this install has not created yet.

    Idempotent: every statement is CREATE ... IF NOT EXISTS, and the schemas
    come from the application's own modules so the sync cannot invent a
    different shape. Only ever called on the local database; the server's
    schema is its own business.
    """
    conn = _connect_rw(db_path)
    try:
        conn.executescript(user_store._SCHEMA)
        conn.executescript(user_store._INDEXES)
        conn.executescript(query_history._SCHEMA)
    finally:
        conn.close()


# ── writing ──────────────────────────────────────────────────────────────────

def _connect_rw(db_path) -> sqlite3.Connection:
    path = Path(db_path)
    if not path.is_file():
        raise MissingState(f"Database not found: {path}.")
    conn = sqlite3.connect(str(path), timeout=30.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    return conn


def backup_database(src, dest) -> None:
    """Self-contained copy via SQLite's backup API, so WAL contents are included."""
    src, dest = Path(src), Path(dest)
    if not src.is_file():
        raise MissingState(f"Database not found: {src}.")
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        dest.unlink()
    source = sqlite3.connect(str(src), timeout=30.0)
    target = sqlite3.connect(str(dest))
    try:
        source.backup(target)
        # A plain file, so it can be copied and opened without sidecars.
        target.execute("PRAGMA journal_mode = DELETE")
    finally:
        target.close()
        source.close()


def _insert(conn: sqlite3.Connection, table: str, row: dict) -> None:
    payload = {k: v for k, v in row.items() if k != "id"}
    names = ", ".join(payload)
    holes = ", ".join("?" * len(payload))
    conn.execute(f"INSERT INTO {table} ({names}) VALUES ({holes})", tuple(payload.values()))


def _tmdb_ids(footprint_rows: dict) -> set:
    ids = set()
    for rows in footprint_rows.values():
        for row in rows:
            if row.get("tmdb_id") is not None:
                ids.add(row["tmdb_id"])
    return ids


def _delete_title(conn: sqlite3.Connection, key, tmdb_ids: set) -> None:
    """Remove a title from the four tables under both of its possible identities.

    A title can exist twice in one table -- once with a tmdb_id and once
    without -- because the unique indexes are partial. Deleting by normalized
    title and by every known id means the insert that follows cannot leave a
    second identity behind.
    """
    content_type, normalized = key
    for table in ("saved_titles", "title_ratings", "manual_archive_entries"):
        conn.execute(
            f"DELETE FROM {table} WHERE content_type = ? AND normalized_title = ?",
            (content_type, normalized),
        )
        for tmdb_id in tmdb_ids:
            conn.execute(f"DELETE FROM {table} WHERE tmdb_id = ?", (tmdb_id,))

    # show_tracking has no normalized_title column, so match on the derived one.
    doomed = [r["tmdb_id"] for r in conn.execute("SELECT tmdb_id, title FROM show_tracking")
              if normalize_title(r["title"] or "") == normalized]
    for tmdb_id in set(doomed) | tmdb_ids:
        conn.execute("DELETE FROM show_tracking WHERE tmdb_id = ?", (tmdb_id,))


def _cas_view(footprint) -> dict:
    """A JSON-stable view of a footprint, for comparing across a transport."""
    if footprint is None:
        return {}
    return {table: [json.dumps(r, sort_keys=True, default=str) for r in rows]
            for table, rows in footprint.rows.items()}


def operations_for(changes: list, local: Snapshot, server: Snapshot) -> list:
    """Describe each ticked title as rows to insert plus the server state expected."""
    local_fps = build_footprints(local.rows)
    server_fps = build_footprints(server.rows)
    by_key = {}
    for table in USER_TABLES:
        for row in local.rows.get(table) or []:
            from tools.sync_diff import _title_key
            by_key.setdefault(_title_key(table, row), {}).setdefault(table, []).append(row)

    ops = []
    for change in changes:
        ops.append({
            "key": list(change.key),
            "title": change.title,
            "expected": _cas_view(server_fps.get(change.key)),
            "insert": by_key.get(change.key, {}),
            "tmdb_ids": sorted(
                _tmdb_ids(local_fps[change.key].rows) if change.key in local_fps else set()
            ) + sorted(
                _tmdb_ids(server_fps[change.key].rows) if change.key in server_fps else set()
            ),
        })
    return ops


def promote(db_path, operations: list, backup_path) -> None:
    """Apply ticked titles to the server: one transaction, checked, backed up first.

    Only these titles are touched. There is no path here that replaces a table.
    """
    if not operations:
        return
    backup_database(db_path, backup_path)
    conn = _connect_rw(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            live = build_footprints(_read(conn).rows)
            for op in operations:
                key = tuple(op["key"])
                if _cas_view(live.get(key)) != op["expected"]:
                    raise ServerMoved(
                        f"{op['title']} changed on the server while the checklist was open. "
                        "Nothing was written. Rerun to see the new state."
                    )
            for op in operations:
                _delete_title(conn, tuple(op["key"]), set(op["tmdb_ids"]))
                for table, rows in op["insert"].items():
                    for row in rows:
                        _insert(conn, table, row)
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
    finally:
        conn.close()


def replace_tables(db_path, snapshot: Snapshot) -> None:
    """Replace this database's five tables with the snapshot's, in one transaction."""
    conn = _connect_rw(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            for table in SYNC_TABLES:
                conn.execute(f"DELETE FROM {table}")
                # Insertion order matters for history: ids are reassigned here,
                # and load() reads back in id order.
                for row in snapshot.rows.get(table) or []:
                    _insert(conn, table, row)
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
    finally:
        conn.close()


def write_baseline(path, snapshot: Snapshot) -> None:
    """Record the five tables as they now stand, as the next run's common ancestor."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
    conn = sqlite3.connect(str(path))
    try:
        conn.executescript(user_store._SCHEMA)
        conn.executescript(user_store._INDEXES)
        conn.executescript(query_history._SCHEMA)
        conn.commit()
    finally:
        conn.close()
    replace_tables(path, snapshot)


# ── transports ───────────────────────────────────────────────────────────────

class DirTransport:
    """A stand-in server that is just another database file. Used by the tests."""

    def __init__(self, db_path):
        self.db_path = Path(db_path)
        self.before_promote = None
        self.fail_promote = False

    def snapshot(self) -> Snapshot:
        return read_snapshot(self.db_path)

    def promote(self, operations: list, stamp: str) -> None:
        if self.before_promote is not None:
            self.before_promote()
        if self.fail_promote:
            raise RuntimeError("simulated transfer failure")
        promote(self.db_path, operations,
                self.db_path.with_name(f"{self.db_path.name}.predeploy-{stamp}"))


class SshTransport:
    """The home server over ssh. It needs this repo checked out and current."""

    def __init__(self, host: str, remote_root: str, db_rel: str):
        self.host = host
        self.remote_root = remote_root.rstrip("/")
        self.db_rel = db_rel

    def _run(self, command: str, stdin: str | None = None) -> str:
        result = subprocess.run(["ssh", self.host, command], input=stdin,
                                capture_output=True, text=True)
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip()
            if "No module named tools.sync_state" in detail:
                raise TransportError(
                    f"{self.host} does not have this version of the code yet. "
                    "Deploy first, then rerun."
                )
            raise TransportError(f"{self.host}: {detail}")
        return result.stdout

    def _helper(self, *args: str, stdin: str | None = None) -> str:
        # The server's venv is `venv`; a checkout made like this machine's uses
        # `.venv`. Pick whichever is there rather than assuming.
        pick = 'if [ -x venv/bin/python ]; then PY=venv/bin/python; else PY=.venv/bin/python; fi'
        quoted = " ".join(args)
        return self._run(
            f"cd {self.remote_root} && {pick} && "
            f'"$PY" -m tools.sync_state helper {quoted}',
            stdin=stdin,
        )

    def snapshot(self) -> Snapshot:
        payload = json.loads(self._helper("snapshot", self.db_rel))
        return Snapshot(rows=payload["rows"], columns=payload["columns"])

    def promote(self, operations: list, stamp: str) -> None:
        out = self._helper("promote", self.db_rel, stamp,
                           stdin=json.dumps({"operations": operations}))
        report = json.loads(out or "{}")
        if report.get("refused") == "server_moved":
            raise ServerMoved(report.get("message", "The server changed mid-run."))


# ── the run ──────────────────────────────────────────────────────────────────

@dataclass
class Result:
    promoted: list = field(default_factory=list)
    discarded: list = field(default_factory=list)
    server_changes: list = field(default_factory=list)


def _stamp() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


class Sync:
    def __init__(self, local_db, baseline_db, transport, chooser, confirm):
        self.local_db = Path(local_db)
        self.baseline_db = Path(baseline_db)
        self.transport = transport
        self.chooser = chooser
        self.confirm = confirm

    def _baseline_rows(self):
        if not self.baseline_db.is_file():
            return None
        return read_snapshot(self.baseline_db).rows

    def _gather(self):
        local = read_snapshot(self.local_db)
        server = self.transport.snapshot()
        check_schemas(local, server)
        changes = classify(self._baseline_rows(), local.rows, server.rows)
        return local, server, changes

    def status(self) -> list:
        return self._gather()[2]

    def run(self) -> Result:
        local, server, changes = self._gather()
        offered = [c for c in changes if c.offered]

        # Local is read again before each write, so a like clicked in a browser
        # tab while the checklist sits in an editor cannot be quietly erased.
        before = fingerprint(local.rows)

        ticked = self.chooser(changes) if offered else []
        self._assert_local_unmoved(before)

        discarded = [c for c in offered if c not in ticked]
        if offered and not self.confirm(ticked, discarded):
            raise Cancelled("Nothing was written.")

        stamp = _stamp()
        if ticked:
            self.transport.promote(operations_for(ticked, local, server), stamp)

        self._assert_local_unmoved(before)
        fresh = self.transport.snapshot()
        ensure_tables(self.local_db)
        backup_database(self.local_db,
                        self.local_db.with_name(f"{self.local_db.name}.bak-{stamp}"))
        replace_tables(self.local_db, fresh)
        write_baseline(self.baseline_db, fresh)

        return Result(promoted=ticked, discarded=discarded,
                      server_changes=[c for c in changes if c.state is State.SERVER_ONLY])

    def _assert_local_unmoved(self, before: str) -> None:
        if fingerprint(read_snapshot(self.local_db).rows) != before:
            raise LocalMoved(
                "Local user state changed while the checklist was open, so the refresh "
                "would erase it. Nothing was written. Rerun."
            )


# ── editor and prompts ───────────────────────────────────────────────────────

def edit_checklist(changes: list, host: str | None = None) -> list:
    """Open the checklist in the operator's editor and return what they ticked."""
    editor = os.environ.get("VISUAL") or os.environ.get("EDITOR") or "vi"
    text = render_checklist(changes, host=host)
    with tempfile.TemporaryDirectory(prefix="streamline-sync-") as tmp:
        path = Path(tmp) / "promote.txt"
        path.write_text(text)
        result = subprocess.run(f'{editor} "{path}"', shell=True)
        if result.returncode != 0:
            raise Cancelled(f"{editor} exited non-zero. Nothing was written.")
        return parse_checklist(path.read_text(), changes)


def describe(change, *, promotable: bool = True) -> str:
    """One change in words. Non-promotable ones are described by what arrives here."""
    body = change.action if promotable else change.incoming
    line = f"  {change.title}  {body}"
    if change.note:
        line += f"   ({change.note})"
    return line


def _ask(prompt: str) -> str | None:
    """None when there is no terminal to ask on."""
    try:
        return input(prompt).strip().lower()
    except EOFError:
        # The editor subprocess shares stdin, so a piped answer is not
        # reliably still there by the time we get here. Silence is not consent.
        print("\nNo terminal to confirm on. Nothing was written.")
        return None


def ask_to_confirm(ticked: list, discarded: list) -> bool:
    deleting = [c for c in ticked if c.removes_title]
    adding = [c for c in ticked if not c.removes_title]

    print()
    if adding:
        print(f"Updating {len(adding)} title(s) on prod:")
        print("\n".join(describe(c) for c in adding))
    if discarded:
        print(f"\nDiscarding {len(discarded)} local change(s) on refresh:")
        print("\n".join(describe(c) for c in discarded))
    if not ticked:
        print("Sending nothing to prod.")

    # Deletions get their own gate. A tick reads as "include this", which is
    # the right reading for every other kind of line and the wrong one here,
    # so ticking alone must not be enough to remove data from the live app.
    if deleting:
        print(f"\nDELETING {len(deleting)} title(s) FROM PROD.")
        print("This removes data from the live app at 192.168.1.101.")
        print("\n".join(describe(c) for c in deleting))
        answer = _ask('\nType "delete" to confirm these removals, anything else to cancel: ')
        if answer != "delete":
            print("Not confirmed. Nothing was written.")
            return False
        if not adding:
            return True

    answer = _ask("\nProceed? [y/N] ")
    return answer in ("y", "yes")


# ── CLI ──────────────────────────────────────────────────────────────────────

def render_status(changes: list) -> str:
    if not changes:
        return "Local and prod user state are identical."
    lines = []
    buckets = (
        (State.CONFLICT, "Changed on both sides, differently:"),
        (State.UNCLASSIFIED, "Differs, and there is no baseline yet to say which side moved:"),
        (None, "Only on prod, so not offered until a baseline exists:"),
        (State.LOCAL_ONLY, "Changed locally, can be promoted:"),
        (State.SERVER_ONLY, "Changed on prod, arrives when local refreshes:"),
    )
    for state, heading in buckets:
        rows = [c for c in changes if c.state is state]
        if rows:
            lines.append(heading)
            lines += [describe(c, promotable=c.offered) for c in rows]
    return "\n".join(lines)


def _helper_main(args) -> int:
    """Runs on the server, inside its own checkout, over ssh."""
    db_path = PROJECT_ROOT / args.db_rel
    if args.op == "snapshot":
        snap = read_snapshot(db_path)
        print(json.dumps({"rows": snap.rows, "columns": snap.columns}, default=str))
        return 0

    payload = json.loads(sys.stdin.read() or "{}")
    backup = db_path.with_name(f"{db_path.name}.predeploy-{args.stamp}")
    try:
        promote(db_path, payload.get("operations") or [], backup)
    except ServerMoved as exc:
        print(json.dumps({"refused": "server_moved", "message": str(exc)}))
        return 0
    print(json.dumps({"ok": True}))
    return 0


def _build_sync(args) -> Sync:
    if not config.SYNC_HOST:
        raise SystemExit(
            "No sync host configured. Add to config.local.yaml:\n"
            "  sync:\n    host: user@homeserver\n    remote_root: ~/streamline\n"
            "or set STREAMLINE_SYNC_HOST."
        )
    db_rel = os.path.relpath(config.EVENT_DB_PATH, PROJECT_ROOT)
    return Sync(
        local_db=config.EVENT_DB_PATH,
        baseline_db=PROJECT_ROOT / BASELINE_REL,
        transport=SshTransport(config.SYNC_HOST, config.SYNC_REMOTE_ROOT, db_rel),
        chooser=lambda changes: edit_checklist(changes, host=config.SYNC_HOST),
        confirm=ask_to_confirm,
    )


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--status", action="store_true",
                        help="show what differs and write nothing")
    sub = parser.add_subparsers(dest="verb")
    helper = sub.add_parser("helper", help="internal: runs on the server over ssh")
    helper.add_argument("op", choices=("snapshot", "promote"))
    helper.add_argument("db_rel")
    helper.add_argument("stamp", nargs="?", default="manual")
    args = parser.parse_args(argv)

    if args.verb == "helper":
        return _helper_main(args)

    try:
        sync = _build_sync(args)
        if args.status:
            changes = sync.status()
            print(render_status(changes))
            return EXIT_DIFFERS if changes else 0

        result = sync.run()
    except (MissingState, SchemaMismatch, ServerMoved, LocalMoved,
            ChecklistEdited, TransportError) as exc:
        print(f"Refused: {exc}")
        return EXIT_REFUSED
    except Cancelled as exc:
        print(f"Cancelled: {exc}")
        return EXIT_REFUSED

    if result.promoted:
        print(f"\nSent {len(result.promoted)} title(s) to prod.")
    if result.discarded:
        print(f"Discarded {len(result.discarded)} local change(s).")
    print("Local now matches prod. Previous local state is beside it as *.bak-<timestamp>.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
