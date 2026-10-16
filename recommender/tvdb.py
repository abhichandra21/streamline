"""A followed show's regular air time, from TheTVDB.

TMDB only knows air dates. TheTVDB lists each show's release time, including
the hour a streaming service drops new episodes, so that time is used for every
episode. TheTVDB gives no timezone, so the time is read in the main timezone of
the show's home country. Its editors enter a foreign show's time in local time
even when a US streamer carries it (Lupin on Netflix is 09:00, Paris time), so
the home country is right more often than the network's country, which is what
Sonarr uses.

Apple TV lists its shows at midnight on the release date but puts them out at
9 PM Eastern the evening before (2026), so that is used instead. Netflix puts
everything out at 3 AM Eastern worldwide, whatever time TheTVDB lists.

Needs TVDB_API_KEY. Every failure here leaves the caller on date-only behavior
and never fails a refresh.
"""

import json
import logging
import time
from pathlib import Path

import requests

log = logging.getLogger("recommender.tvdb")

TVDB_BASE = "https://api4.thetvdb.com/v4"
TIMEOUT_SECONDS = 10

# A show's slot rarely moves, but a new season can move it.
RECHECK_AGE_SECONDS = 7 * 86400

# Apple TV's real release slot for shows TheTVDB lists at midnight.
APPLE_EVENING_BEFORE = {"time": "21:00", "timezone": "America/New_York", "days_early": 1}
# Netflix's worldwide release time (midnight Pacific).
NETFLIX_RELEASE = {"time": "03:00", "timezone": "America/New_York"}

# TheTVDB country code -> the timezone its shows are scheduled in.
COUNTRY_TIMEZONES = {
    "usa": "America/New_York",
    "can": "America/Toronto",
    "gbr": "Europe/London",
    "irl": "Europe/Dublin",
    "aus": "Australia/Sydney",
    "nzl": "Pacific/Auckland",
    "fra": "Europe/Paris",
    "deu": "Europe/Berlin",
    "esp": "Europe/Madrid",
    "ita": "Europe/Rome",
    "nld": "Europe/Amsterdam",
    "bel": "Europe/Brussels",
    "dnk": "Europe/Copenhagen",
    "swe": "Europe/Stockholm",
    "nor": "Europe/Oslo",
    "fin": "Europe/Helsinki",
    "isl": "Atlantic/Reykjavik",
    "isr": "Asia/Jerusalem",
    "ind": "Asia/Kolkata",
    "jpn": "Asia/Tokyo",
    "kor": "Asia/Seoul",
}


class TvdbClient:
    def __init__(self, cache_dir: str | Path, api_key: str):
        self.cache_dir = Path(cache_dir)
        self.api_key = api_key
        self._token = None
        self._login_error = None

    def _login(self) -> str:
        # One login per client; a bad key fails every lookup without asking again.
        if self._token:
            return self._token
        if self._login_error:
            raise self._login_error
        try:
            resp = requests.post(f"{TVDB_BASE}/login", json={"apikey": self.api_key}, timeout=TIMEOUT_SECONDS)
            if not resp.ok:
                raise requests.HTTPError(f"TheTVDB login HTTP {resp.status_code}", response=resp)
            self._token = resp.json()["data"]["token"]
        except Exception as exc:
            self._login_error = exc
            raise
        return self._token

    def _series(self, tvdb_id: int) -> dict | None:
        """Series record, or None when TheTVDB has no such show (404)."""
        resp = requests.get(
            f"{TVDB_BASE}/series/{tvdb_id}/extended",
            params={"short": "true"},
            headers={"Authorization": f"Bearer {self._login()}"},
            timeout=TIMEOUT_SECONDS,
        )
        if resp.status_code == 404:
            return None
        if not resp.ok:
            raise requests.HTTPError(f"TheTVDB HTTP {resp.status_code} for series {tvdb_id}", response=resp)
        return resp.json().get("data")

    @staticmethod
    def _schedule(series: dict | None) -> dict | None:
        if not series:
            return None
        clock = (series.get("airsTime") or "").strip()
        network = series.get("latestNetwork") or series.get("originalNetwork") or {}
        network_name = (network.get("name") or "").lower()
        if network_name.startswith("netflix"):
            return dict(NETFLIX_RELEASE)
        if clock == "00:00" and network_name.startswith("apple tv"):
            return dict(APPLE_EVENING_BEFORE)
        country = series.get("originalCountry") or network.get("country") or ""
        zone = COUNTRY_TIMEZONES.get(country.strip().lower())
        return {"time": clock, "timezone": zone} if clock and zone else None

    def show_time(self, tmdb_id: int, fetch_external_ids) -> dict | None:
        """{"time": "23:00", "timezone": "America/New_York"}, or None when the show has no set time.

        An optional "days_early" moves the time to that many days before the air date.

        The answer is re-checked after RECHECK_AGE_SECONDS. fetch_external_ids is
        only called when TheTVDB has to be asked. On any failure the last saved
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
            log.warning("Ignoring unreadable TheTVDB cache file %s: %s", path, exc)
        try:
            tvdb_id = (fetch_external_ids() or {}).get("tvdb_id")
            answer = self._schedule(self._series(tvdb_id)) if tvdb_id else None
        except Exception as exc:
            log.warning("TheTVDB lookup failed for TMDB TV %d: %s", tmdb_id, type(exc).__name__)
            return cached
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"show_time": answer}))
        return answer
