import logging
import time

from .ingestion.base import WatchEvent
from .llm import LLMClient

log = logging.getLogger("recommender.profile")

BATCH_SIZE = 200
RATE_LIMIT_WAIT = 65  # seconds to wait on rate limit


def _build_batch_profile(
    titles: list[tuple[str, float]],
    enrichments: dict[str, str],
    batch_num: int,
    total_batches: int,
    client: LLMClient,
) -> str:
    """Build a mini taste profile from a batch of titles."""
    lines = [
        f"- {title} (score: {score:.2f}): {enrichments[title]}"
        for title, score in titles
    ]
    history_str = '\n'.join(lines)

    prompt = (
        f"Analyze this batch ({batch_num}/{total_batches}) of a person's streaming watch history.\n"
        "Identify distinct taste clusters, preferences for tone/pacing/culture, "
        "and notable patterns. Be thorough — capture every distinct genre or style cluster "
        "you see, even small ones.\n"
        "Write in second person (\"You gravitate toward...\").\n\n"
        f"Watch history (sorted by engagement score):\n{history_str}"
    )
    return client.generate(prompt, role="reason", max_tokens=800, timeout=60.0).strip()


def _merge_profiles(
    batch_profiles: list[str],
    client: LLMClient,
) -> str:
    """Merge multiple batch profiles into one consolidated taste profile."""
    profiles_str = '\n\n---\n\n'.join(
        f"BATCH {i+1}:\n{p}" for i, p in enumerate(batch_profiles)
    )

    prompt = (
        "Below are taste profile analyses from different segments of the same person's "
        "watch history. Merge them into one consolidated taste profile.\n\n"
        "Rules:\n"
        "- Preserve EVERY distinct taste cluster found across all batches\n"
        "- Merge overlapping clusters (e.g., if batch 1 says 'British dramas' and batch 3 "
        "says 'British crime procedurals', combine into one richer cluster)\n"
        "- Keep the relative strength — clusters that appear across multiple batches are stronger\n"
        "- Write in second person (\"You gravitate toward...\")\n"
        "- Be specific about titles, not generic\n\n"
        f"{profiles_str}"
    )
    return client.generate(prompt, role="reason", max_tokens=1500, timeout=60.0).strip()


def build(
    events: list[WatchEvent],
    scores: dict[str, float],
    enrichments: dict[str, str],
    client: LLMClient,
    negative_prefs: list[str] | None = None,
) -> str:
    """Build a natural-language taste profile.

    Processes all enriched titles in batches to avoid context window limits
    and ensure full taste coverage.
    """
    scored = sorted(
        [(title, score) for title, score in scores.items() if title in enrichments],
        key=lambda x: -x[1],
    )

    if not scored:
        return "No watch history available for taste profiling."

    negative_section = ""
    if negative_prefs:
        titles_str = ", ".join(f'"{t}"' for t in negative_prefs)
        negative_section = (
            f"\n\nThe user has explicitly disliked: {titles_str}. "
            "Add a brief 'What you don't enjoy' section to the profile capturing "
            "patterns in what they disliked."
        )

    # Single batch — no need for merge
    if len(scored) <= BATCH_SIZE:
        lines = [
            f"- {title} (score: {score:.2f}): {enrichments[title]}"
            for title, score in scored
        ]
        history_str = '\n'.join(lines)

        prompt = (
            "Analyze this person's streaming watch history and write a detailed taste profile.\n"
            "Identify distinct taste clusters, preferences for tone/pacing/culture, "
            "what they consistently finish, and notable patterns.\n"
            "Write in second person (\"You gravitate toward...\")."
            f"{negative_section}\n\n"
            f"Watch history (sorted by engagement score):\n{history_str}"
        )
        return client.generate(prompt, role="reason", max_tokens=1500, timeout=60.0).strip()

    # Multiple batches — build per-batch profiles then merge
    batches = [scored[i:i + BATCH_SIZE] for i in range(0, len(scored), BATCH_SIZE)]
    total = len(batches)
    print(f"  Building taste profile in {total} batches ({len(scored)} titles)...")

    batch_profiles = []
    for i, batch in enumerate(batches):
        print(f"  Batch {i+1}/{total} ({len(batch)} titles)...")
        for attempt in range(3):
            try:
                profile = _build_batch_profile(batch, enrichments, i + 1, total, client)
                batch_profiles.append(profile)
                break
            except Exception as exc:
                if "rate" in str(exc).lower() or "429" in str(exc):
                    print(f"  Rate limited, waiting {RATE_LIMIT_WAIT}s...")
                    time.sleep(RATE_LIMIT_WAIT)
                else:
                    log.warning("Batch %d failed (attempt %d): %s", i + 1, attempt + 1, exc)
                    if attempt == 2:
                        print(f"  Warning: batch {i+1} failed after 3 retries, skipping.")

    print(f"  Merging {len(batch_profiles)} batch profiles...")
    return _merge_profiles(batch_profiles, client)
