"""Merge user-generated state (watchlist, ratings, manual archive, search
history) between two copies of the same Streamline install.

Used by the streamline-deploy skill so that deploying never silently
overwrites watchlist/rating/history changes made on the other side (local
vs. the home-server web UI). Derived/cache data (TMDB metadata, enrichments,
provider availability) is not touched here -- it's fine to overwrite since
it's rebuilt from setup, not user input.

Usage:
    python3 tools/merge_user_state.py db <local db.sqlite path> <other db.sqlite path>
    python3 tools/merge_user_state.py history <local query_history.json> <other query_history.json>

The "db" form expects the app's user-store database (default path:
data/streamline.db, see config.py's EVENT_DB_PATH) -- not
recommender/cache/events.db, which is an unrelated always-empty file.

Both forms merge "other" into "local" in place. Run the merged local file
back through rsync/scp to push the union to the other side.
"""

import json
import re
import sqlite3
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

MERGE_TABLES = {
    "saved_titles": "updated_at",
    "title_ratings": "updated_at",
}
APPEND_ONLY_TABLES = ["manual_archive_entries"]

HISTORY_MAX_ENTRIES = 100


def _normalize(title: str) -> str:
    title = title.lower()
    title = re.sub(r"\s*\([^)]*\)", "", title)
    return title.strip()


def _row_key(row: sqlite3.Row) -> tuple:
    if row["tmdb_id"] is not None:
        return ("tmdb", row["content_type"], row["tmdb_id"])
    return ("title", row["content_type"], row["normalized_title"] or _normalize(row["title"]))


def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=30.0)
    conn.row_factory = sqlite3.Row
    return conn


def merge_db(local_path: str, other_path: str) -> None:
    from recommender.user_store import init_db

    init_db(local_path)
    local = _connect(local_path)
    other = _connect(other_path)
    try:
        for table, ts_col in MERGE_TABLES.items():
            local_rows = {_row_key(r): dict(r) for r in local.execute(f"SELECT * FROM {table}")}
            other_rows = other.execute(f"SELECT * FROM {table}").fetchall()
            for orow in other_rows:
                key = _row_key(orow)
                lrow = local_rows.get(key)
                if lrow is None:
                    cols = [c for c in orow.keys() if c != "id"]
                    placeholders = ", ".join("?" for _ in cols)
                    local.execute(
                        f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({placeholders})",
                        [orow[c] for c in cols],
                    )
                    continue
                if orow[ts_col] > lrow[ts_col]:
                    cols = [c for c in orow.keys() if c not in ("id", "saved_at", "rated_at")]
                    set_clause = ", ".join(f"{c} = ?" for c in cols)
                    local.execute(
                        f"UPDATE {table} SET {set_clause} WHERE id = ?",
                        [orow[c] for c in cols] + [lrow["id"]],
                    )

        for table in APPEND_ONLY_TABLES:
            local_keys = {_row_key(r) for r in local.execute(f"SELECT * FROM {table}")}
            for orow in other.execute(f"SELECT * FROM {table}").fetchall():
                if _row_key(orow) in local_keys:
                    continue
                cols = [c for c in orow.keys() if c != "id"]
                placeholders = ", ".join("?" for _ in cols)
                local.execute(
                    f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({placeholders})",
                    [orow[c] for c in cols],
                )
        local.commit()
    finally:
        local.close()
        other.close()


def merge_history(local_path: str, other_path: str) -> None:
    local_entries = json.loads(Path(local_path).read_text()) if Path(local_path).exists() else []
    other_entries = json.loads(Path(other_path).read_text()) if Path(other_path).exists() else []

    by_key = {(e.get("timestamp"), e.get("query")): e for e in local_entries}
    for e in other_entries:
        key = (e.get("timestamp"), e.get("query"))
        by_key.setdefault(key, e)

    merged = sorted(by_key.values(), key=lambda e: e.get("timestamp") or "")
    if len(merged) > HISTORY_MAX_ENTRIES:
        merged = merged[-HISTORY_MAX_ENTRIES:]

    Path(local_path).write_text(json.dumps(merged, indent=2))


def main() -> int:
    if len(sys.argv) != 4 or sys.argv[1] not in ("db", "history"):
        print(__doc__)
        return 1
    kind, local_path, other_path = sys.argv[1], sys.argv[2], sys.argv[3]
    if kind == "db":
        merge_db(local_path, other_path)
    else:
        merge_history(local_path, other_path)
    print(f"Merged {other_path} into {local_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
