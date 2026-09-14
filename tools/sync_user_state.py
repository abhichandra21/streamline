"""Move user state between this install and the home server, one way at a time.

User state is the SQLite user store (watchlist, ratings, manual archive, show
tracking) plus the search history JSON. Both machines are used by one person,
one at a time, so this never merges rows. It copies whole files in the
direction you ask for and refuses when that would overwrite work on the other
side. It knows which side moved because it keeps a baseline: a copy of the
files as they were after the last pull or push.

    ./recommend-sync status   show what differs, and which side changed since the baseline
    ./recommend-sync pull     replace local user state with the server's (backs up local first)
    ./recommend-sync push     replace the server's with local (backs up the server first),
                              refused if the server changed since the baseline; --force overrides

Caches, watch events, and imports are not user state and are not touched.
"""

import argparse
import json
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

PROJECT_ROOT = Path(__file__).resolve().parent.parent

DB_REL = "data/streamline.db"
HISTORY_REL = "recommender/cache/query_history.json"
BASE_DIRNAME = "data/sync"

DEFAULT_HOST = "abhishek@192.168.1.101"
DEFAULT_REMOTE_ROOT = "~/streamline"
SERVICE_NAME = "streamline-web"

# Table -> columns that only record time. They never make a row "changed" on
# their own, but the newest one is shown as when the change happened.
USER_TABLES: dict[str, tuple[str, ...]] = {
    "saved_titles": ("saved_at", "updated_at"),
    "title_ratings": ("rated_at", "updated_at"),
    "manual_archive_entries": ("watched_at",),
    "show_tracking": ("created_at", "updated_at"),
}
HISTORY_TABLE = "query_history"
TABLE_ORDER = (*USER_TABLES, HISTORY_TABLE)

LOCAL_TZ = ZoneInfo("America/Chicago")


class StaleRemote(Exception):
    """A push was refused because it would overwrite changes on the server."""


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


def snapshot_db(db_path: Path) -> dict[str, dict[str, dict]]:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    out: dict[str, dict[str, dict]] = {}
    try:
        present = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for table in USER_TABLES:
            rows: dict[str, dict] = {}
            if table in present:
                for r in conn.execute(f"SELECT * FROM {table}"):
                    row = {k: r[k] for k in r.keys() if k != "id"}
                    rows[_row_key(table, row)] = row
            out[table] = rows
    finally:
        conn.close()
    return out


def snapshot_history(history_path: Path) -> dict[str, dict]:
    if not history_path.exists():
        return {}
    entries = json.loads(history_path.read_text() or "[]")
    return {f"{e.get('timestamp')}|{e.get('query')}": e for e in entries}


def snapshot(db_path: Path, history_path: Path) -> dict[str, dict[str, dict]]:
    snap = snapshot_db(db_path)
    snap[HISTORY_TABLE] = snapshot_history(history_path)
    return snap


# ── Diff ─────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Change:
    table: str
    key: str
    title: str
    fields: dict = field(default_factory=dict)   # name -> (before, after), changed rows only
    when: str | None = None                      # ISO timestamp of the newest time column


@dataclass
class Diff:
    added: list[Change] = field(default_factory=list)
    removed: list[Change] = field(default_factory=list)
    changed: list[Change] = field(default_factory=list)

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


def _when_of(table: str, row: dict) -> str | None:
    if table == HISTORY_TABLE:
        return row.get("timestamp")
    times = [row[c] for c in USER_TABLES[table] if row.get(c)]
    return max(times) if times else None


def _compared_fields(table: str, row: dict) -> dict:
    ignore = set(USER_TABLES.get(table, ()))
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


def _local_time(iso: str | None) -> str:
    if not iso:
        return ""
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return iso
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ZoneInfo("UTC"))
    return dt.astimezone(LOCAL_TZ).strftime("%Y-%m-%d %H:%M %Z")


def describe(d: Diff, indent: str = "  ") -> list[str]:
    lines: list[str] = []
    for sign, changes in (("+", d.added), ("-", d.removed), ("~", d.changed)):
        for c in changes:
            detail = ""
            if c.fields:
                detail = "  " + ", ".join(f"{k}: {b!r} -> {a!r}" for k, (b, a) in c.fields.items())
            when = f"  ({_local_time(c.when)})" if c.when else ""
            lines.append(f"{indent}{sign} {c.table:24s} {c.title}{detail}{when}")
    return lines


# ── Transports ───────────────────────────────────────────────────────────────

class DirTransport:
    """A fake remote that is just another directory. Used by tests."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.puts: list[str] = []
        self.restarts = 0
        self.fail_put = False

    def fetch(self, rel: str, dest: Path) -> None:
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(self.root / rel, dest)

    def put(self, src: Path, rel: str) -> None:
        if self.fail_put:
            raise RuntimeError("simulated transfer failure")
        shutil.copy2(src, self.root / rel)
        self.puts.append(rel)

    def backup_remote(self, rel: str, suffix: str) -> None:
        target = self.root / rel
        shutil.copy2(target, target.with_name(f"{target.name}.{suffix}"))

    def restart(self) -> None:
        self.restarts += 1


class SshTransport:
    """The home server over ssh and scp."""

    def __init__(self, host: str = DEFAULT_HOST, remote_root: str = DEFAULT_REMOTE_ROOT):
        self.host = host
        self.remote_root = remote_root.rstrip("/")

    def _remote(self, rel: str) -> str:
        return f"{self.remote_root}/{rel}"

    @staticmethod
    def _run(cmd: list[str]) -> None:
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            raise RuntimeError(f"{' '.join(cmd[:2])} failed: {result.stderr.strip() or result.stdout.strip()}")

    def fetch(self, rel: str, dest: Path) -> None:
        dest.parent.mkdir(parents=True, exist_ok=True)
        self._run(["scp", "-q", f"{self.host}:{self._remote(rel)}", str(dest)])

    def put(self, src: Path, rel: str) -> None:
        self._run(["scp", "-q", str(src), f"{self.host}:{self._remote(rel)}"])

    def backup_remote(self, rel: str, suffix: str) -> None:
        path = self._remote(rel)
        self._run(["ssh", self.host, f"cp -p {path} {path}.{suffix}"])

    def restart(self) -> None:
        self._run(["ssh", self.host, f"sudo systemctl restart {SERVICE_NAME}"])


# ── Sync ─────────────────────────────────────────────────────────────────────

@dataclass
class StatusReport:
    local_vs_remote: Diff                 # what pull would change locally
    baseline: dict | None
    local_since_base: Diff | None
    remote_since_base: Diff | None


@dataclass
class PushResult:
    noop: bool
    pushed: Diff


def _stamp() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


class Sync:
    def __init__(self, root: Path, base_dir: Path, transport):
        self.root = Path(root)
        self.base_dir = Path(base_dir)
        self.transport = transport
        self.local_db = self.root / DB_REL
        self.local_history = self.root / HISTORY_REL
        self.base_db = self.base_dir / Path(DB_REL).name
        self.base_history = self.base_dir / Path(HISTORY_REL).name

    # -- snapshots --

    def _local(self) -> dict:
        return snapshot(self.local_db, self.local_history)

    def _baseline(self) -> dict | None:
        if not self.base_db.exists():
            return None
        return snapshot(self.base_db, self.base_history)

    def _fetch_remote(self) -> tuple[Path, Path]:
        tmp = Path(tempfile.mkdtemp(prefix="streamline-sync-"))
        db, history = tmp / Path(DB_REL).name, tmp / Path(HISTORY_REL).name
        self.transport.fetch(DB_REL, db)
        self.transport.fetch(HISTORY_REL, history)
        return db, history

    def _save_baseline(self, db: Path, history: Path) -> None:
        self.base_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(db, self.base_db)
        if history.exists():
            shutil.copy2(history, self.base_history)
        elif self.base_history.exists():
            self.base_history.unlink()

    # -- verbs --

    def status(self) -> StatusReport:
        rdb, rhist = self._fetch_remote()
        local, remote, base = self._local(), snapshot(rdb, rhist), self._baseline()
        return StatusReport(
            local_vs_remote=diff(local, remote),
            baseline=base,
            local_since_base=diff(base, local) if base is not None else None,
            remote_since_base=diff(base, remote) if base is not None else None,
        )

    def pull(self) -> Diff:
        rdb, rhist = self._fetch_remote()
        applied = diff(self._local(), snapshot(rdb, rhist))
        stamp = _stamp()
        if self.local_db.exists():
            shutil.copy2(self.local_db, self.local_db.with_name(f"{self.local_db.name}.bak-{stamp}"))
        if self.local_history.exists():
            shutil.copy2(self.local_history, self.local_history.with_name(f"{self.local_history.name}.bak-{stamp}"))
        self.local_db.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(rdb, self.local_db)
        self.local_history.parent.mkdir(parents=True, exist_ok=True)
        if rhist.exists():
            shutil.copy2(rhist, self.local_history)
        self._save_baseline(self.local_db, self.local_history)
        return applied

    def push(self, force: bool = False) -> PushResult:
        base = self._baseline()
        if base is None and not force:
            raise StaleRemote(
                "No baseline yet, so it is unknown whether the server has work you would overwrite. "
                "Run `pull` first (it saves the baseline), or `push --force` after checking `status`."
            )
        rdb, rhist = self._fetch_remote()
        local, remote = self._local(), snapshot(rdb, rhist)
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
            self._save_baseline(self.local_db, self.local_history)
            return PushResult(noop=True, pushed=to_push)
        stamp = _stamp()
        self.transport.backup_remote(DB_REL, f"predeploy-{stamp}")
        if rhist.exists():
            self.transport.backup_remote(HISTORY_REL, f"bak-{stamp}")
        self.transport.put(self.local_db, DB_REL)
        if self.local_history.exists():
            self.transport.put(self.local_history, HISTORY_REL)
        self.transport.restart()
        self._save_baseline(self.local_db, self.local_history)
        return PushResult(noop=False, pushed=to_push)


# ── CLI ──────────────────────────────────────────────────────────────────────

def render_status(report: StatusReport) -> str:
    lines: list[str] = []
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
    root = PROJECT_ROOT
    return Sync(root=root, base_dir=root / BASE_DIRNAME,
                transport=SshTransport(args.host, args.remote_path))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("verb", choices=("status", "pull", "push"))
    parser.add_argument("--force", action="store_true", help="push even if the server changed since the baseline")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--remote-path", default=DEFAULT_REMOTE_ROOT)
    args = parser.parse_args(argv)
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
        print(f"Backups of the previous local files sit beside them as *.bak-<timestamp>.")
        return 0

    try:
        result = s.push(force=args.force)
    except StaleRemote as exc:
        print("Refused to push.")
        print(str(exc))
        return 2
    if result.noop:
        print("Server already matched local. Nothing pushed; baseline recorded.")
    else:
        print(f"Pushed. Server changed in {result.pushed.count} place(s) and {SERVICE_NAME} restarted:")
        print("\n".join(describe(result.pushed)))
        print("The server's previous files sit beside them as *.predeploy-<timestamp> and *.bak-<timestamp>.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
