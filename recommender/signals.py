import math
from collections import defaultdict
from datetime import datetime

from .ingestion.base import WatchEvent
from .tmdb_client import TmdbMetadata

DEFAULT_TV_RUNTIME = 45      # minutes, used when TMDB has no data
DEFAULT_MOVIE_RUNTIME = 90
REWATCH_SATURATION = 5       # log scale saturates at ~5 rewatches


def compute_scores(
    events: list[WatchEvent],
    metadata: dict[str, TmdbMetadata],
    recency_half_life_days: int = 90,
) -> dict[str, float]:
    """
    Returns {series_name_or_title: implicit_score (0.0–1.0)}.
    Groups TV events by series_name; movies by title.
    """
    today = datetime.now()

    grouped: dict[str, list[WatchEvent]] = defaultdict(list)
    for e in events:
        key = e.series_name if e.content_type == "tv" else e.title
        grouped[key].append(e)

    scores: dict[str, float] = {}
    for key, evts in grouped.items():
        content_type = evts[0].content_type
        meta = metadata.get(key)

        # Runtime
        if meta and meta.runtime_minutes:
            runtime = meta.runtime_minutes
        else:
            runtime = DEFAULT_TV_RUNTIME if content_type == "tv" else DEFAULT_MOVIE_RUNTIME

        # Completion rate (average across all watch events)
        completions = [
            min(1.0, e.watched_duration.total_seconds() / 60 / runtime)
            for e in evts
        ]
        completion = sum(completions) / len(completions)

        # Rewatch bonus
        if content_type == "tv":
            episode_counts: dict[str, int] = defaultdict(int)
            for e in evts:
                episode_counts[e.title] += 1
            rewatch_count = sum(max(0, c - 1) for c in episode_counts.values())
        else:
            rewatch_count = max(0, len(evts) - 1)
        rewatch_bonus = min(1.0, math.log(rewatch_count + 1) / math.log(REWATCH_SATURATION))

        # Recency
        most_recent = max(e.timestamp for e in evts)
        days_since = max(0, (today - most_recent).days)
        recency = math.exp(-days_since / recency_half_life_days)

        scores[key] = 0.5 * completion + 0.3 * rewatch_bonus + 0.2 * recency

    return scores
