"""Count a film franchise once in the taste profile.

Liking X-Men means liking X-Men, however many of its films were watched, so a
TMDB collection becomes one profile entry instead of one entry per film.
"""
import json
from collections import defaultdict
from pathlib import Path


def _collection(cache_dir: Path, tmdb_id: int) -> tuple[int, str] | None:
    path = cache_dir / "movie" / f"{tmdb_id}.json"
    try:
        collection = json.loads(path.read_text()).get("belongs_to_collection")
    except (OSError, ValueError):
        return None
    if not collection or not collection.get("id"):
        return None
    return collection["id"], collection.get("name") or f"Collection {collection['id']}"


def collapse_collections(
    scores: dict[str, float],
    tmdb_ids: dict[str, int],
    enrichments: dict[str, str],
    cache_dir: Path,
) -> tuple[dict[str, float], dict[str, str]]:
    """Merge watched films of one collection into a "<name> (<n> films)" entry.

    tmdb_ids maps film score keys to TMDB movie IDs. The entry takes the highest
    member score, so one More-rated film carries the whole franchise. Reads the
    TMDB cache directly, because metadata is not loaded in --refresh-profile mode.
    """
    groups: dict[tuple[int, str], list[str]] = defaultdict(list)
    for title in scores:
        if title in tmdb_ids:
            found = _collection(cache_dir, tmdb_ids[title])
            if found:
                groups[found].append(title)

    scores = dict(scores)
    enrichments = dict(enrichments)
    for (_, name), members in groups.items():
        if len(members) < 2:
            continue
        members.sort(key=lambda t: (-scores[t], t))
        described = [t for t in members if t in enrichments]
        key = f"{name} ({len(members)} films)"
        scores[key] = scores[members[0]]
        if described:
            enrichments[key] = f"{name}: {', '.join(members)}. {enrichments[described[0]]}"
        for title in members:
            del scores[title]
    return scores, enrichments
