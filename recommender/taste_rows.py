"""Taste rows: place every love in a theme by its tags. Pure code, same answer every time."""
from __future__ import annotations

import json
import logging
import math
from collections import Counter
from pathlib import Path

import config
from .structured_profile import (
    _strip_json_fence, apply_member_weights, explicit_dislikes, validate_structured_profile,
)
from .taste_tags import _write_json_atomic, tag_titles
from .taste_themes import build_themes, load_ignored, load_themes, map_new_tags, tag_to_theme

log = logging.getLogger("recommender.taste_rows")

# Setup warns, and still rebuilds, when the themes stop describing the household.
UNMAPPED_WARN_SHARE = 0.15
BIGGEST_ROW_WARN_SHARE = 1 / 3
# A row keeps its saved name and description while this much of it is unchanged.
WORDS_KEEP_OVERLAP = 0.8
WORDS_MEMBERS_SHOWN = 40


def region_facts(index_entries: list[dict], cache_dir: Path, collections: dict[str, list[str]]) -> dict[str, dict]:
    facts = {}
    for entry in index_entries:
        ct, tmdb_id = entry.get("content_type"), entry.get("tmdb_id")
        if ct not in ("movie", "tv") or not tmdb_id:
            continue
        try:
            meta = json.loads((Path(cache_dir) / ct / f"{tmdb_id}.json").read_text())
        except (OSError, ValueError):
            continue
        if ct == "tv":
            # The channel and origin say where a show was made (BBC, ITV); co-financing doesn't.
            countries = {n.get("origin_country") for n in meta.get("networks") or [] if n.get("origin_country")}
            countries |= set(meta.get("origin_country") or [])
        else:
            countries = set(meta.get("origin_country") or [])
            countries |= {c.get("iso_3166_1") for c in meta.get("production_countries") or [] if c.get("iso_3166_1")}
        facts[entry["title"]] = {"countries": countries, "language": meta.get("original_language") or ""}
    for key, members in collections.items():
        known = [facts[m] for m in members if m in facts]
        if known:
            facts[key] = {"countries": set().union(*(f["countries"] for f in known)), "language": known[0]["language"]}
    return facts


def _is_region(theme: dict) -> bool:
    return bool(theme["region"] or theme["language"])


def _in_region(theme: dict, fact: dict | None) -> bool:
    if not fact:
        return False
    if theme["region"]:
        return theme["region"] in fact["countries"]
    return theme["language"] == fact["language"]


def place_loves(loves, tags, themes, facts):
    mapping = tag_to_theme(themes)
    carriers = Counter(t for title in loves for t in set(tags.get(title, [])))
    placed = {i: [] for i in range(len(themes))}
    unplaced = []
    for title in sorted(loves, key=str.casefold):
        mapped = [(t, mapping[t]) for t in tags.get(title, []) if t in mapping]
        fact = facts.get(title)
        # Strict both ways: no TMDB fact, no region row, whatever the tags say.
        allowed = [(t, i) for t, i in mapped if not _is_region(themes[i]) or _in_region(themes[i], fact)]
        chosen = min((i for _, i in allowed if _is_region(themes[i])), default=None)
        if chosen is None and allowed:
            votes = Counter()
            for tag, i in allowed:
                votes[i] += 1 / math.sqrt(carriers[tag])
            # Highest vote, then the earlier theme.
            chosen = min(votes, key=lambda i: (-round(votes[i], 9), i))
        if chosen is None:
            unplaced.append(title)
            continue
        placed[chosen].append(title)
    if unplaced:
        log.info("%d loves have no tag mapped to a theme", len(unplaced))
    return placed, unplaced


def coverage_warnings(loves, unplaced, placed) -> list[str]:
    if not loves:
        return []
    thin = len(unplaced)
    warnings = []
    if thin / len(loves) > UNMAPPED_WARN_SHARE:
        warnings.append(f"{thin} of {len(loves)} loves fit no theme; your tastes may have shifted. "
                        "Run setup --rethink-themes to rebuild the themes.")
    biggest = max((len(m) for m in placed.values()), default=0)
    if biggest / len(loves) > BIGGEST_ROW_WARN_SHARE:
        warnings.append(f"One row holds {biggest} of {len(loves)} loves, more than a third. "
                        "Consider setup --rethink-themes.")
    return warnings


def _overlap(a: list[str], b: list[str]) -> float:
    a, b = set(a), set(b)
    return len(a & b) / len(a | b) if a | b else 1.0


def rows_needing_words(rows: list[dict], saved: dict) -> list[dict]:
    return [r for r in rows if r["id"] not in saved
            or _overlap(r["members"], saved[r["id"]].get("members", [])) < WORDS_KEEP_OVERLAP]


def words_prompt(rows: list[dict]) -> str:
    listed = "\n".join(f"{r['id']}: {r['label']}: {', '.join(r['members'][:WORDS_MEMBERS_SHOWN])}" for r in rows)
    return (
        "Write the headline and description for each of a household's taste rows below.\n"
        "name: 2 to 6 words, warm and plain-spoken, like a friend describing them. Not a joke or pun, "
        "and not a dry genre label. The right voice: \"Cosy British mysteries\", \"Hindi stories that feel "
        "like home\", \"Sci-fi that bends your brain\", \"History that leaves a mark\".\n"
        "description: 2 to 3 sentences in second person, playful, specific and affectionate, naming several "
        "of the row's titles. Not a report and not a personality test.\n"
        "positive_traits: 3 to 6 short phrases on what they respond to. mood_states: 1 to 3 short phrases.\n"
        "Return ONLY JSON mapping each row id to {\"name\", \"description\", \"positive_traits\", \"mood_states\"}.\n\n"
        + listed
    )


def _load_json(path) -> dict:
    try:
        data = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_words(rows: list[dict], client) -> dict:
    """Saved row words, with rows whose members moved a lot rewritten."""
    saved = _load_json(config.TASTE_WORDS_PATH)
    todo = rows_needing_words(rows, saved)
    if not todo:
        return saved
    try:
        text = client.generate(words_prompt(todo), role="reason",
                               max_tokens=config.TOKENS_PROFILE_MERGE, timeout=config.TIMEOUT_PROFILE_MERGE)
        answer = json.loads(_strip_json_fence(text))
    except Exception as exc:
        # Rows keep their old words, or show their label; the next build retries.
        log.warning("Taste row words failed, will retry next build: %s", exc)
        return saved
    for row in todo:
        words = answer.get(row["id"]) if isinstance(answer, dict) else None
        if isinstance(words, dict) and words.get("name"):
            saved[row["id"]] = {**words, "members": row["members"]}
    _write_json_atomic(saved, config.TASTE_WORDS_PATH)
    return saved


def build_tag_profile(loves_scores, enrichments, index_entries, collections, client, negative_prefs, rethink=False):
    """The structured profile from saved tags, themes and words. Returns (profile, warnings)."""
    loves = sorted(loves_scores, key=str.casefold)
    tags = tag_titles(loves, enrichments, client, config.TASTE_TAGS_PATH)
    loved_tags = {t: tags[t] for t in loves if t in tags}
    themes = load_themes(config.TASTE_THEMES_PATH)
    if rethink or not themes:
        themes = build_themes(loved_tags, client, config.TASTE_THEMES_PATH, themes)
    mapped = tag_to_theme(themes)
    ignored = load_ignored(config.TASTE_THEMES_PATH)
    unmapped = sorted({t for ts in loved_tags.values() for t in ts if t not in mapped and t not in ignored})
    if unmapped:
        themes = map_new_tags(themes, unmapped, client, config.TASTE_THEMES_PATH)
    facts = region_facts(index_entries, Path(config.CACHE_DIR), collections)
    placed, unplaced = place_loves(loves, loved_tags, themes, facts)
    rows = [{**themes[i], "members": placed[i]} for i in range(len(themes)) if placed[i]]
    saved = _write_words(rows, client)

    clusters = []
    for row in rows:
        words = saved.get(row["id"], {})
        clusters.append({
            "id": row["id"], "label": row["label"], "name": words.get("name", ""),
            "description": words.get("description", ""),
            "positive_traits": words.get("positive_traits", []), "mood_states": words.get("mood_states", []),
            "co_viewing": "family" if row["family"] else "personal",
            "regions": [row["region"]] if row["region"] else [],
            "languages": [row["language"]] if row["language"] else [],
            "representative_titles": row["members"][:6], "members": row["members"],
        })
    profile = validate_structured_profile({"clusters": clusters})
    profile = apply_member_weights(profile, loves_scores)
    profile["negative_preferences"] = explicit_dislikes(negative_prefs)
    return profile, coverage_warnings(loves, unplaced, placed)
