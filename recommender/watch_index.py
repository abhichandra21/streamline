import json
import re
from dataclasses import dataclass
from pathlib import Path

from .ingestion.base import WatchEvent
from .tmdb_client import TmdbMetadata


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

    return WatchIndex(tmdb_ids=tmdb_ids, normalized_titles=normalized_titles, entries=entries)


def save(index: WatchIndex, path: str) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(index.entries))


def load(path: str) -> WatchIndex:
    entries = json.loads(Path(path).read_text())
    tmdb_ids = {e["tmdb_id"] for e in entries if e.get("tmdb_id")}
    normalized_titles = {(_normalize(e["title"]), e.get("content_type", "movie")) for e in entries}
    return WatchIndex(tmdb_ids=tmdb_ids, normalized_titles=normalized_titles, entries=entries)
