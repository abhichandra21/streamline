"""Feedback storage and application for the taste profile."""

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("recommender.feedback")

LIKED_MULTIPLIER = 1.3
DISLIKED_MULTIPLIER = 0.5


def load(path: str) -> dict:
    """Load feedback from JSON file. Returns empty structure if file doesn't exist."""
    p = Path(path)
    if p.exists():
        return json.loads(p.read_text())
    return {"ratings": [], "additions": []}


def save(feedback: dict, path: str) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(feedback, indent=2))


def add_rating(feedback: dict, title: str, rating: str,
               tmdb_id: int | None = None, content_type: str | None = None) -> None:
    """Add or update a liked/disliked rating. rating must be 'liked' or 'disliked'."""
    # Remove any existing entry for this title so we don't duplicate.
    feedback["ratings"] = [r for r in feedback["ratings"] if r["title"].lower() != title.lower()]
    entry: dict = {
        "title": title,
        "rating": rating,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    if tmdb_id is not None:
        entry["tmdb_id"] = tmdb_id
    if content_type is not None:
        entry["content_type"] = content_type
    feedback["ratings"].append(entry)
    log.debug("Feedback: %s rated as %s", title, rating)


def add_addition(feedback: dict, title: str, content_type: str) -> None:
    """Add a title to the watch history additions list."""
    # Deduplicate by title + content_type.
    feedback["additions"] = [
        a for a in feedback["additions"]
        if not (a["title"].lower() == title.lower() and a.get("content_type") == content_type)
    ]
    feedback["additions"].append({
        "title": title,
        "content_type": content_type,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })
    log.debug("Feedback: added %s (%s) to watch history", title, content_type)


def apply_score_multipliers(scores: dict[str, float], feedback: dict) -> dict[str, float]:
    """Apply liked/disliked multipliers to engagement scores.

    Liked titles get a 1.3x boost; disliked titles get a 0.5x penalty.
    Scores are capped at 1.0 after boosting.
    """
    modified = dict(scores)
    for entry in feedback.get("ratings", []):
        title = entry["title"]
        rating = entry.get("rating")
        if title in modified:
            if rating == "liked":
                modified[title] = min(1.0, modified[title] * LIKED_MULTIPLIER)
            elif rating == "disliked":
                modified[title] = modified[title] * DISLIKED_MULTIPLIER
    return modified


def get_disliked_titles(feedback: dict) -> list[str]:
    """Return list of titles the user explicitly disliked."""
    return [
        e["title"] for e in feedback.get("ratings", [])
        if e.get("rating") == "disliked"
    ]
