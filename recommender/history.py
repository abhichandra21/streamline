"""Query history storage.

Stores the last N queries with results, timestamps, and usage stats
in a JSON file under the cache directory. Gitignored (personal data).
"""

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

import config
from .models import Recommendation

log = logging.getLogger("recommender.history")

HISTORY_PATH = Path(config.ENRICHMENT_CACHE_DIR).parent / "query_history.json"
MAX_ENTRIES = 100


def _load_raw() -> list[dict]:
    if HISTORY_PATH.exists():
        try:
            return json.loads(HISTORY_PATH.read_text())
        except (json.JSONDecodeError, OSError):
            log.warning("Corrupt query history, resetting.")
            return []
    return []


def record(
    query: str,
    results: list[Recommendation],
    provider: str,
    usage_summary: str,
) -> None:
    """Append a query + results to history, capped at MAX_ENTRIES."""
    entries = _load_raw()

    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "query": query,
        "provider": provider,
        "results": [
            {
                "title": r.title,
                "content_type": r.content_type,
                "score": round(r.score, 3),
                "vote_average": r.vote_average,
                "explanation": r.explanation,
                "streaming_providers": r.streaming_providers[:4],
            }
            for r in results
        ],
        "usage": usage_summary,
    }
    entries.append(entry)

    # Keep only the most recent entries
    if len(entries) > MAX_ENTRIES:
        entries = entries[-MAX_ENTRIES:]

    HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    HISTORY_PATH.write_text(json.dumps(entries, indent=2))


def load(limit: int | None = None) -> list[dict]:
    """Load history entries, most recent first."""
    entries = _load_raw()
    entries.reverse()
    if limit:
        entries = entries[:limit]
    return entries
