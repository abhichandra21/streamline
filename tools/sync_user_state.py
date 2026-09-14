"""Move user state between this install and the home server, one way at a time.

User state is four tables in the user store (watchlist, ratings, manual
archive, show tracking) plus the search history JSON. The same SQLite file
also holds imported watch events, which are not user state and never move.
Both machines are used by one person, one at a time, so this never merges
rows. It replaces the user tables wholesale in the direction you ask for and
refuses when that would overwrite work on the other side. It knows which side
moved because it keeps a baseline: a snapshot taken after the last pull or push.

    ./recommend-sync status   what differs, and which side changed since the baseline
    ./recommend-sync pull     replace local user state with the server's (backs up local first)
    ./recommend-sync push     replace the server's with local (backs up the server first);
                              refused if the server changed since the baseline; --force overrides

The other machine is configured in config.local.yaml under `sync:` (or the
STREAMLINE_SYNC_HOST environment variable). It needs this repo checked out
too: snapshots and applies run there through `python -m tools.sync_user_state
helper ...` so WAL state is captured and a push is one compare-and-swap
transaction against the live database.
"""

import argparse
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Optional
from zoneinfo import ZoneInfo

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import config  # noqa: E402
from recommender import history as query_history  # noqa: E402

DB_REL = os.path.relpath(config.EVENT_DB_PATH, PROJECT_ROOT)
HISTORY_REL = os.path.relpath(query_history.HISTORY_PATH, PROJECT_ROOT)
BASE_DIRNAME = "data/sync"
INCOMING_DIRNAME = "data/sync/incoming"

USER_TABLES = ("saved_titles", "title_ratings", "manual_archive_entries", "show_tracking")
# Columns that only record time, per table. They are shown as "when" but do
# not make a row count as changed. watched_at is deliberately absent: it
# orders the archive, so a re-watch is a real change.
BOOKKEEPING_COLUMNS = {
    "saved_titles": ("saved_at", "updated_at"),
    "title_ratings": ("rated_at", "updated_at"),
    "manual_archive_entries": (),
    "show_tracking": ("created_at", "updated_at"),
}
TIME_COLUMNS = {
    "saved_titles": ("saved_at", "updated_at"),
    "title_ratings": ("rated_at", "updated_at"),
    "manual_archive_entries": ("watched_at",),
    "show_tracking": ("created_at", "updated_at"),
}
HISTORY_TABLE = "query_history"
TABLE_ORDER = (*USER_TABLES, HISTORY_TABLE)

LOCAL_TZ = ZoneInfo("America/Chicago")
EXIT_STALE = 3


class StaleRemote(Exception):
    """A push was refused because it would overwrite changes on the server."""


class MissingState(Exception):
    """A database that must exist does not, so nothing can be trusted."""


# ── Snapshots: a logical view of user state, independent of file bytes ───────

def _normalize(title: str) -> str:
    title = title.lower()
    title = re.sub(r"\s*\([^)]*\)", "", title)
    return title.strip()


def _row_key(table: str, row: dict) -> str:
    if table == "show_tracking":
        return f"tmdb:{row['tmdb_id']}"
    if row.get("tmdb_id") is not None:
        return f"tmdb:{row['content_type']}:{row['tmdb_id']}"
    norm = row.get("normalized_title") or _normalize(row["title"])
    return f"title:{row['content_type']}:{norm}"


def _require_db(db_path: Path) -> None:
    if not Path(db_path).is_file():
        raise MissingState(f"Database not found: {db_path}. Refusing to treat a missing file as empty state.")


def _connect_ro(db_path: Path) -> sqlite3.Connection:
    """Read-only connection that still sees committed WAL contents."""
    _require_db(db_path)
    conn = sqlite3.connect(f"file:{Path(db_path).resolve()}?mode=ro", uri=True, timeout=30.0)
    conn.row_factory = sqlite3.Row
    return conn


def _snapshot_conn(conn: sqlite3.Connection) -> dict:
    present = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    out: dict = {}
    for table in USER_TABLES:
        rows: dict = {}
        if table in present:
            for r in conn.execute(f"SELECT * FROM {table}"):
                row = {k: r[k] for k in r.keys() if k != "id"}
                rows[_row_key(table, row)] = row
        out[table] = rows
    return out


def snapshot_db(db_path: Path) -> dict:
    conn = _connect_ro(db_path)
    try:
        return _snapshot_conn(conn)
    finally:
        conn.close()


def snapshot_history(history_path: Optional[Path]) -> dict:
    if history_path is None or not Path(history_path).exists():
        return {}
    entries = json.loads(Path(history_path).read_text() or "[]")
    return {f"{e.get('timestamp')}|{e.get('query')}": e for e in entries}


def snapshot(db_path: Path, history_path: Optional[Path]) -> dict:
    snap = snapshot_db(db_path)
    snap[HISTORY_TABLE] = snapshot_history(history_path)
    return snap


def fingerprint(db_snapshot: dict) -> str:
    """Exact-state hash of the user tables (every column but id), for compare-and-swap."""
    tables = {t: db_snapshot.get(t, {}) for t in USER_TABLES}
    return hashlib.sha256(json.dumps(tables, sort_keys=True, default=str).encode()).hexdigest()


def backup_snapshot(db_path: Path, dest: Path) -> None:
    """Self-contained copy of a live database via SQLite's backup API (WAL-safe)."""
    _require_db(db_path)
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        dest.unlink()
    src = sqlite3.connect(str(db_path), timeout=30.0)
    dst = sqlite3.connect(str(dest))
    try:
        src.backup(dst)
        # The copy inherits WAL mode from the source. Make it a single plain
        # file so it can be moved with scp and opened without sidecars.
        dst.execute("PRAGMA journal_mode = DELETE")
    finally:
        dst.close()
        src.close()


# ── Diff ─────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Change:
    table: str
    key: str
    title: str
    fields: dict = field(default_factory=dict)   # name -> (before, after), changed rows only
    when: Optional[str] = None                   # ISO timestamp of the newest time column


@dataclass
class Diff:
    added: list = field(default_factory=list)
    removed: list = field(default_factory=list)
    changed: list = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not (self.added or self.removed or self.changed)

    @property
    def count(self) -> int:
        return len(self.added) + len(self.removed) + len(self.changed)


def _title_of(table: str, row: dict) -> str:
    if table == HISTORY_TABLE:
        return row.get("query") or "(search)"
    return row.get("title") or row.get("normalized_title") or "(untitled)"


def _when_of(table: str, row: dict) -> Optional[str]:
    if table == HISTORY_TABLE:
        return row.get("timestamp")
    times = [row[c] for c in TIME_COLUMNS[table] if row.get(c)]
    return max(times) if times else None


def _compared_fields(table: str, row: dict) -> dict:
    ignore = set(BOOKKEEPING_COLUMNS.get(table, ()))
    return {k: v for k, v in row.items() if k not in ignore}


def diff(a: dict, b: dict) -> Diff:
    """Changes needed to turn snapshot a into snapshot b."""
    d = Diff()
    for table in TABLE_ORDER:
        ra, rb = a.get(table, {}), b.get(table, {})
        for key in sorted(rb.keys() - ra.keys()):
            d.added.append(Change(table, key, _title_of(table, rb[key]), when=_when_of(table, rb[key])))
        for key in sorted(ra.keys() - rb.keys()):
            d.removed.append(Change(table, key, _title_of(table, ra[key]), when=_when_of(table, ra[key])))
        for key in sorted(ra.keys() & rb.keys()):
            fa, fb = _compared_fields(table, ra[key]), _compared_fields(table, rb[key])
            fields = {k: (fa.get(k), fb.get(k)) for k in sorted(fa.keys() | fb.keys()) if fa.get(k) != fb.get(k)}
            if fields:
                d.changed.append(Change(table, key, _title_of(table, rb[key]), fields=fields,
                                        when=_when_of(table, rb[key])))
    return d


def _local_time(iso: Optional[str]) -> str:
    if not iso:
        return ""
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return iso
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(LOCAL_TZ).strftime("%Y-%m-%d %H:%M %Z")


def describe(d: Diff, indent: str = "  ") -> list:
    lines = []
    for sign, changes in (("+", d.added), ("-", d.removed), ("~", d.changed)):
        for c in changes:
            detail = ""
            if c.fields:
                detail = "  " + ", ".join(f"{k}: {b!r} -> {a!r}" for k, (b, a) in c.fields.items())
            when = f"  ({_local_time(c.when)})" if c.when else ""
            lines.append(f"{indent}{sign} {c.table:24s} {c.title}{detail}{when}")
    return lines


# ── Apply: replace the user tables and the history, in one transaction ───────

def apply_bundle(
    db_path: Path,
    history_path: Path,
    bundle_db: Path,
    bundle_history: Optional[Path],
    expected_fingerprint: Optional[str],
    backup_suffix: str,
) -> None:
    """Replace db_path's user tables with bundle_db's and history_path with bundle_history.

    Runs as one BEGIN IMMEDIATE transaction on the live database: other
    writers wait, the current state is compared with expected_fingerprint
    (compare-and-swap; StaleRemote on mismatch), the tables are swapped, the
    new history is staged beside the old one, then commit and rename. The
    live database is backed up with the backup API first. Watch events and
    imports in the same file are untouched.
    """
    db_path, history_path, bundle_db = Path(db_path), Path(history_path), Path(bundle_db)
    _require_db(db_path)
    _require_db(bundle_db)

    backup_snapshot(db_path, db_path.with_name(f"{db_path.name}.{backup_suffix}"))
    if history_path.exists():
        shutil.copy2(history_path, history_path.with_name(f"{history_path.name}.{backup_suffix}"))

    new_history = "[]"
    if bundle_history is not None and Path(bundle_history).exists():
        new_history = json.dumps(json.loads(Path(bundle_history).read_text() or "[]"), indent=2)

    conn = sqlite3.connect(str(db_path), timeout=30.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    staged: Optional[Path] = None
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            if expected_fingerprint is not None and fingerprint(_snapshot_conn(conn)) != expected_fingerprint:
                raise StaleRemote("The server's user state changed while this push was in flight. Nothing was written.")
            conn.execute("ATTACH DATABASE ? AS bundle", (str(bundle_db),))
            for table in USER_TABLES:
                live_cols = [r["name"] for r in conn.execute(f"PRAGMA table_info({table})")]
                bundle_cols = [r["name"] for r in conn.execute(f"PRAGMA bundle.table_info({table})")]
                if not live_cols or live_cols != bundle_cols:
                    raise RuntimeError(f"{table}: column mismatch between live ({live_cols}) and bundle ({bundle_cols}); "
                                       "deploy the same code to both sides first")
                cols = ", ".join(live_cols)
                conn.execute(f"DELETE FROM {table}")
                conn.execute(f"INSERT INTO {table} ({cols}) SELECT {cols} FROM bundle.{table}")
            history_path.parent.mkdir(parents=True, exist_ok=True)
            staged = history_path.with_name(f".{history_path.name}.incoming")
            staged.write_text(new_history)
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            if staged is not None and staged.exists():
                staged.unlink()
            raise
        os.replace(staged, history_path)
    finally:
        conn.close()


# ── Transports ───────────────────────────────────────────────────────────────

class DirTransport:
    """A fake remote that is just another checkout directory. Used by tests."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.applies: list = []
        self.fail_apply = False
        self.before_apply = None   # callable run right before apply, to simulate a concurrent writer

    def snapshot_db(self, rel: str, dest: Path) -> None:
        backup_snapshot(self.root / rel, dest)

    def fetch_history(self, rel: str, dest: Path) -> bool:
        src = self.root / rel
        if not src.exists():
            return False
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)
        return True

    def apply(self, db_rel: str, history_rel: str, bundle_db: Path, bundle_history: Optional[Path],
              expected_fingerprint: Optional[str], stamp: str) -> None:
        if self.fail_apply:
            raise RuntimeError("simulated transfer failure")
        if self.before_apply is not None:
            self.before_apply()
        apply_bundle(self.root / db_rel, self.root / history_rel, bundle_db, bundle_history,
                     expected_fingerprint, f"predeploy-{stamp}")
        self.applies.append(stamp)


class SshTransport:
    """The other machine over ssh and scp. It must have this repo checked out."""

    def __init__(self, host: str, remote_root: str):
        self.host = host
        self.remote_root = remote_root.rstrip("/")

    def _remote(self, rel: str) -> str:
        return f"{self.remote_root}/{rel}"

    def _helper(self, *args: str) -> subprocess.CompletedProcess:
        cmd = f"cd {self.remote_root} && venv/bin/python -m tools.sync_user_state helper " + " ".join(args)
        return subprocess.run(["ssh", self.host, cmd], capture_output=True, text=True)

    @staticmethod
    def _check(result: subprocess.CompletedProcess, what: str) -> None:
        if result.returncode != 0:
            raise RuntimeError(f"{what} failed: {(result.stderr or result.stdout).strip()}")

    def _scp(self, src: str, dest: str, what: str) -> None:
        self._check(subprocess.run(["scp", "-q", src, dest], capture_output=True, text=True), what)

    def snapshot_db(self, rel: str, dest: Path) -> None:
        remote_tmp = self._remote(f"{INCOMING_DIRNAME}/snapshot.db")
        self._check(self._helper("snapshot", rel, remote_tmp), "remote snapshot")
        dest.parent.mkdir(parents=True, exist_ok=True)
        self._scp(f"{self.host}:{remote_tmp}", str(dest), "fetching snapshot")
        subprocess.run(["ssh", self.host, f"rm -f {remote_tmp}"], capture_output=True)

    def fetch_history(self, rel: str, dest: Path) -> bool:
        probe = subprocess.run(["ssh", self.host, f"test -f {self._remote(rel)}"], capture_output=True)
        if probe.returncode != 0:
            return False
        dest.parent.mkdir(parents=True, exist_ok=True)
        self._scp(f"{self.host}:{self._remote(rel)}", str(dest), "fetching history")
        return True

    def apply(self, db_rel: str, history_rel: str, bundle_db: Path, bundle_history: Optional[Path],
              expected_fingerprint: Optional[str], stamp: str) -> None:
        incoming = self._remote(INCOMING_DIRNAME)
        self._check(subprocess.run(["ssh", self.host, f"mkdir -p {incoming}"], capture_output=True, text=True),
                    "preparing incoming dir")
        self._scp(str(bundle_db), f"{self.host}:{incoming}/bundle.db", "uploading bundle")
        history_arg = "-"
        if bundle_history is not None and Path(bundle_history).exists():
            self._scp(str(bundle_history), f"{self.host}:{incoming}/bundle_history.json", "uploading history")
            history_arg = f"{incoming}/bundle_history.json"
        result = self._helper("apply", db_rel, history_rel, f"{incoming}/bundle.db", history_arg,
                              expected_fingerprint or "-", stamp)
        subprocess.run(["ssh", self.host, f"rm -f {incoming}/bundle.db {incoming}/bundle_history.json"],
                       capture_output=True)
        if result.returncode == EXIT_STALE:
            raise StaleRemote((result.stdout or result.stderr).strip())
        self._check(result, "remote apply")


# ── Sync ─────────────────────────────────────────────────────────────────────

@dataclass
class StatusReport:
    local_vs_remote: Diff                 # what pull would change locally
    baseline: Optional[dict]
    local_since_base: Optional[Diff]
    remote_since_base: Optional[Diff]


@dataclass
class PushResult:
    noop: bool
    pushed: Diff


def _stamp() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


class Sync:
    def __init__(self, root: Path, base_dir: Path, transport, db_rel: str = DB_REL, history_rel: str = HISTORY_REL):
        self.root = Path(root)
        self.base_dir = Path(base_dir)
        self.transport = transport
        self.db_rel, self.history_rel = db_rel, history_rel
        self.local_db = self.root / db_rel
        self.local_history = self.root / history_rel
        self.base_db = self.base_dir / "streamline.db"
        self.base_history = self.base_dir / "query_history.json"

    # -- snapshots --

    def _local(self) -> dict:
        return snapshot(self.local_db, self.local_history)

    def _baseline(self) -> Optional[dict]:
        if not self.base_db.exists():
            return None
        return snapshot(self.base_db, self.base_history)

    @contextmanager
    def _remote(self) -> Iterator[tuple]:
        """Yield (snapshot_db_path, history_path_or_None) in a temp dir removed afterwards."""
        with tempfile.TemporaryDirectory(prefix="streamline-sync-") as tmp:
            db = Path(tmp) / "remote.db"
            history = Path(tmp) / "remote_history.json"
            self.transport.snapshot_db(self.db_rel, db)
            has_history = self.transport.fetch_history(self.history_rel, history)
            yield db, (history if has_history else None)

    def _save_baseline(self) -> None:
        backup_snapshot(self.local_db, self.base_db)
        if self.local_history.exists():
            shutil.copy2(self.local_history, self.base_history)
        else:
            self.base_history.write_text("[]")

    # -- verbs --

    def status(self) -> StatusReport:
        local = self._local()
        with self._remote() as (rdb, rhist):
            remote = snapshot(rdb, rhist)
        base = self._baseline()
        return StatusReport(
            local_vs_remote=diff(local, remote),
            baseline=base,
            local_since_base=diff(base, local) if base is not None else None,
            remote_since_base=diff(base, remote) if base is not None else None,
        )

    def pull(self) -> Diff:
        local = self._local()
        with self._remote() as (rdb, rhist):
            applied = diff(local, snapshot(rdb, rhist))
            apply_bundle(self.local_db, self.local_history, rdb, rhist, None, f"bak-{_stamp()}")
        self._save_baseline()
        return applied

    def push(self, force: bool = False) -> PushResult:
        local = self._local()
        base = self._baseline()
        if base is None and not force:
            raise StaleRemote(
                "No baseline yet, so it is unknown whether the server has work you would overwrite. "
                "Run `pull` first (it saves the baseline), or `push --force` after checking `status`."
            )
        with self._remote() as (rdb, rhist):
            remote = snapshot(rdb, rhist)
            if base is not None and not force:
                remote_moved = diff(base, remote)
                if not remote_moved.empty:
                    local_moved = diff(base, local)
                    lines = ["The server changed since the last sync:", *describe(remote_moved)]
                    if local_moved.empty:
                        lines.append("Nothing changed locally since the baseline, so `pull` is safe and loses nothing.")
                    else:
                        lines += ["Local also changed since the baseline:", *describe(local_moved),
                                  "Both sides moved. Pick a side: `pull` keeps the server's and you redo the local "
                                  "changes there, or `push --force` keeps local and you redo the server's here."]
                    raise StaleRemote("\n".join(lines))
            to_push = diff(remote, local)
            if to_push.empty:
                self._save_baseline()
                return PushResult(noop=True, pushed=to_push)
            expected = fingerprint(snapshot_db(rdb))
            with tempfile.TemporaryDirectory(prefix="streamline-sync-") as tmp:
                bundle = Path(tmp) / "bundle.db"
                backup_snapshot(self.local_db, bundle)
                self.transport.apply(self.db_rel, self.history_rel, bundle,
                                     self.local_history if self.local_history.exists() else None,
                                     expected, _stamp())
        self._save_baseline()
        return PushResult(noop=False, pushed=to_push)


# ── CLI ──────────────────────────────────────────────────────────────────────

def render_status(report: StatusReport) -> str:
    lines = []
    d = report.local_vs_remote
    if d.empty:
        lines.append("Local and server user state are identical.")
    else:
        lines.append(f"Local and server differ in {d.count} place(s). Shown as what `pull` would change locally:")
        lines += describe(d)
    if report.baseline is None:
        lines.append("No baseline yet: cannot tell which side changed. `pull` records one.")
        return "\n".join(lines)
    ls, rs = report.local_since_base, report.remote_since_base
    if ls.empty and rs.empty:
        lines.append("Neither side changed since the last sync.")
    elif ls.empty:
        lines.append("Only the server changed since the last sync. `pull` is safe:")
        lines += describe(rs)
    elif rs.empty:
        lines.append("Only local changed since the last sync. `push` is safe:")
        lines += describe(ls)
    else:
        lines.append("Both sides changed since the last sync. One of them has to be redone by hand.")
        lines.append("Server changes:")
        lines += describe(rs)
        lines.append("Local changes:")
        lines += describe(ls)
    return "\n".join(lines)


def _build_sync(args) -> Sync:
    if not args.host:
        raise SystemExit(
            "No sync host configured. Add to config.local.yaml:\n"
            "  sync:\n    host: user@host\n    remote_root: ~/streamline\n"
            "or set STREAMLINE_SYNC_HOST, or pass --host."
        )
    return Sync(root=PROJECT_ROOT, base_dir=PROJECT_ROOT / BASE_DIRNAME,
                transport=SshTransport(args.host, args.remote_path))


def _helper_main(args) -> int:
    """Runs on the other machine over ssh, inside its own checkout."""
    if args.op == "snapshot":
        backup_snapshot(PROJECT_ROOT / args.db_rel, Path(args.dest))
        return 0
    bundle_history = None if args.bundle_history == "-" else Path(args.bundle_history)
    expected = None if args.expected == "-" else args.expected
    try:
        apply_bundle(PROJECT_ROOT / args.db_rel, PROJECT_ROOT / args.history_rel, Path(args.bundle_db),
                     bundle_history, expected, f"predeploy-{args.stamp}")
    except StaleRemote as exc:
        print(str(exc))
        return EXIT_STALE
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="verb", required=True)
    for verb in ("status", "pull", "push"):
        p = sub.add_parser(verb)
        p.add_argument("--host", default=config.SYNC_HOST)
        p.add_argument("--remote-path", default=config.SYNC_REMOTE_ROOT)
        if verb == "push":
            p.add_argument("--force", action="store_true", help="push even if the server changed since the baseline")
    helper = sub.add_parser("helper", help="internal: run on the other machine over ssh")
    hsub = helper.add_subparsers(dest="op", required=True)
    snap = hsub.add_parser("snapshot"); snap.add_argument("db_rel"); snap.add_argument("dest")
    app = hsub.add_parser("apply")
    for name in ("db_rel", "history_rel", "bundle_db", "bundle_history", "expected", "stamp"):
        app.add_argument(name)
    args = parser.parse_args(argv)

    if args.verb == "helper":
        return _helper_main(args)

    try:
        s = _build_sync(args)
        if args.verb == "status":
            report = s.status()
            print(render_status(report))
            return 0 if report.local_vs_remote.empty else 1

        if args.verb == "pull":
            applied = s.pull()
            if applied.empty:
                print("Local already matched the server. Baseline recorded.")
            else:
                print(f"Pulled. Local changed in {applied.count} place(s):")
                print("\n".join(describe(applied)))
            print("The previous local files sit beside them as *.bak-<timestamp>.")
            return 0

        result = s.push(force=args.force)
    except MissingState as exc:
        print(f"Refused: {exc}")
        return 2
    except StaleRemote as exc:
        print("Refused to push.")
        print(str(exc))
        return 2
    if result.noop:
        print("Server already matched local. Nothing pushed; baseline recorded.")
    else:
        print(f"Pushed. Server changed in {result.pushed.count} place(s):")
        print("\n".join(describe(result.pushed)))
        print("The server's previous files sit beside them as *.predeploy-<timestamp>.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
