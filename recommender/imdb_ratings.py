"""IMDb ratings from IMDb's daily ratings dataset.

TMDB stays the catalogue and the identity of every title. This module only
answers "what rating and vote count does IMDb have for this IMDb ID", from a
local copy of title.ratings.tsv.gz (free for personal, non-commercial use).

The whole file is loaded into one SQLite table. A refresh builds a new database
beside the old one and swaps it in only when complete, so readers never see a
half-written table and a failed download leaves the previous copy in place.
"""

from __future__ import annotations

import gzip
import logging
import os
import sqlite3
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterable

import requests

log = logging.getLogger(__name__)

DATASET_URL = "https://datasets.imdbws.com/title.ratings.tsv.gz"
# IMDb regenerates the file daily; new releases move fast in their first weeks.
REFRESH_AGE = timedelta(days=1)
DOWNLOAD_TIMEOUT_SECONDS = 60
# Background job label in the web UI; tests wait on jobs carrying it.
REFRESH_JOB_LABEL = "refreshing IMDb ratings"
_EXPECTED_HEADER = ["tconst", "averageRating", "numVotes"]
# SQLite's default bound-parameter limit is 999 on older builds.
_LOOKUP_CHUNK = 500


@dataclass(frozen=True)
class ImdbRating:
    rating: float
    votes: int


def _download(dest: Path) -> None:
    with requests.get(DATASET_URL, stream=True, timeout=DOWNLOAD_TIMEOUT_SECONDS) as response:
        response.raise_for_status()
        with open(dest, "wb") as f:
            for chunk in response.iter_content(chunk_size=1 << 20):
                f.write(chunk)


def _parse_imdb_id(tconst: str) -> int | None:
    if not tconst.startswith("tt"):
        return None
    try:
        return int(tconst[2:])
    except ValueError:
        return None


def _read_rows(gz_path: Path) -> Iterable[tuple[int, float, int]]:
    with gzip.open(gz_path, "rt", encoding="utf-8") as f:
        header = f.readline().rstrip("\n").split("\t")
        if header != _EXPECTED_HEADER:
            raise ValueError(f"Unexpected IMDb ratings header: {header}")
        for line in f:
            parts = line.rstrip("\n").split("\t")
            if len(parts) != 3:
                continue
            imdb_id = _parse_imdb_id(parts[0])
            if imdb_id is None:
                continue
            try:
                yield imdb_id, float(parts[1]), int(parts[2])
            except ValueError:
                continue


def refresh(db_path: str | Path, download: Callable[[Path], None] | None = None) -> int:
    """Download the dataset and replace the local ratings database.

    Returns the number of rows loaded. Raises on download or parse failure,
    in which case the existing database is untouched.
    """
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    # Unique names beside the target, so a CLI and a web refresh cannot collide
    # and the final rename stays on one filesystem.
    fd, gz_name = tempfile.mkstemp(dir=db_path.parent, prefix=".imdb-", suffix=".tsv.gz")
    os.close(fd)
    fd, tmp_db_name = tempfile.mkstemp(dir=db_path.parent, prefix=".imdb-", suffix=".db")
    os.close(fd)
    gz_path, tmp_db = Path(gz_name), Path(tmp_db_name)
    try:
        (download or _download)(gz_path)
        conn = sqlite3.connect(tmp_db)
        try:
            conn.execute(
                "CREATE TABLE ratings ("
                "imdb_id INTEGER PRIMARY KEY, rating REAL NOT NULL, votes INTEGER NOT NULL"
                ") WITHOUT ROWID"
            )
            conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            conn.executemany("INSERT OR REPLACE INTO ratings VALUES (?, ?, ?)", _read_rows(gz_path))
            count = conn.execute("SELECT COUNT(*) FROM ratings").fetchone()[0]
            if count == 0:
                raise ValueError("IMDb ratings dataset contained no rows")
            conn.execute(
                "INSERT INTO meta VALUES ('refreshed_at', ?)",
                (datetime.now(timezone.utc).isoformat(),),
            )
            conn.commit()
        finally:
            conn.close()
        # mkstemp creates owner-only files; match the rest of the cache so a
        # web service running as another user can still read the ratings.
        tmp_db.chmod(0o644)
        tmp_db.replace(db_path)
        log.info("Loaded %d IMDb ratings into %s", count, db_path)
        return count
    finally:
        gz_path.unlink(missing_ok=True)
        tmp_db.unlink(missing_ok=True)


def _connect_readonly(db_path: str | Path) -> sqlite3.Connection | None:
    if not Path(db_path).exists():
        return None
    try:
        return sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        log.warning("IMDb ratings database unreadable at %s: %s", db_path, exc)
        return None


def refreshed_at(db_path: str | Path) -> datetime | None:
    """When the local copy was built, or None when there is no usable copy."""
    conn = _connect_readonly(db_path)
    if conn is None:
        return None
    try:
        row = conn.execute("SELECT value FROM meta WHERE key = 'refreshed_at'").fetchone()
        return datetime.fromisoformat(row[0]) if row else None
    except (sqlite3.Error, ValueError) as exc:
        log.warning("IMDb ratings database unreadable at %s: %s", db_path, exc)
        return None
    finally:
        conn.close()


def refresh_is_due(db_path: str | Path, now: datetime | None = None) -> bool:
    built = refreshed_at(db_path)
    now = now or datetime.now(timezone.utc)
    return built is None or now - built >= REFRESH_AGE


def lookup(db_path: str | Path, imdb_ids: Iterable[str]) -> dict[str, ImdbRating]:
    """Ratings for the given IMDb IDs ("tt..."). Missing IDs are left out.

    Returns an empty mapping when there is no local copy, so callers fall back
    to TMDB rather than failing.
    """
    wanted = {imdb_id: n for imdb_id in set(imdb_ids)
              if imdb_id and (n := _parse_imdb_id(imdb_id)) is not None}
    if not wanted:
        return {}
    conn = _connect_readonly(db_path)
    if conn is None:
        return {}
    by_number = {n: imdb_id for imdb_id, n in wanted.items()}
    numbers = list(by_number)
    found: dict[str, ImdbRating] = {}
    try:
        for start in range(0, len(numbers), _LOOKUP_CHUNK):
            chunk = numbers[start:start + _LOOKUP_CHUNK]
            placeholders = ",".join("?" * len(chunk))
            for n, rating, votes in conn.execute(
                f"SELECT imdb_id, rating, votes FROM ratings WHERE imdb_id IN ({placeholders})",
                chunk,
            ):
                found[by_number[n]] = ImdbRating(rating=rating, votes=votes)
    except sqlite3.Error as exc:
        log.warning("IMDb ratings lookup failed at %s: %s", db_path, exc)
        return {}
    finally:
        conn.close()
    return found
