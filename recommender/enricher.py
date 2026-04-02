import logging
import re
import time
from pathlib import Path

from .llm import LLMClient
from .tmdb_client import TmdbMetadata

log = logging.getLogger("recommender.enricher")

RATE_LIMIT_WAIT = 30  # seconds to wait on rate limit
MAX_RETRIES = 3


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


def enrich(metadata: TmdbMetadata, cache_dir: str, client: LLMClient) -> str:
    """Return a semantic description for a title, using cache if available."""
    path = _cache_path(metadata, cache_dir)
    if path.exists():
        log.debug("Enrichment cache hit: %s", metadata.title)
        return path.read_text()
    log.debug("Enriching: %s (ID %d)", metadata.title, metadata.tmdb_id)

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

    prompt = (
        "Write a 2-3 sentence description of this title capturing its mood, "
        "pacing, tone, cultural flavor, and themes. Be specific, not generic.\n\n"
        + tmdb_info
    )

    for attempt in range(MAX_RETRIES):
        try:
            description = client.generate(prompt, role="fast", max_tokens=200).strip()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(description)
            break
        except Exception as exc:
            if ("429" in str(exc) or "rate" in str(exc).lower()
                    or "RESOURCE_EXHAUSTED" in str(exc)):
                log.debug("Rate limited during enrichment, waiting %ds...", RATE_LIMIT_WAIT)
                time.sleep(RATE_LIMIT_WAIT)
                continue
            log.warning("Enrichment failed for %s, using fallback: %s", metadata.title, exc)
            description = _fallback_description(metadata)
            break
    else:
        log.warning("Enrichment failed for %s after %d retries, using fallback", metadata.title, MAX_RETRIES)
        description = _fallback_description(metadata)

    return description


def enrich_batch(
    titles_metadata: dict[str, TmdbMetadata],
    cache_dir: str,
    client: LLMClient,
) -> dict[str, str]:
    """Enrich a batch of titles. Falls back gracefully on individual failures."""
    # Throttle API calls for providers with tight RPM limits
    is_gemini = hasattr(client, 'provider') and client.provider == 'gemini'
    throttle = 1.2 if is_gemini else 0.0  # ~50 RPM for Gemini

    result = {}
    api_calls = 0
    for i, (title, metadata) in enumerate(titles_metadata.items()):
        cache_path = _cache_path(metadata, cache_dir)
        needs_api = not cache_path.exists()
        result[title] = enrich(metadata, cache_dir, client)
        if needs_api and throttle:
            api_calls += 1
            time.sleep(throttle)
        if (i + 1) % 50 == 0:
            print(f"  {i+1}/{len(titles_metadata)} enriched...")
    if api_calls and throttle:
        log.debug("Enrichment: %d API calls with %.1fs throttle", api_calls, throttle)
    return result
