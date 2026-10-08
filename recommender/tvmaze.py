"""A followed show's regular air time, from TVmaze.

TMDB only knows air dates. A show that airs at a set time keeps that time, and
TVmaze lists it as the show's schedule weeks ahead, so the schedule time is
used for every episode. Data is CC BY-SA 4.0, credited on On Deck. Every
failure here leaves the caller on date-only behavior and never fails a refresh.
"""

import json
import logging
import time
from pathlib import Path

import requests

log = logging.getLogger("recommender.tvmaze")

TVMAZE_BASE = "https://api.tvmaze.com"
TIMEOUT_SECONDS = 10

# A show's slot rarely moves, but a new season can move it.
RECHECK_AGE_SECONDS = 7 * 86400


class TvmazeClient:
    def __init__(self, cache_dir: str | Path):
        self.cache_dir = Path(cache_dir)

    def _get(self, path: str, params: dict) -> dict | None:
        """JSON body, or None when TVmaze has no such show (404)."""
        resp = requests.get(f"{TVMAZE_BASE}/{path}", params=params, timeout=TIMEOUT_SECONDS)
        if resp.status_code == 404:
            return None
        if not resp.ok:
            raise requests.HTTPError(f"TVmaze HTTP {resp.status_code} for {path}", response=resp)
        return resp.json()

    def _lookup(self, external_ids: dict) -> dict | None:
        # IMDb first, TVDB as the fallback.
        for key, param in (("imdb_id", "imdb"), ("tvdb_id", "thetvdb")):
            value = external_ids.get(key)
            if value:
                show = self._get("lookup/shows", {param: value})
                if isinstance(show, dict):
                    return show
        return None

    @staticmethod
    def _schedule(show: dict | None) -> dict | None:
        if not show:
            return None
        clock = ((show.get("schedule") or {}).get("time") or "").strip()
        channel = show.get("network") or show.get("webChannel") or {}
        zone = ((channel.get("country") or {}).get("timezone") or "").strip()
        return {"time": clock, "timezone": zone} if clock and zone else None

    def show_time(self, tmdb_id: int, fetch_external_ids) -> dict | None:
        """{"time": "23:00", "timezone": "America/New_York"}, or None when the show has no set time.

        The answer is re-checked after RECHECK_AGE_SECONDS. fetch_external_ids is
        only called when TVmaze has to be asked. On any failure the last saved
        answer is kept.
        """
        path = self.cache_dir / f"{tmdb_id}.json"
        cached = None
        try:
            cached = json.loads(path.read_text())["show_time"]
            if time.time() - path.stat().st_mtime < RECHECK_AGE_SECONDS:
                return cached
        except FileNotFoundError:
            pass
        except (OSError, ValueError, KeyError, TypeError) as exc:
            log.warning("Ignoring unreadable TVmaze cache file %s: %s", path, exc)
        try:
            answer = self._schedule(self._lookup(fetch_external_ids() or {}))
        except Exception as exc:
            log.warning("TVmaze lookup failed for TMDB TV %d: %s", tmdb_id, type(exc).__name__)
            return cached
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"show_time": answer}))
        return answer
