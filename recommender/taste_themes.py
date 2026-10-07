"""The household's themes: which taste tags add up to which taste.

One reasoning call decides them and they are saved. They are re-thought only
on request, so rows don't move between builds. Tags that show up later are
filed into existing themes by a small fast call.
"""
from __future__ import annotations

import json
import logging
import re
from collections import defaultdict
from pathlib import Path

import config
from .taste_tags import _strip_fence, _write_json_atomic, vocabulary

log = logging.getLogger("recommender.taste_themes")

MAX_THEMES = 16
MAX_THEME_TAGS = 30
EXAMPLES_PER_TAG = 3


def load_themes(path: str | Path) -> list[dict]:
    source = Path(path)
    if not source.exists():
        return []
    try:
        return _clean(json.loads(source.read_text()).get("themes"))
    except (OSError, json.JSONDecodeError, AttributeError) as exc:
        log.warning("Ignoring unreadable theme map at %s: %s", source, exc)
        return []


def load_ignored(path: str | Path) -> set[str]:
    """Tags judged facts or too vague. Kept so they are never asked about again."""
    try:
        return set(json.loads(Path(path).read_text()).get("ignored") or [])
    except (OSError, json.JSONDecodeError, AttributeError):
        return set()


def save_themes(themes: list[dict], path: str | Path, ignored: set[str] | None = None) -> None:
    keep = load_ignored(path) if ignored is None else ignored
    _write_json_atomic({"themes": themes, "ignored": sorted(keep)}, path)


def parse_themes(text: str) -> list[dict]:
    try:
        data = json.loads(_strip_fence(text))
    except json.JSONDecodeError as exc:
        raise ValueError(f"theme answer was not JSON: {exc}") from exc
    return _clean(data.get("themes") if isinstance(data, dict) else None)


def _clean(raw_themes) -> list[dict]:
    # A tag over one theme's cap may still land in a later theme; the first theme wins.
    themes, claimed, ids = [], set(), set()
    for raw in raw_themes if isinstance(raw_themes, list) else []:
        if not isinstance(raw, dict) or not str(raw.get("label") or "").strip():
            continue
        label = str(raw["label"]).strip()
        tags = []
        for tag in raw.get("tags") or []:
            tag = str(tag).strip().lower()
            if tag and tag not in claimed and len(tags) < MAX_THEME_TAGS:
                claimed.add(tag)
                tags.append(tag)
        theme_id = str(raw.get("id") or re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-"))
        # Ids key the saved words; two themes must never share one.
        base, n = theme_id, 2
        while theme_id in ids:
            theme_id, n = f"{base}-{n}", n + 1
        ids.add(theme_id)
        themes.append({
            "id": theme_id,
            "label": label,
            "region": str(raw.get("region") or "").strip().upper(),
            "language": str(raw.get("language") or "").strip().lower(),
            "family": bool(raw.get("family")),
            "tags": tags,
        })
        if len(themes) == MAX_THEMES:
            break
    return themes


def tag_to_theme(themes: list[dict]) -> dict[str, int]:
    return {tag: i for i, theme in enumerate(themes) for tag in theme["tags"]}


def theme_prompt(tags: dict[str, list[str]], previous: list[dict]) -> str:
    titles_by_tag = defaultdict(list)
    for title in sorted(tags, key=str.casefold):
        for tag in tags[title]:
            titles_by_tag[tag].append(title)
    counts = vocabulary(tags)
    lines = [f"{tag} ({counts[tag]}): {'; '.join(titles_by_tag[tag][:EXAMPLES_PER_TAG])}"
             for tag in sorted(counts, key=lambda t: (-counts[t], t))]
    keep = (
        "These themes exist from last time. Keep a theme's id when its taste still exists:\n"
        + json.dumps(previous, indent=1) + "\n\n"
        if previous else ""
    )
    return (
        "Below is every taste tag on the titles a household loved, as "
        "\"tag (number of loved titles): example titles\". Decide what this household's tastes are, "
        f"as 10 to {MAX_THEMES} themes.\n"
        "Each theme is one taste a person would recognise in themselves, like \"cosy British mysteries\" "
        "or \"true-story underdogs\". Never a list of tastes joined by \"and\", and never a fact about a "
        "title (\"based on book\", \"miniseries\", \"female lead\", \"set in new york\").\n"
        "When a country or language runs through many loved titles across genres, make it its own theme "
        "and set region (ISO 3166 code, like GB) or language (ISO 639-1 code, like hi). Set family true "
        "only for a theme of children's titles.\n"
        f"Map tags to themes: each theme lists the tags that are evidence for it, at most {MAX_THEME_TAGS} "
        "tags per theme, each tag in one theme only. If a theme needs more tags, split it. Map spelling "
        "variants and synonyms to the same theme.\n"
        "Moods and relationships are tastes: \"feel-good\", \"bittersweet\", \"family drama\", \"romantic "
        "drama\", \"coming of age\" belong in themes, and a household that carries many of them has a "
        "theme for them. Leave out only facts about a title: its source (\"based on novel\", \"remake\"), "
        "its format (\"miniseries\", \"ensemble cast\"), its cast (\"female lead\"), a city or decade.\n"
        "List themes in priority order: country and language themes first, family last.\n\n"
        + keep +
        "Return ONLY JSON: {\"themes\": [{\"id\": \"short-slug\", \"label\": \"plain descriptive label\", "
        "\"region\": \"\", \"language\": \"\", \"family\": false, \"tags\": [\"...\"]}]}\n\n"
        + "\n".join(lines)
    )


def build_themes(tags: dict[str, list[str]], client, path: str | Path, previous: list[dict]) -> list[dict]:
    text = client.generate(theme_prompt(tags, previous), role="reason",
                           max_tokens=config.TOKENS_PROFILE_MERGE, timeout=config.TIMEOUT_PROFILE_MERGE)
    themes = parse_themes(text)
    if not themes:
        raise ValueError("the model returned no themes")
    mapped = tag_to_theme(themes)
    # Whatever the theme step left out it judged a fact or too vague; never ask again.
    save_themes(themes, path, {t for ts in tags.values() for t in ts if t not in mapped})
    return themes


def map_new_tags(themes: list[dict], unmapped: list[str], client, path: str | Path) -> list[dict]:
    """File tags that appeared after the themes were built. Never re-decides existing themes."""
    if not unmapped or not themes:
        return themes
    prompt = (
        "A household's taste themes:\n"
        + "\n".join(f"{t['id']}: {t['label']} (tags like {', '.join(t['tags'][:6])})" for t in themes)
        + "\n\nFor each new tag below, answer the id of the theme it is evidence for, or \"none\" if it "
        "fits none or is a fact rather than a taste. Return ONLY JSON mapping tag to id.\n\n"
        + "\n".join(sorted(unmapped))
    )
    try:
        answer = json.loads(_strip_fence(client.generate(prompt, role="fast", max_tokens=4000,
                                                         timeout=config.TIMEOUT_PROFILE_MERGE)))
    except Exception as exc:
        log.warning("Could not file new taste tags: %s", exc)
        return themes
    taken = set(tag_to_theme(themes))
    updated = [dict(t, tags=list(t["tags"])) for t in themes]
    by_id = {t["id"]: t for t in updated}
    ignored = load_ignored(path)
    if not isinstance(answer, dict):
        return themes
    for tag in sorted(unmapped):
        if tag not in answer:
            continue  # skipped, not judged: ask again next build
        target = by_id.get(str(answer[tag]))
        if target and tag not in taken and len(target["tags"]) < MAX_THEME_TAGS:
            target["tags"].append(tag)
            taken.add(tag)
        else:
            ignored.add(tag)
    save_themes(updated, path, ignored)
    return updated
