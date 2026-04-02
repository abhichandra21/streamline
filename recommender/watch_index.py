import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path

from .ingestion.base import WatchEvent
from .tmdb_client import TmdbMetadata

log = logging.getLogger("recommender.index")


def _normalize(title: str) -> str:
    """Lowercase, strip parenthetical suffixes and edition markers."""
    title = title.lower()
    title = re.sub(r'\s*\([^)]*\)', '', title)
    return title.strip()


@dataclass
class WatchIndex:
    tmdb_ids: set[int]
    normalized_titles: set[tuple[str, str]]
    entries: list[dict]

    def is_watched(self, candidate: TmdbMetadata) -> bool:
        """Check by TMDB ID first; fall back to content-type-aware normalized title match."""
        if candidate.tmdb_id and candidate.tmdb_id in self.tmdb_ids:
            return True
        return (_normalize(candidate.title), candidate.content_type) in self.normalized_titles


def build(events: list[WatchEvent], metadata: dict[str, TmdbMetadata]) -> WatchIndex:
    """Build exclusion index from watch events. metadata provides TMDB IDs."""
    tmdb_ids: set[int] = set()
    normalized_titles: set[tuple[str, str]] = set()
    entries: list[dict] = []
    seen_keys: set[str] = set()

    for e in events:
        key = e.series_name if e.content_type == 'tv' else e.title
        normalized_titles.add((_normalize(e.title), e.content_type))
        if e.content_type == 'tv':
            normalized_titles.add((_normalize(e.series_name), e.content_type))
        if key not in seen_keys:
            seen_keys.add(key)
            meta = metadata.get(key)
            tmdb_id = meta.tmdb_id if meta else 0
            if tmdb_id:
                tmdb_ids.add(tmdb_id)
            entries.append({
                "tmdb_id": tmdb_id,
                "title": key,
                "content_type": e.content_type,
            })

    index = WatchIndex(tmdb_ids=tmdb_ids, normalized_titles=normalized_titles, entries=entries)
    return deduplicate(index)


def deduplicate(index: WatchIndex) -> WatchIndex:
    """Merge near-duplicate entries using TMDB ID and fuzzy title matching.

    1. Entries with the same TMDB ID are merged (keep first occurrence).
    2. Entries with no TMDB ID are fuzzy-matched against each other (threshold 90).
    """
    from rapidfuzz import fuzz

    # Phase 1: merge by TMDB ID
    seen_ids: dict[int, int] = {}  # tmdb_id -> index in deduped
    deduped: list[dict] = []
    for e in index.entries:
        tmdb_id = e.get("tmdb_id", 0)
        if tmdb_id and tmdb_id in seen_ids:
            log.debug("Dedup by TMDB ID: %r merged with %r (ID %d)",
                       e["title"], deduped[seen_ids[tmdb_id]]["title"], tmdb_id)
            continue
        if tmdb_id:
            seen_ids[tmdb_id] = len(deduped)
        deduped.append(e)

    # Phase 2: fuzzy dedup for entries without TMDB ID
    no_id = [e for e in deduped if not e.get("tmdb_id")]
    has_id = [e for e in deduped if e.get("tmdb_id")]

    kept_no_id: list[dict] = []
    for e in no_id:
        is_dup = False
        for existing in kept_no_id:
            if (e.get("content_type") == existing.get("content_type")
                    and fuzz.ratio(e["title"].lower(), existing["title"].lower()) >= 90):
                log.debug("Dedup by fuzzy match: %r ~ %r (score >= 90)",
                           e["title"], existing["title"])
                is_dup = True
                break
        if not is_dup:
            kept_no_id.append(e)

    final = has_id + kept_no_id
    before = len(index.entries)
    after = len(final)
    if before != after:
        log.info("Deduplication: %d -> %d entries (%d removed)", before, after, before - after)

    # Rebuild sets from deduped entries
    tmdb_ids = {e["tmdb_id"] for e in final if e.get("tmdb_id")}
    normalized_titles = set()
    for e in final:
        normalized_titles.add((_normalize(e["title"]), e.get("content_type", "movie")))

    return WatchIndex(tmdb_ids=tmdb_ids, normalized_titles=normalized_titles, entries=final)


def save(index: WatchIndex, path: str) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(index.entries))


def load(path: str) -> WatchIndex:
    entries = json.loads(Path(path).read_text())
    tmdb_ids = {e["tmdb_id"] for e in entries if e.get("tmdb_id")}
    normalized_titles = {(_normalize(e["title"]), e.get("content_type", "movie")) for e in entries}
    return WatchIndex(tmdb_ids=tmdb_ids, normalized_titles=normalized_titles, entries=entries)
