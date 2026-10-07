import math
from collections import defaultdict
from datetime import datetime

import config
from .ingestion.base import WatchEvent
from .tmdb_client import TmdbMetadata


# Bulk sources that say a title was seen, not that it was chosen and watched.
LIST_PLATFORMS = frozenset({"manual", "archive"})
LIST_WEIGHT = 0.3
STRONG_WEIGHT = 2.0


def profile_weights(
    events: list[WatchEvent],
    followed_keys: set[str] | frozenset[str],
    more_keys: set[str] | frozenset[str],
) -> dict[str, float]:
    """Real viewing sets the weight; the downloads list and Seen it taps only fill in.

    More and Follow are the owner's own word, so they lift any title to full strength.
    """
    grouped: dict[str, list[WatchEvent]] = defaultdict(list)
    for e in events:
        grouped[e.series_name if e.content_type == "tv" else e.title].append(e)
    weights = {}
    for key, evts in grouped.items():
        base = 1.0 if any(e.platform not in LIST_PLATFORMS for e in evts) else LIST_WEIGHT
        if key in more_keys or key in followed_keys:
            base = max(base, 1.0) * STRONG_WEIGHT
        weights[key] = base
    return weights


def compute_scores(
    events: list[WatchEvent],
    metadata: dict[str | tuple[str, str], TmdbMetadata],
    recency_half_life_days: int = 90,
    followed_keys: set[str] | frozenset[str] = frozenset(),
    more_keys: set[str] | frozenset[str] = frozenset(),
) -> dict[str, float]:
    """
    Returns {series_name_or_title: implicit_score}.
    Groups TV events by series_name; movies by title.
    Unless scoring.use_viewing_signals is on, scores come from profile_weights().
    When on: completion, rewatch and recency weights from config.yaml (0.0-1.0).
    """
    if not config.USE_VIEWING_SIGNALS:
        return profile_weights(events, followed_keys, more_keys)

    today = datetime.now()

    grouped: dict[str, list[WatchEvent]] = defaultdict(list)
    for e in events:
        key = e.series_name if e.content_type == "tv" else e.title
        grouped[key].append(e)

    scores: dict[str, float] = {}
    for key, evts in grouped.items():
        content_type = evts[0].content_type
        meta = metadata.get((key, content_type)) or metadata.get(key)

        # Runtime
        if meta and meta.runtime_minutes:
            runtime = meta.runtime_minutes
        else:
            runtime = config.DEFAULT_TV_RUNTIME if content_type == "tv" else config.DEFAULT_MOVIE_RUNTIME

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
        rewatch_bonus = min(1.0, math.log(rewatch_count + 1) / math.log(config.REWATCH_SATURATION))

        # Recency
        most_recent = max(e.timestamp for e in evts)
        days_since = max(0, (today - most_recent).days)
        recency = 0.5 ** (days_since / recency_half_life_days)

        scores[key] = (config.WEIGHT_COMPLETION * completion
                        + config.WEIGHT_REWATCH * rewatch_bonus
                        + config.WEIGHT_RECENCY * recency)

    return scores
