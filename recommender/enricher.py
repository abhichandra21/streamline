import re
from pathlib import Path

import anthropic

from .tmdb_client import TmdbMetadata


def _cache_path(metadata: TmdbMetadata, cache_dir: str) -> Path:
    if metadata.tmdb_id:
        return Path(cache_dir) / metadata.content_type / f"{metadata.tmdb_id}.txt"
    slug = re.sub(r'[^a-z0-9]+', '-', metadata.title.lower()).strip('-')
    return Path(cache_dir) / 'unknown' / f"{slug}.txt"


def _fallback_description(metadata: TmdbMetadata) -> str:
    parts = metadata.genres + metadata.keywords
    if metadata.original_language:
        parts.append(metadata.original_language)
    if metadata.creator_or_director:
        parts.append(metadata.creator_or_director)
    return ' '.join(parts)


def enrich(metadata: TmdbMetadata, cache_dir: str, client: anthropic.Anthropic) -> str:
    """Return a semantic description for a title, using cache if available."""
    path = _cache_path(metadata, cache_dir)
    if path.exists():
        return path.read_text()

    tmdb_info = (
        f"Title: {metadata.title}\n"
        f"Type: {metadata.content_type}\n"
        f"Genres: {', '.join(metadata.genres)}\n"
        f"Keywords: {', '.join(metadata.keywords)}\n"
        f"Cast: {', '.join(metadata.cast)}\n"
        f"Language: {metadata.original_language}"
    )
    if metadata.creator_or_director:
        tmdb_info += f"\nDirector/Creator: {metadata.creator_or_director}"

    try:
        message = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=200,
            timeout=30.0,
            messages=[{
                "role": "user",
                "content": (
                    "Write a 2-3 sentence description of this title capturing its mood, "
                    "pacing, tone, cultural flavor, and themes. Be specific, not generic.\n\n"
                    + tmdb_info
                ),
            }],
        )
        description = message.content[0].text.strip()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(description)
    except Exception:
        description = _fallback_description(metadata)

    return description


def enrich_batch(
    titles_metadata: dict[str, TmdbMetadata],
    cache_dir: str,
    client: anthropic.Anthropic,
) -> dict[str, str]:
    """Enrich a batch of titles. Falls back gracefully on individual failures."""
    result = {}
    for i, (title, metadata) in enumerate(titles_metadata.items()):
        result[title] = enrich(metadata, cache_dir, client)
        if (i + 1) % 50 == 0:
            print(f"  {i+1}/{len(titles_metadata)} enriched...")
    return result
