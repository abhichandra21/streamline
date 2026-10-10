import hashlib
import json
import logging
import re
import time
from pathlib import Path

import config
from .ingestion.base import SYNTHETIC_TIMESTAMP_PLATFORMS, WatchEvent
from .llm import LLMClient

log = logging.getLogger("recommender.profile")

_BATCH_DIR = Path(config.ENRICHMENT_CACHE_DIR).parent / "profile_batches"
_MAX_PROFILE_CLUSTERS = 15
_LIST_ITEM_RE = re.compile(r"^\s*(?:[-*]\s+|\d+[\.)]\s*)(.+?)\s*$")
_MARKDOWN_HEADING_NUMBER_RE = re.compile(r"^(##\s+)(?:\d+[\.)]\s*)?(.+?)\s*$")
_FAMILY_CLUSTER_PATTERNS = (
    r"\bdisney\b",
    r"\bpixar\b",
    r"\bchristmas\b",
    r"\bholiday\b",
    r"\bseasonal\b",
    r"\bkids?\b",
    r"\bchildren(?:'s)?\b",
    r"\bchild[- ]friendly\b",
    r"\bfamily\s+(?:animation|animated|television|tv|sitcom|comfort|viewing|content|"
    r"movie|movies|film|films|musical|musicals|adventure|adventures|channel)\b",
    r"\b(?:animation|animated|television|tv|sitcom|comfort|viewing|content|movie|movies|"
    r"film|films|musical|musicals|adventure|adventures|channel)\s+(?:and\s+)?family\b",
)


_SKIP_HEADING = "What you tend to skip"

# Cluster order should follow how much of the history each cluster covers, so
# every prompt that describes clusters asks for an approximate title count.
_CLUSTER_COUNT_NOTE = (
    "Cluster by genre, tone, and style; when a country or language runs through many titles "
    "across genres (for example British or Hindi series), give it its own cluster. Assign each title to exactly one cluster and give the "
    "count, like \"(~34 titles)\", so counts add up to the batch. Count titles marked "
    "\"More like this\" extra. List clusters largest first.\n"
)

_PROMPT_VERSION = "face-v1"

_EQUAL_WEIGHT_NOTE = (
    "Each title's score says how sure we are it reflects this household's taste: 2 for titles "
    "they rated \"More like this\" or follow, 1 for real viewing, 0.3 for titles known only from "
    "a downloads list or a \"Seen it\" tap. A film franchise appears once, as "
    "\"<collection> (n films)\".\n"
)


def history_label() -> str:
    return "engagement score" if config.USE_VIEWING_SIGNALS else "weight, newest first among ties"


def sort_scored(
    events: list[WatchEvent],
    scores: dict[str, float],
    enrichments: dict[str, str],
) -> list[tuple[str, float]]:
    """Highest score first; ties go to the most recently watched title, then title."""
    latest: dict[str, object] = {}
    for e in events:
        if e.platform in SYNTHETIC_TIMESTAMP_PLATFORMS:
            continue  # import-time date, not a watch time
        key = e.series_name if e.content_type == "tv" else e.title
        if key not in latest or e.timestamp > latest[key]:
            latest[key] = e.timestamp
    items = [(title, score) for title, score in scores.items() if title in enrichments]
    # Titles with no event sort after those with one; sort twice for stable mixed-direction keys.
    items.sort(key=lambda x: x[0])
    items.sort(key=lambda x: latest[x[0]].timestamp() if x[0] in latest else float("-inf"), reverse=True)
    items.sort(key=lambda x: -x[1])
    return items


def _batch_fingerprint(scored: list[tuple[str, float]]) -> str:
    """Hash the scored title list to detect when batches are stale.

    Hashes titles in their scored order with rounded scores. Rounding to
    2 decimal places absorbs minor recency drift between runs while still
    invalidating on meaningful changes (feedback, weight changes, new titles).
    """
    rounded = [(t, round(s, 2)) for t, s in scored]
    # Prompt wording differs by mode, so batches from the other mode must not be reused.
    # Bump the version when prompt wording changes, so older cached batches are not reused.
    content = json.dumps([_PROMPT_VERSION, config.USE_VIEWING_SIGNALS, rounded])
    return hashlib.sha256(content.encode()).hexdigest()[:16]


def _load_cached_batches(fingerprint: str, total: int) -> list[str | None]:
    """Load cached batch profiles. Returns a list with None for missing batches."""
    results: list[str | None] = []
    fp_file = _BATCH_DIR / "fingerprint.txt"

    if not _BATCH_DIR.exists() or not fp_file.exists():
        return [None] * total

    cached_fp = fp_file.read_text().strip()
    if cached_fp != fingerprint:
        log.debug("Batch fingerprint changed (%s -> %s), invalidating cache", cached_fp, fingerprint)
        _clear_batch_cache()
        return [None] * total

    for i in range(total):
        path = _BATCH_DIR / f"batch_{i+1:02d}.txt"
        if path.exists():
            results.append(path.read_text())
        else:
            results.append(None)

    cached_count = sum(1 for r in results if r is not None)
    if cached_count:
        log.debug("Found %d/%d cached batch profiles", cached_count, total)
    return results


def _save_batch(index: int, text: str, fingerprint: str) -> None:
    """Save a single batch profile to disk."""
    _BATCH_DIR.mkdir(parents=True, exist_ok=True)
    (_BATCH_DIR / "fingerprint.txt").write_text(fingerprint)
    (_BATCH_DIR / f"batch_{index+1:02d}.txt").write_text(text)


def _clear_batch_cache() -> None:
    """Remove all cached batch files."""
    if _BATCH_DIR.exists():
        for f in _BATCH_DIR.iterdir():
            f.unlink()
        log.debug("Cleared batch cache")


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
        f"{_CLUSTER_COUNT_NOTE}"
        "Write in second person (\"You gravitate toward...\").\n\n"
        f"{'' if config.USE_VIEWING_SIGNALS else _EQUAL_WEIGHT_NOTE + chr(10)}"
        f"Watch history (sorted by {history_label()}):\n{history_str}"
    )
    return client.generate(prompt, role="reason", max_tokens=config.TOKENS_PROFILE_BATCH,
                            timeout=config.TIMEOUT_PROFILE_BATCH).strip()


def _is_family_cluster_text(text: str) -> bool:
    normalized = text.strip().splitlines()[0].casefold() if text.strip() else ""
    return any(re.search(pattern, normalized) for pattern in _FAMILY_CLUSTER_PATTERNS)


def _personal_clusters_first(items: list[str]) -> list[str]:
    personal: list[str] = []
    family: list[str] = []
    for item in items:
        if _is_family_cluster_text(item):
            family.append(item)
        else:
            personal.append(item)
    return personal + family


def _cap_cluster_list(text: str, max_clusters: int = _MAX_PROFILE_CLUSTERS) -> str:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    list_items = []
    for line in lines:
        match = _LIST_ITEM_RE.match(line)
        if match:
            list_items.append(match.group(1).strip())

    if list_items:
        ordered = _personal_clusters_first(list_items)
        capped = ordered[:max_clusters]
        if len(list_items) > max_clusters:
            log.info("Capped extracted clusters to %d (was %d list items)", max_clusters, len(list_items))
        return "\n".join(f"{index}. {item}" for index, item in enumerate(capped, start=1))

    if len(lines) > max_clusters:
        log.info("Capped extracted clusters to %d lines (was %d lines)", max_clusters, len(lines))
    return "\n".join(lines[:max_clusters])


def _split_markdown_sections(text: str) -> tuple[str, list[str]]:
    preamble: list[str] = []
    sections: list[str] = []
    current_section: list[str] | None = None

    for line in text.splitlines():
        if line.startswith("## "):
            if current_section is not None:
                sections.append("\n".join(current_section).strip())
            current_section = [line]
        elif current_section is None:
            preamble.append(line)
        else:
            current_section.append(line)

    if current_section is not None:
        sections.append("\n".join(current_section).strip())

    return "\n".join(preamble).strip(), sections


def _renumber_markdown_sections(sections: list[str]) -> list[str]:
    renumbered: list[str] = []
    for index, section in enumerate(sections, start=1):
        lines = section.splitlines()
        if not lines:
            continue
        match = _MARKDOWN_HEADING_NUMBER_RE.match(lines[0])
        if match:
            lines[0] = f"{match.group(1)}{index}. {match.group(2)}"
        renumbered.append("\n".join(lines))
    return renumbered


def _cap_markdown_sections(text: str, max_sections: int = _MAX_PROFILE_CLUSTERS) -> str:
    """Keep at most max_sections markdown h2 sections, preserving any preamble."""
    preamble, sections = _split_markdown_sections(text)
    if not sections:
        return text.strip()

    ordered_sections = _personal_clusters_first(sections)
    kept_sections = _renumber_markdown_sections(ordered_sections[:max_sections])
    if len(sections) > max_sections:
        log.info("Capped final profile to %d sections (was %d)", max_sections, len(sections))

    parts = []
    if preamble:
        parts.append(preamble)
    parts.extend(kept_sections)
    return "\n".join(parts).strip()


def _skip_section_note(negative_prefs: list[str] | None) -> str:
    """Prompt text asking for a skip section from the Less titles."""
    if not negative_prefs:
        return ""
    titles_str = ", ".join(f'"{t}"' for t in negative_prefs)
    # The signal is "I would not seek out more like this", not "I hated
    # it" -- these are titles the user watched to the end. Overstating
    # it to the model produces a harsher anti-pattern than the evidence
    # supports.
    return (
        f"\n\nThe user has asked to see less like these titles: {titles_str}. "
        "They watched them; this is a preference against more of the same, "
        f"not a verdict that they were bad. Add a brief '{_SKIP_HEADING}' section "
        "capturing the patterns they point to."
    )


def _split_skip_section(text: str) -> tuple[str, str]:
    """Separate the skip section so the cluster cap neither counts nor drops it."""
    preamble, sections = _split_markdown_sections(text)
    skip = [s for s in sections if _SKIP_HEADING.casefold() in s.splitlines()[0].casefold()]
    if not skip:
        return text, ""
    rest = [s for s in sections if s not in skip]
    return "\n".join(([preamble] if preamble else []) + rest).strip(), skip[0]


def _merge_profiles(
    batch_profiles: list[str],
    client: LLMClient,
    negative_section: str = "",
) -> str:
    """Merge multiple batch profiles into one consolidated taste profile.

    Uses a two-step approach:
    1. Extract cluster labels from all batches (short, structured)
    2. Write the full profile with strict length constraints
    """
    profiles_str = '\n\n---\n\n'.join(
        f"BATCH {i+1}:\n{p}" for i, p in enumerate(batch_profiles)
    )

    # Step 1: Extract and consolidate cluster labels
    cluster_prompt = (
        "Below are taste profile analyses from different segments of the same person's "
        "watch history. List ONLY the distinct taste cluster names you see, merging "
        "overlapping clusters. Return a numbered list of 8-15 cluster names, nothing else.\n"
        "Each batch gives approximate title counts for its clusters. For merged clusters, sum "
        "those counts across batches, and order the clusters by total share, largest first. "
        "Titles marked \"More like this\" count extra. Put family/kids/co-viewing clusters last. "
        "Put the total after each name, like \"Franchise action and superhero blockbusters (~120 titles)\". "
        "Do not merge clusters just because they share a language.\n\n"
        f"{profiles_str}"
    )
    clusters_text = client.generate(cluster_prompt, role="reason",
                                     max_tokens=500, timeout=config.TIMEOUT_PROFILE_MERGE).strip()
    log.debug("Extracted clusters: %s", clusters_text[:500])

    # Enforce maximum clusters and keep family/co-viewing patterns from occupying the top slots.
    clusters_text = _cap_cluster_list(clusters_text)

    # Step 2: Write the full profile constrained to those clusters
    prompt = (
        "Below are taste profile analyses from different segments of the same person's "
        "watch history, followed by a consolidated list of taste clusters.\n\n"
        f"BATCH ANALYSES:\n{profiles_str}\n\n"
        f"CONSOLIDATED CLUSTERS:\n{clusters_text}\n\n"
        "Write the final merged taste profile. Rules:\n"
        "- Write one section (## heading) per cluster from the list above, in the order given "
        "(it is ordered by share of the history, largest first). Leave the counts out of the headings\n"
        "- Do not add extra ## sections beyond the consolidated cluster list; merge leftover "
        "patterns into the nearest retained cluster\n"
        "- Each section: a rich paragraph describing the pattern (tone, pacing, themes, "
        "what draws this person), followed by key titles in bold/italic\n"
        "- Merge overlapping content from different batches into the same cluster\n"
        "- Clusters that appear across multiple batches are stronger — note this. Do not call a "
        "small cluster the deepest or most consistent; its size is in the counts. Describe "
        "mainstream and franchise clusters as genuine taste, not a guilty pleasure, and avoid "
        "\"arthouse\" unless the titles are\n"
        "- Write in second person (\"You gravitate toward...\")\n"
        "- Be specific about titles and what connects them, not generic\n"
        "- IMPORTANT: You MUST complete every section. If running low on space, "
        "make later sections shorter rather than cutting off mid-sentence.\n"
        + (f"- After the cluster sections, write one more section, '## {_SKIP_HEADING}', "
           "without a number. It is the one exception to the no-extra-sections rule."
           f"{negative_section}\n" if negative_section else "")
    )
    profile_text = client.generate(prompt, role="reason", max_tokens=config.TOKENS_PROFILE_MERGE,
                                   timeout=config.TIMEOUT_PROFILE_MERGE).strip()
    clusters_part, skip_part = _split_skip_section(profile_text)
    capped = _cap_markdown_sections(clusters_part)
    return f"{capped}\n\n{skip_part}" if skip_part else capped


def build(
    events: list[WatchEvent],
    scores: dict[str, float],
    enrichments: dict[str, str],
    client: LLMClient,
    negative_prefs: list[str] | None = None,
    on_batch_progress: "callable | None" = None,
) -> str:
    """Build a natural-language taste profile.

    Processes all enriched titles in batches. Batch results are cached to disk
    so that a failed merge can be retried without re-generating batches.
    Cached batches are invalidated when the scored title list changes.

    on_batch_progress, if provided, is called as on_batch_progress(done, total)
    after each batch completes (cached or freshly generated) so callers can
    drive a progress bar.
    """
    scored = sort_scored(events, scores, enrichments)

    if not scored:
        return "No watch history available for taste profiling."

    negative_section = _skip_section_note(negative_prefs)

    # Single batch — no need for merge or caching
    if len(scored) <= config.PROFILE_BATCH_SIZE:
        lines = [
            f"- {title} (score: {score:.2f}): {enrichments[title]}"
            for title, score in scored
        ]
        history_str = '\n'.join(lines)

        prompt = (
            "Analyze this person's streaming watch history and write a detailed taste profile.\n"
            "Identify distinct taste clusters, preferences for tone/pacing/culture, "
            + ("what they consistently finish, and notable patterns.\n" if config.USE_VIEWING_SIGNALS
               else "what they keep coming back to, and notable patterns.\n")
            + f"{_CLUSTER_COUNT_NOTE}"
            "Write in second person (\"You gravitate toward...\")."
            f"{negative_section}\n\n"
            f"{'' if config.USE_VIEWING_SIGNALS else _EQUAL_WEIGHT_NOTE + chr(10)}"
            f"Watch history (sorted by {history_label()}):\n{history_str}"
        )
        return client.generate(prompt, role="reason", max_tokens=config.TOKENS_PROFILE_MERGE,
                                timeout=config.TIMEOUT_PROFILE_MERGE).strip()

    # Multiple batches — use cache for resumability
    batches = [scored[i:i + config.PROFILE_BATCH_SIZE] for i in range(0, len(scored), config.PROFILE_BATCH_SIZE)]
    total = len(batches)
    fingerprint = _batch_fingerprint(scored)

    # Check for cached batches from a previous interrupted run
    cached = _load_cached_batches(fingerprint, total)
    cached_count = sum(1 for c in cached if c is not None)
    if cached_count == total:
        log.info("All %d batch profiles cached, skipping to merge...", total)
    elif cached_count > 0:
        log.info("Resuming: %d/%d batch profiles cached, generating remaining...", cached_count, total)
    else:
        log.info("Building taste profile in %d batches (%d titles)...", total, len(scored))

    batch_profiles = []
    try:
        for i, batch in enumerate(batches):
            if cached[i] is not None:
                batch_profiles.append(cached[i])
                if on_batch_progress is not None:
                    on_batch_progress(i + 1, total)
                continue

            log.info("Batch %d/%d (%d titles)...", i + 1, total, len(batch))
            for attempt in range(3):
                try:
                    profile = _build_batch_profile(batch, enrichments, i + 1, total, client)
                    _save_batch(i, profile, fingerprint)
                    batch_profiles.append(profile)
                    if on_batch_progress is not None:
                        on_batch_progress(i + 1, total)
                    break
                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    err = str(exc).lower()
                    is_retryable = ("rate" in err or "429" in err
                                    or "timeout" in err or "timed out" in err)
                    if is_retryable and attempt < 2:
                        wait = config.RATE_LIMIT_WAIT if "rate" in err or "429" in err else 10
                        reason = "Rate limited" if "429" in err or "rate" in err else "Timed out"
                        log.info("%s, waiting %ds (attempt %d/3)...", reason, wait, attempt + 1)
                        time.sleep(wait)
                    else:
                        log.warning("Batch %d failed (attempt %d): %s", i + 1, attempt + 1, exc)
                        if attempt == 2:
                            log.warning("Batch %d failed after 3 retries, skipping.", i + 1)
    except KeyboardInterrupt:
        on_disk = sum(1 for f in _BATCH_DIR.glob("batch_*.txt")) if _BATCH_DIR.exists() else 0
        log.info("Interrupted. %d/%d batches saved to cache.", on_disk, total)
        if on_disk:
            log.info("Re-run --refresh-profile to resume from batch %d.", on_disk + 1)
        raise

    log.info("Merging %d batch profiles...", len(batch_profiles))
    result = _merge_profiles(batch_profiles, client, negative_section)

    if client.was_truncated:
        log.error(
            "Merge output was truncated (%d chars). Batch cache preserved — "
            "re-run --refresh-profile to retry merge only. "
            "To fix: increase llm.tokens_profile_merge in config.yaml (currently %d).",
            len(result), config.TOKENS_PROFILE_MERGE,
        )
        log.error("Profile merge truncated at %d chars. Batches preserved.", len(result))
        raise RuntimeError(f"Profile merge truncated. Increase llm.tokens_profile_merge (currently {config.TOKENS_PROFILE_MERGE}).")

    _clear_batch_cache()
    return result
