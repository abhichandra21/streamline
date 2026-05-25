import logging
import re
import time
from pathlib import Path

import config
from .llm import LLMClient
from .tmdb_client import TmdbMetadata

log = logging.getLogger("recommender.enricher")

_IDENTITY_KEY_RE = re.compile(r"^(tv|movie)/[0-9]+$")
_UNKNOWN_KEY_RE = re.compile(r"^unknown/[a-z0-9-]+$")


def _slug(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")


def enrichment_key(metadata: TmdbMetadata) -> str:
    return enrichment_key_from_parts(
        metadata.content_type,
        metadata.tmdb_id,
        metadata.title,
    )


def enrichment_key_from_parts(
    content_type: str | None,
    tmdb_id: int | None,
    title: str,
) -> str:
    if tmdb_id and content_type in ("tv", "movie"):
        return f"{content_type}/{tmdb_id}"
    return f"unknown/{_slug(title)}"


def is_identity_enrichment_key(key: str) -> bool:
    return bool(_IDENTITY_KEY_RE.match(key) or _UNKNOWN_KEY_RE.match(key))


def is_identity_enrichment_index(enrichments: dict[str, str]) -> bool:
    return all(is_identity_enrichment_key(k) for k in enrichments)


def _cache_path(metadata: TmdbMetadata, cache_dir: str) -> Path:
    key = enrichment_key(metadata)
    return Path(cache_dir) / f"{key}.txt"


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
    log.debug("Enriching: %s (ID %s)", metadata.title, metadata.tmdb_id)

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

    for attempt in range(3):
        try:
            description = client.generate(prompt, role="fast", max_tokens=config.TOKENS_FAST, timeout=config.TIMEOUT_FAST).strip()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(description)
            break
        except Exception as exc:
            if ("429" in str(exc) or "rate" in str(exc).lower()
                    or "RESOURCE_EXHAUSTED" in str(exc)):
                log.debug("Rate limited during enrichment, waiting %ds...", config.RATE_LIMIT_WAIT)
                time.sleep(config.RATE_LIMIT_WAIT)
                continue
            log.warning("Enrichment failed for %s, using fallback: %s", metadata.title, exc)
            description = _fallback_description(metadata)
            break
    else:
        log.warning("Enrichment failed for %s after %d retries, using fallback", metadata.title, 3)
        description = _fallback_description(metadata)

    return description


def enrich_batch(
    titles_metadata: dict[str, TmdbMetadata],
    cache_dir: str,
    client: LLMClient,
    on_progress: "callable | None" = None,
) -> dict[str, str]:
    """Enrich a batch of titles. Falls back gracefully on individual failures.

    on_progress, if provided, is called as on_progress(done, total, cache_hit)
    after each title so callers can drive a progress bar.
    """
    # Throttle API calls for providers with tight RPM limits
    is_gemini = hasattr(client, 'provider') and client.provider == 'gemini'
    throttle = 1.2 if is_gemini else 0.0  # ~50 RPM for Gemini

    total = len(titles_metadata)
    result = {}
    api_calls = 0
    for i, (title, metadata) in enumerate(titles_metadata.items()):
        cache_path = _cache_path(metadata, cache_dir)
        needs_api = not cache_path.exists()
        result[enrichment_key(metadata)] = enrich(metadata, cache_dir, client)
        if needs_api and throttle:
            api_calls += 1
            time.sleep(throttle)
        if on_progress is not None:
            on_progress(i + 1, total, not needs_api)
        elif (i + 1) % 50 == 0:
            log.info("%d/%d titles enriched...", i + 1, total)
    if api_calls and throttle:
        log.debug("Enrichment: %d API calls with %.1fs throttle", api_calls, throttle)
    return result
