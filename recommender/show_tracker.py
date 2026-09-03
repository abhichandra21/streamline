"""Release discovery and tracking rules for watched TV shows."""

import json
import logging
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from recommender.tmdb_client import TmdbRateLimitError

log = logging.getLogger("recommender.show_tracker")

FOLLOWED_REFRESH_AGE = timedelta(hours=24)
DISCOVERY_REFRESH_AGE = timedelta(days=7)
REQUEST_INTERVAL_SECONDS = 0.5
RATE_LIMIT_FALLBACK_SECONDS = 5.0
FAILED_REFRESH_AGE = timedelta(hours=24)


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value[:10])
    except (TypeError, ValueError):
        return None


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _archive_entry_key(entry: dict) -> tuple:
    content_type = entry.get("content_type", "tv")
    tmdb_id = entry.get("tmdb_id")
    if tmdb_id:
        return "tmdb", content_type, tmdb_id
    return "title", content_type, entry.get("title", "").strip().lower()


def merge_archive_entries(watch_entries: list[dict], manual_entries: list[dict]) -> list[dict]:
    """Union imported and manual history without losing the latest watch date."""
    merged = [dict(entry) for entry in watch_entries]
    positions: dict[tuple, int] = {}
    for index, entry in enumerate(merged):
        positions.setdefault(_archive_entry_key(entry), index)

    for manual in manual_entries:
        content_type = manual.get("content_type", "tv")
        tmdb_id = manual.get("tmdb_id")
        key = _archive_entry_key(manual)
        watched_at = manual.get("watched_at") or ""
        if key in positions:
            current = dict(merged[positions[key]])
            if watched_at > (current.get("last_watched") or ""):
                current["last_watched"] = watched_at
            merged[positions[key]] = current
            continue
        positions[key] = len(merged)
        merged.append({
            "tmdb_id": tmdb_id,
            "title": manual.get("title", ""),
            "content_type": content_type,
            "last_watched": watched_at,
        })

    return merged


def _manifest_path(cache_dir: str | Path) -> Path:
    return Path(cache_dir) / "manifest.json"


def _snapshot_path(cache_dir: str | Path, tmdb_id: int) -> Path:
    return Path(cache_dir) / "shows" / f"{tmdb_id}.json"


def _read_json(path: Path) -> dict | None:
    try:
        value = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    temporary.write_text(json.dumps(value, sort_keys=True))
    temporary.replace(path)


def load_snapshots(cache_dir: str | Path) -> dict[int, dict]:
    """Read valid cached show snapshots without making network requests."""
    shows_dir = Path(cache_dir) / "shows"
    if not shows_dir.exists():
        return {}
    snapshots = {}
    for path in shows_dir.glob("*.json"):
        snapshot = _read_json(path)
        if not snapshot or not isinstance(snapshot.get("tmdb_id"), int):
            log.warning("Ignoring invalid show release cache file %s", path)
            continue
        snapshots[snapshot["tmdb_id"]] = snapshot
    return snapshots


def _snapshot_is_fresh(snapshot: dict | None, now: datetime, max_age: timedelta) -> bool:
    fetched_at = _parse_datetime((snapshot or {}).get("fetched_at"))
    return bool(fetched_at and now - fetched_at < max_age)


def _failed_refresh_ids(manifest: dict) -> set[int]:
    return {
        tmdb_id
        for tmdb_id in manifest.get("failed_ids", [])
        if isinstance(tmdb_id, int)
    }


def _failed_refresh_is_due(manifest: dict, now: datetime) -> bool:
    if not _failed_refresh_ids(manifest):
        return False
    last_attempt = _parse_datetime(manifest.get("failed_attempt_at"))
    return not last_attempt or now - last_attempt >= FAILED_REFRESH_AGE


def _eligible_archive(archive_entries: list[dict], tracking_rows: list[dict]) -> dict[int, dict]:
    ignored = {row["tmdb_id"] for row in tracking_rows if row.get("state") == "ignored"}
    return {
        entry["tmdb_id"]: entry
        for entry in archive_entries
        if entry.get("content_type") == "tv"
        and isinstance(entry.get("tmdb_id"), int)
        and entry["tmdb_id"] not in ignored
    }


def last_refresh_at(cache_dir: str | Path) -> datetime | None:
    """Return when the last full discovery refresh completed, if ever."""
    manifest = _read_json(_manifest_path(cache_dir)) or {}
    return _parse_datetime(manifest.get("full_refresh_at"))


def refresh_is_due(
    archive_entries: list[dict],
    tracking_rows: list[dict],
    cache_dir: str | Path,
    now: datetime | None = None,
) -> bool:
    """Return whether a weekly discovery or daily followed-show refresh is due."""
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    manifest = _read_json(_manifest_path(cache_dir)) or {}
    last_full_refresh = _parse_datetime(manifest.get("full_refresh_at"))
    if not last_full_refresh or now - last_full_refresh >= DISCOVERY_REFRESH_AGE:
        return True

    snapshots = load_snapshots(cache_dir)
    eligible = _eligible_archive(archive_entries, tracking_rows)
    failed_ids = _failed_refresh_ids(manifest)
    failed_retry_due = _failed_refresh_is_due(manifest, now)
    if failed_retry_due and any(tmdb_id in eligible for tmdb_id in failed_ids):
        return True
    for row in tracking_rows:
        tmdb_id = row.get("tmdb_id")
        if row.get("state") != "following" or tmdb_id not in eligible:
            continue
        if (
            not _snapshot_is_fresh(snapshots.get(tmdb_id), now, FOLLOWED_REFRESH_AGE)
            and (tmdb_id not in failed_ids or failed_retry_due)
        ):
            return True
    return False


class _PacedRequests:
    def __init__(self, sleep):
        self.sleep = sleep
        self.has_requested = False

    def call(self, fn, *args):
        if self.has_requested:
            self.sleep(REQUEST_INTERVAL_SECONDS)
        self.has_requested = True
        try:
            return fn(*args)
        except TmdbRateLimitError as exc:
            wait_seconds = exc.retry_after_seconds
            if wait_seconds is None:
                wait_seconds = RATE_LIMIT_FALLBACK_SECONDS
            self.sleep(max(REQUEST_INTERVAL_SECONDS, wait_seconds))
            return fn(*args)


def _returning_season_numbers(
    entry: dict,
    tracking: dict | None,
    seasons: list[dict],
    series_details: dict,
    today: date,
    lookback_days: int,
) -> set[int]:
    if tracking and tracking.get("state") == "following":
        tracking_from = tracking.get("tracking_from_season") or 1
        return {
            season["season_number"]
            for season in seasons
            if season["season_number"] >= tracking_from
        }

    last_watched = _parse_date(entry.get("last_watched"))
    cutoff = today - timedelta(days=lookback_days)
    candidates = []
    for season in seasons:
        air_date = _parse_date(season.get("air_date"))
        signal_date = air_date or _parse_date(season.get("discovered_at"))
        if signal_date and signal_date >= cutoff and (not last_watched or signal_date > last_watched):
            candidates.append((signal_date, season["season_number"]))

    next_episode = series_details.get("next_episode_to_air") or {}
    next_episode_date = _parse_date(next_episode.get("air_date"))
    next_episode_season = next_episode.get("season_number")
    if (
        isinstance(next_episode_season, int)
        and next_episode_season > 0
        and next_episode_date
        and next_episode_date >= cutoff
        and (not last_watched or next_episode_date > last_watched)
    ):
        candidates.append((next_episode_date, next_episode_season))

    return {max(candidates)[1]} if candidates else set()


def _build_snapshot(
    entry: dict,
    tracking: dict | None,
    previous: dict | None,
    client,
    paced: _PacedRequests,
    now: datetime,
    lookback_days: int,
) -> dict:
    tmdb_id = entry["tmdb_id"]
    details = paced.call(client.fetch_tv_series_details, tmdb_id)
    if not isinstance(details, dict) or details.get("id") != tmdb_id:
        raise ValueError(f"TMDB returned invalid series details for {tmdb_id}")

    previous_seasons = {
        season.get("season_number"): season
        for season in (previous or {}).get("seasons", [])
    }
    seasons = []
    for raw in details.get("seasons", []):
        season_number = raw.get("season_number")
        if not isinstance(season_number, int) or season_number <= 0:
            continue
        old = previous_seasons.get(season_number) or {}
        seasons.append({
            "season_number": season_number,
            "air_date": raw.get("air_date"),
            "discovered_at": old.get("discovered_at") or now.date().isoformat(),
            "episodes": list(old.get("episodes") or []),
        })

    season_numbers = _returning_season_numbers(
        entry,
        tracking,
        seasons,
        details,
        now.date(),
        lookback_days,
    )
    for season in seasons:
        season_number = season["season_number"]
        if season_number not in season_numbers:
            continue
        season_details = paced.call(client.fetch_tv_season_details, tmdb_id, season_number)
        if not isinstance(season_details, dict) or season_details.get("season_number") != season_number:
            raise ValueError(f"TMDB returned invalid season details for {tmdb_id} season {season_number}")
        season["episodes"] = [
            {
                "episode_number": episode.get("episode_number"),
                "air_date": episode.get("air_date"),
            }
            for episode in season_details.get("episodes", [])
            if isinstance(episode.get("episode_number"), int) and episode["episode_number"] > 0
        ]

    return {
        "tmdb_id": tmdb_id,
        "title": details.get("name") or entry.get("title", ""),
        "status": details.get("status"),
        "in_production": bool(details.get("in_production")),
        "poster_path": details.get("poster_path"),
        "fetched_at": now.isoformat(),
        "next_episode_to_air": details.get("next_episode_to_air"),
        "seasons": seasons,
    }


def refresh_release_cache(
    archive_entries: list[dict],
    tracking_rows: list[dict],
    client,
    cache_dir: str | Path,
    now: datetime | None = None,
    sleep=time.sleep,
    lookback_days: int = 730,
    progress=None,
) -> dict:
    """Refresh due release snapshots, preserving cached data on failures.

    progress, when given, is called with (completed, total) after each show so
    callers can surface how far along the refresh is.
    """
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    manifest = _read_json(_manifest_path(cache_dir)) or {}
    last_full_refresh = _parse_datetime(manifest.get("full_refresh_at"))
    full_scan = not last_full_refresh or now - last_full_refresh >= DISCOVERY_REFRESH_AGE
    eligible = _eligible_archive(archive_entries, tracking_rows)
    tracking = {row["tmdb_id"]: row for row in tracking_rows}
    snapshots = load_snapshots(cache_dir)
    previous_failed_ids = _failed_refresh_ids(manifest)
    failed_retry_due = _failed_refresh_is_due(manifest, now)

    if full_scan:
        targets = []
        for tmdb_id in eligible:
            max_age = (
                FOLLOWED_REFRESH_AGE
                if (tracking.get(tmdb_id) or {}).get("state") == "following"
                else DISCOVERY_REFRESH_AGE
            )
            if not _snapshot_is_fresh(snapshots.get(tmdb_id), now, max_age):
                targets.append(tmdb_id)
    else:
        targets = []
        for tmdb_id, row in tracking.items():
            if row.get("state") != "following" or tmdb_id not in eligible:
                continue
            if (
                not _snapshot_is_fresh(snapshots.get(tmdb_id), now, FOLLOWED_REFRESH_AGE)
                and (tmdb_id not in previous_failed_ids or failed_retry_due)
            ):
                targets.append(tmdb_id)
        if failed_retry_due:
            targets.extend(
                tmdb_id
                for tmdb_id in sorted(previous_failed_ids)
                if tmdb_id in eligible and tmdb_id not in targets
            )

    result = {"refreshed": 0, "failed": 0, "aborted": False, "full_scan": full_scan}
    failed_ids = set() if full_scan else set(previous_failed_ids)
    paced = _PacedRequests(sleep)
    total = len(targets)
    if progress:
        progress(0, total)
    for completed, tmdb_id in enumerate(targets, start=1):
        try:
            snapshot = _build_snapshot(
                eligible[tmdb_id],
                tracking.get(tmdb_id),
                snapshots.get(tmdb_id),
                client,
                paced,
                now,
                lookback_days,
            )
        except TmdbRateLimitError:
            log.warning("TMDB rate limit persisted after retry; aborting release refresh")
            result["aborted"] = True
            return result
        except Exception as exc:
            log.warning("Release refresh failed for TMDB TV %d: %s", tmdb_id, exc)
            result["failed"] += 1
            failed_ids.add(tmdb_id)
            if progress:
                progress(completed, total)
            continue
        _write_json(_snapshot_path(cache_dir, tmdb_id), snapshot)
        snapshots[tmdb_id] = snapshot
        result["refreshed"] += 1
        failed_ids.discard(tmdb_id)
        if progress:
            progress(completed, total)

    if full_scan or failed_retry_due or result["failed"]:
        updated_manifest = dict(manifest)
        if full_scan:
            updated_manifest["full_refresh_at"] = now.isoformat()
        updated_manifest["failed_ids"] = sorted(failed_ids)
        if failed_ids:
            updated_manifest["failed_attempt_at"] = now.isoformat()
        else:
            updated_manifest.pop("failed_attempt_at", None)
        _write_json(_manifest_path(cache_dir), updated_manifest)
    return result


def _following_card(
    entry: dict,
    tracked: dict,
    snapshot: dict,
    today: date,
) -> tuple[str, dict] | None:
    tracking_from = tracked["tracking_from_season"]
    marker = None
    if tracked.get("caught_up_season") and tracked.get("caught_up_episode"):
        marker = (tracked["caught_up_season"], tracked["caught_up_episode"])

    aired = []
    future = []
    for season in snapshot.get("seasons", []):
        season_number = season.get("season_number", 0)
        if season_number <= 0 or season_number < tracking_from:
            continue
        for episode in season.get("episodes", []):
            episode_number = episode.get("episode_number", 0)
            episode_key = (season_number, episode_number)
            if episode_number <= 0 or (marker and episode_key <= marker):
                continue
            air_date = _parse_date(episode.get("air_date"))
            if not air_date:
                continue
            item = (air_date, season_number, episode_number)
            if air_date <= today:
                aired.append(item)
            else:
                future.append(item)

    next_episode = min(future) if future else None
    title = tracked.get("title") or entry["title"]
    poster_path = snapshot.get("poster_path")
    if aired:
        latest = max(aired, key=lambda item: (item[1], item[2]))
        return "ready_now", {
            "tmdb_id": entry["tmdb_id"],
            "title": title,
            "season_number": latest[1],
            "available_episode_count": len(aired),
            "latest_aired_episode": latest[2],
            "next_air_date": next_episode[0].isoformat() if next_episode else None,
            "next_episode_number": next_episode[2] if next_episode else None,
            "poster_path": poster_path,
        }

    if next_episode:
        return "coming_soon", {
            "tmdb_id": entry["tmdb_id"],
            "title": title,
            "season_number": next_episode[1],
            "next_air_date": next_episode[0].isoformat(),
            "next_episode_number": next_episode[2],
            "poster_path": poster_path,
        }

    future_seasons = []
    undated_seasons = []
    for season in snapshot.get("seasons", []):
        season_number = season.get("season_number", 0)
        if season_number <= 0 or season_number < tracking_from:
            continue
        season_air_date = _parse_date(season.get("air_date"))
        if season_air_date and season_air_date > today:
            future_seasons.append((season_air_date, season_number))
        elif not season_air_date and (not marker or season_number > marker[0]):
            undated_seasons.append(season_number)

    if future_seasons:
        season_air_date, season_number = min(future_seasons)
        return "coming_soon", {
            "tmdb_id": entry["tmdb_id"],
            "title": title,
            "season_number": season_number,
            "next_air_date": season_air_date.isoformat(),
            "next_episode_number": None,
            "poster_path": poster_path,
        }

    if snapshot.get("status") in {"Returning Series", "Planned", "In Production"}:
        return "coming_soon", {
            "tmdb_id": entry["tmdb_id"],
            "title": title,
            "season_number": max(undated_seasons) if undated_seasons else tracking_from,
            "next_air_date": None,
            "next_episode_number": None,
            "poster_path": poster_path,
        }

    if snapshot.get("status") in {"Ended", "Canceled"}:
        return "finished", {
            "tmdb_id": entry["tmdb_id"],
            "title": title,
            "poster_path": poster_path,
        }

    if marker:
        return "caught_up", {
            "tmdb_id": entry["tmdb_id"],
            "title": title,
            "poster_path": poster_path,
        }

    return None


def build_sections(
    archive_entries: list[dict],
    tracking_rows: list[dict],
    snapshots: dict[int, dict],
    today: date,
    lookback_days: int,
) -> dict[str, list[dict]]:
    sections: dict[str, list[dict]] = {
        "ready_now": [],
        "coming_soon": [],
        "might_be_back": [],
        "finished": [],
        "caught_up": [],
        "ignored": [],
    }
    tracking = {row["tmdb_id"]: row for row in tracking_rows}
    cutoff = today - timedelta(days=lookback_days)

    for entry in archive_entries:
        if entry.get("content_type") != "tv" or not entry.get("tmdb_id"):
            continue
        tmdb_id = entry["tmdb_id"]
        tracked = tracking.get(tmdb_id)
        snapshot = snapshots.get(tmdb_id)
        if tracked and tracked.get("state") == "ignored":
            regular_seasons = [
                season.get("season_number")
                for season in (snapshot or {}).get("seasons", [])
                if isinstance(season.get("season_number"), int)
                and season["season_number"] > 0
            ]
            sections["ignored"].append({
                "tmdb_id": tmdb_id,
                "title": tracked.get("title") or entry["title"],
                "season_number": (
                    tracked.get("tracking_from_season")
                    or (max(regular_seasons) if regular_seasons else None)
                ),
                "poster_path": (snapshot or {}).get("poster_path"),
            })
            continue
        if not snapshot:
            continue
        if tracked:
            result = _following_card(entry, tracked, snapshot, today)
            if result:
                section, card = result
                sections[section].append(card)
            continue

        last_watched = _parse_date(entry.get("last_watched"))
        returning_seasons = []
        for season in snapshot.get("seasons", []):
            if season.get("season_number", 0) <= 0:
                continue
            air_date = _parse_date(season.get("air_date"))
            signal_date = air_date or _parse_date(season.get("discovered_at"))
            if not signal_date or signal_date < cutoff:
                continue
            if last_watched and signal_date <= last_watched:
                continue
            returning_seasons.append((signal_date, season, air_date))

        if returning_seasons:
            _, season, air_date = max(returning_seasons, key=lambda item: item[0])
            sections["might_be_back"].append({
                "tmdb_id": tmdb_id,
                "title": entry["title"],
                "season_number": season["season_number"],
                "air_date": air_date.isoformat() if air_date else None,
                "poster_path": snapshot.get("poster_path"),
                "reason": (
                    "A new season was announced" if not air_date or air_date > today
                    else "A new season started"
                ),
            })
            continue

        next_episode = snapshot.get("next_episode_to_air") or {}
        next_air_date = _parse_date(next_episode.get("air_date"))
        next_season = next_episode.get("season_number")
        if (
            isinstance(next_season, int)
            and next_season > 0
            and next_air_date
            and next_air_date >= cutoff
            and (not last_watched or next_air_date > last_watched)
        ):
            sections["might_be_back"].append({
                "tmdb_id": tmdb_id,
                "title": entry["title"],
                "season_number": next_season,
                "air_date": next_air_date.isoformat(),
                "poster_path": snapshot.get("poster_path"),
                "reason": (
                    "A new episode is coming" if next_air_date > today
                    else "A new episode is available"
                ),
            })

    return sections
