#!/usr/bin/env python3
"""Build a demo copy of Streamline from made-up watch history, for public screenshots.

The copy lives outside the repo and shares no state with it. The watch history
is a generated Netflix export of famous titles; ratings, follows and the
watchlist are seeded below. Descriptions and taste rows come from llm-cache/,
and two past searches from searches.json, so a build makes no LLM calls.
TMDB metadata and IMDb ratings are fetched fresh, and are not committed.

    venv/bin/python demo/build.py /tmp/streamline-demo

Needs TMDB_API_KEY. Any LLM key in the environment is replaced with a dummy,
so a missing cache entry fails the build instead of spending money.
"""
from __future__ import annotations

import csv
import io
import os
import random
import re
import shutil
import subprocess
import sys
import tarfile
import zipfile
from datetime import datetime, timedelta
from pathlib import Path

DEMO_DIR = Path(__file__).resolve().parent
REPO = DEMO_DIR.parent

# Fixed so every build gives the same history and the same screenshots.
HISTORY_END = datetime(2026, 10, 1, 21, 0, 0)
SEED = 7

LOVED = ["Breaking Bad", "Better Call Saul", "Succession", "Slow Horses", "Line of Duty", "Broadchurch",
         "Happy Valley", "The Night Manager", "Fleabag", "Ted Lasso", "Severance", "Andor", "The Expanse",
         "Dark", "Chernobyl", "The Bear", "Shogun", "Sacred Games", "Delhi Crime", "Panchayat", "Scam 1992",
         "Planet Earth", "Last Week Tonight with John Oliver", "Only Murders in the Building",
         "The Dark Knight", "Inception", "Interstellar", "Arrival", "Dune", "Blade Runner 2049",
         "Mission Impossible Fallout", "Casino Royale", "Tinker Tailor Soldier Spy", "Knives Out",
         "Zodiac", "Parasite", "The Grand Budapest Hotel", "Paddington 2", "Hot Fuzz", "3 Idiots",
         "Andhadhun", "Dangal", "Spirited Away", "Top Gun Maverick"]
FINE = ["Ozark", "Downton Abbey", "La La Land", "Up"]
LESS = ["Money Heist", "The Crown", "Black Mirror"]
FOLLOW = ["Slow Horses", "The Bear", "Severance", "Only Murders in the Building",
          "Last Week Tonight with John Oliver", "Andor", "Grand Designs", "Great British Bake Off"]
WATCHLIST = [("The Penguin", "tv", 194764), ("Shrinking", "tv", 136311), ("Past Lives", "movie", 666277),
             ("The Holdovers", "movie", 840430), ("Bad Sisters", "tv", 199318)]


def _titles(name: str) -> list[str]:
    return [t.strip() for t in (DEMO_DIR / name).read_text().splitlines() if t.strip()]


def write_export(root: Path) -> None:
    """Write a Netflix-format export: a few episodes per show, one play per film."""
    rng = random.Random(SEED)
    rows = []
    for show in _titles("tv.txt"):
        start = HISTORY_END - timedelta(days=rng.randint(5, 540))
        for ep in range(1, rng.randint(4, 9)):
            rows.append((f"{show}: Season 1: Chapter {ep} (Episode {ep})", start + timedelta(days=ep), "00:48:00"))
    for film in _titles("movies.txt"):
        title = re.sub(r"\s\d{4}$", "", film)
        rows.append((title, HISTORY_END - timedelta(days=rng.randint(3, 700)), "01:55:00"))
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["Profile Name", "Start Time", "Duration", "Attributes", "Title",
                     "Supplemental Video Type", "Device Type", "Bookmark", "Latest Bookmark", "Country"])
    for title, ts, duration in sorted(rows, key=lambda r: r[1], reverse=True):
        writer.writerow(["Demo", ts.strftime("%Y-%m-%d %H:%M:%S"), duration, "", title, "", "TV",
                         duration, duration, "US (United States)"])
    export = root / "data/netflix/demo-export.zip"
    export.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(export, "w") as zf:
        zf.writestr("CONTENT_INTERACTION/ViewingActivity.csv", buf.getvalue())
    (root / "config.local.yaml").write_text(
        "platform_paths:\n  netflix: data/netflix/demo-export.zip\n  prime: null\n  apple_tv: null\n")


def copy_code(root: Path) -> None:
    """Copy the committed code (HEAD plus working-tree edits) without any local state."""
    files = subprocess.run(["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
                           cwd=REPO, check=True, capture_output=True).stdout.split(b"\0")
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for name in filter(None, files):
            path = REPO / name.decode()
            if path.is_file() and not name.startswith(b"demo/"):
                tar.add(path, arcname=name.decode())
    buf.seek(0)
    with tarfile.open(fileobj=buf) as tar:
        tar.extractall(root, filter="data")


def seed_state(root: Path) -> None:
    script = f"""
import json
from pathlib import Path
import config
from recommender import user_store

entries = json.loads(Path(config.WATCH_INDEX_PATH).read_text())
entries = entries["entries"] if isinstance(entries, dict) else entries
by_title = {{e["title"]: e for e in entries}}
missing = [t for t in {LOVED + FINE + LESS + FOLLOW!r} if t not in by_title]
if missing:
    raise SystemExit(f"Failed to match demo titles: {{missing}}")
db = config.EVENT_DB_PATH
for group, rating in (({LOVED!r}, "more"), ({FINE!r}, "neutral"), ({LESS!r}, "less")):
    for title in group:
        e = by_title[title]
        user_store.rate_title(db, e["title"], e["content_type"], rating, tmdb_id=e["tmdb_id"])
for title in {FOLLOW!r}:
    e = by_title[title]
    meta = json.loads(Path(config.CACHE_DIR, "tv", f"{{e['tmdb_id']}}.json").read_text())
    user_store.follow_show(db, e["title"], e["tmdb_id"], tracking_from_season=meta.get("number_of_seasons") or 1)
for title, content_type, tmdb_id in {WATCHLIST!r}:
    user_store.save_title(db, title, content_type, tmdb_id=tmdb_id)
# Real searches run once on this library and saved, so Searches has results without an LLM call.
from recommender import history
for entry in json.loads(Path({str(DEMO_DIR / "searches.json")!r}).read_text()):
    history.record(entry["query"], entry["results"], entry["provider"], "", metadata=entry)
"""
    subprocess.run([sys.executable, "-c", script], cwd=root, env=_env(), check=True)


def _env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("PLEX_", "STREAMLINE_", "GEMINI_", "OPENAI_"))}
    env["ANTHROPIC_API_KEY"] = "demo-build-makes-no-llm-calls"
    return env


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("usage: demo/build.py <target-dir>")
    if not os.environ.get("TMDB_API_KEY"):
        raise SystemExit("TMDB_API_KEY is not set")
    root = Path(sys.argv[1]).resolve()
    if root == REPO or REPO in root.parents:
        raise SystemExit("Target must be outside the repo")
    if root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True)

    copy_code(root)
    write_export(root)
    cache = root / "recommender/cache"
    shutil.copytree(DEMO_DIR / "llm-cache", cache)
    subprocess.run([sys.executable, "-m", "recommender.setup"], cwd=root, env=_env(), check=True)
    seed_state(root)
    print(f"Demo built in {root}")


if __name__ == "__main__":
    main()
