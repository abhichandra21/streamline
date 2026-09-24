"""Read-only JSON API and iCal feed, for a Home Assistant dashboard.

Every endpoint is built from local state and never calls the LLM, because a
dashboard polls on a schedule. Endpoints that serve On Deck data start a
release refresh through the same helper as /shows, so it calls TMDB only when
the page would.
"""

from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from flask import Blueprint, Response, jsonify

import config
from recommender import show_tracker
from recommender import user_store

api = Blueprint("api", __name__, url_prefix="/api")


@api.before_request
def _require_setup():
    # Matches /healthz, so the dashboard marks sensors unavailable instead of
    # showing zeros before setup has run.
    if not Path(config.WATCH_INDEX_PATH).exists() or not Path(config.TASTE_PROFILE_PATH).exists():
        return jsonify({"status": "not ready", "reason": "setup not run"}), 503
    return None


def _show_card(card: dict) -> dict:
    """One On Deck card with the same keys whichever section it came from."""
    tmdb_id = card.get("tmdb_id")
    poster_path = card.get("poster_path")
    if poster_path:
        poster_url = f"https://image.tmdb.org/t/p/w300{poster_path}"
    else:
        poster_url = web._get_poster_url(tmdb_id, "tv") if tmdb_id else None
    return {
        "tmdb_id": tmdb_id,
        "title": card.get("title"),
        "season_number": card.get("season_number"),
        "latest_aired_episode": card.get("latest_aired_episode"),
        "available_episode_count": card.get("available_episode_count"),
        # Ready-now cards say which season the next episode opens; a
        # coming-soon card's season is already the upcoming one.
        "next_season_number": card.get("next_season_number", card.get("season_number")),
        "next_episode_number": card.get("next_episode_number"),
        "next_air_date": card.get("next_air_date"),
        "poster_url": poster_url,
    }


def _on_deck() -> tuple[list[dict], list[dict], bool]:
    """Ready-now and coming-soon cards, soonest first, plus the refresh state."""
    sections, job_id = web._show_sections_with_refresh()
    dated, undated = web._split_coming_soon(sections.get("coming_soon", []), "soonest")
    ready_now = [_show_card(card) for card in sections.get("ready_now", [])]
    coming_soon = [_show_card(card) for card in dated + undated]
    return ready_now, coming_soon, bool(job_id)


def _upcoming(ready_now: list[dict], coming_soon: list[dict]) -> list[dict]:
    """Every card with a next air date, soonest first.

    A ready-now show can also have a dated episode still to come, and it
    belongs on the calendar as much as a coming-soon one.
    """
    dated = [card for card in ready_now + coming_soon if card["next_air_date"]]
    return sorted(dated, key=lambda card: card["next_air_date"])


def _shows_checked_at() -> str | None:
    checked_at = show_tracker.last_refresh_at(config.RELEASE_CACHE_DIR)
    return checked_at.isoformat() if checked_at else None


def _watchlist() -> list[dict]:
    web._ensure_user_store_once()
    items = []
    for row in user_store.list_saved_titles(config.EVENT_DB_PATH, status="watchlist"):
        tmdb_id = row.get("tmdb_id")
        content_type = row.get("content_type")
        items.append({
            "title": row.get("title"),
            "content_type": content_type,
            "tmdb_id": tmdb_id,
            "saved_at": row.get("saved_at"),
            "poster_url": (
                web._get_poster_url(tmdb_id, content_type)
                if tmdb_id and content_type in ("tv", "movie") else None
            ),
        })
    return items


@api.route("/summary")
def summary():
    ready_now, coming_soon, refreshing = _on_deck()
    entries = web._get_context().watch_index.entries
    next_up = next(iter(_upcoming(ready_now, coming_soon)), None)
    return jsonify({
        "ready_now_count": len(ready_now),
        "coming_soon_count": len(coming_soon),
        "watchlist_count": len(_watchlist()),
        "next_up": next_up,
        "library": {
            "total": len(entries),
            "tv": sum(1 for e in entries if e.get("content_type") == "tv"),
            "movies": sum(1 for e in entries if e.get("content_type") == "movie"),
        },
        "shows_checked_at": _shows_checked_at(),
        "shows_refreshing": refreshing,
        "taste_profile_built_at": web._profile_built_at(),
    })


@api.route("/on-deck")
def on_deck():
    ready_now, coming_soon, refreshing = _on_deck()
    return jsonify({
        "ready_now": ready_now,
        "coming_soon": coming_soon,
        "shows_checked_at": _shows_checked_at(),
        "shows_refreshing": refreshing,
    })


@api.route("/watchlist")
def watchlist():
    return jsonify({"watchlist": _watchlist()})


def _ical_text(value: str) -> str:
    return (
        value.replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\r\n", "\\n")
        .replace("\n", "\\n")
        .replace("\r", "\\n")
    )


def _ical_fold(line: str) -> list[str]:
    """Split a content line at 75 octets, as RFC 5545 requires."""
    folded = []
    current = ""
    for char in line:
        limit = 75 if not folded else 74
        if len((current + char).encode()) > limit:
            folded.append(current)
            current = char
        else:
            current += char
    folded.append(current)
    return [folded[0]] + [" " + part for part in folded[1:]]


@api.route("/coming-soon.ics")
def coming_soon_ics():
    ready_now, coming_soon, _ = _on_deck()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Streamline//Coming soon//EN",
        "CALSCALE:GREGORIAN",
        "X-WR-CALNAME:Streamline coming soon",
    ]
    for card in _upcoming(ready_now, coming_soon):
        try:
            air_date = date.fromisoformat(card["next_air_date"][:10])
        except ValueError:
            continue
        season = card["next_season_number"]
        episode = card["next_episode_number"]
        season_label = f"S{season}" if season is not None else ""
        if episode is None:
            label = f"{season_label} premiere".strip()
            uid_episode = "premiere"
        else:
            label = f"{season_label}E{episode}"
            uid_episode = f"e{episode}"
        lines += [
            "BEGIN:VEVENT",
            # Stable per episode, so calendar clients update an event in place
            # when its date moves instead of adding a duplicate.
            f"UID:streamline-{card['tmdb_id']}-s{season if season is not None else 'x'}-{uid_episode}",
            f"DTSTAMP:{stamp}",
            f"DTSTART;VALUE=DATE:{air_date.strftime('%Y%m%d')}",
            f"DTEND;VALUE=DATE:{(air_date + timedelta(days=1)).strftime('%Y%m%d')}",
            f"SUMMARY:{_ical_text(card['title'] or 'Untitled')} {label}",
            "TRANSP:TRANSPARENT",
            "END:VEVENT",
        ]
    lines.append("END:VCALENDAR")
    body = "".join(part + "\r\n" for line in lines for part in _ical_fold(line))
    return Response(body, content_type="text/calendar; charset=utf-8")


# Imported last: web.py registers this blueprint at the end of its own
# import, so either module can be imported first.
from recommender import web  # noqa: E402
