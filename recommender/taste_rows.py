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
WORDS_TAGS_SHOWN = 8
# Bump when the words prompt changes, so every row is rewritten once.
WORDS_VERSION = 2
# A title with fewer usable tags than this is placed by the AI, once, not by one stray tag.
MIN_TAGS_FOR_CODE_PLACEMENT = 2
# A vote this close to the runner-up is a coin toss on one stray tag; the AI decides it instead.
CLEAR_WIN_RATIO = 1.5
# Saved for a title the AI skipped: placed by the code's vote, never asked again.
VOTE = "vote"


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
            # A film's origin, not its co-producers: 1917 is US-origin with GB money.
            countries = set(meta.get("origin_country") or []) or {
                c.get("iso_3166_1") for c in meta.get("production_countries") or [] if c.get("iso_3166_1")}
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


def _allowed(title, tags, themes, facts, mapping):
    """The title's mapped tags, minus region tags its TMDB facts don't back."""
    fact = facts.get(title)
    return [(t, mapping[t]) for t in tags.get(title, []) if t in mapping
            and (not _is_region(themes[mapping[t]]) or _in_region(themes[mapping[t]], fact))]


def _votes(allowed, carriers) -> Counter:
    votes = Counter()
    for tag, i in allowed:
        votes[i] += 1 / math.sqrt(carriers[tag])
    return votes


def doubtful_titles(loves, tags, themes, facts) -> list[str]:
    """Titles code can't place with confidence: too few usable tags, or a near-tie between two themes."""
    mapping = tag_to_theme(themes)
    carriers = Counter(t for title in loves for t in set(tags.get(title, [])))
    doubtful = []
    for title in loves:
        allowed = _allowed(title, tags, themes, facts, mapping)
        if any(_is_region(themes[i]) for _, i in allowed):
            continue  # the facts decide region rows
        top = [v for _, v in _votes(allowed, carriers).most_common(2)]
        if len(allowed) < MIN_TAGS_FOR_CODE_PLACEMENT or (len(top) == 2 and top[0] < CLEAR_WIN_RATIO * top[1]):
            doubtful.append(title)
    return sorted(doubtful, key=str.casefold)


def _theme_line(theme: dict) -> str:
    if theme["region"]:
        return f"{theme['id']}: {theme['label']} (only for titles made in {theme['region']})"
    if theme["language"]:
        return f"{theme['id']}: {theme['label']} (only for titles in language {theme['language']})"
    return f"{theme['id']}: {theme['label']}"


def ai_placements(titles: list[str], tags: dict, themes: list[dict], client, reset: bool = False,
                  facts: dict | None = None) -> dict[str, int | None]:
    """The AI's saved pick of theme for titles with thin tag evidence; asked once per title.

    None means the AI judged that no theme fits, so the title stays out of every row.
    """
    saved = {} if reset else _load_json(config.TASTE_PLACEMENTS_PATH)
    index = {t["id"]: i for i, t in enumerate(themes)}
    # An answer the region rule blocks is a wrong answer: ask again.
    for title in titles:
        i = index.get(saved.get(title))
        if i is not None and _is_region(themes[i]) and not _in_region(themes[i], (facts or {}).get(title)):
            del saved[title]
    todo = [t for t in titles if t not in saved]
    if todo:
        prompt = (
            "A household's taste themes:\n"
            + "\n".join(_theme_line(t) for t in themes)
            + "\n\nFor each loved title below, answer the id of the theme it belongs in, judged by what the "
            "title is, or \"none\" if it fits none. Return ONLY JSON mapping title to id.\n\n"
            + "\n".join(f"{t}: {', '.join(tags.get(t, []))}" for t in todo)
        )
        try:
            answer = json.loads(_strip_json_fence(client.generate(
                prompt, role="reason", max_tokens=4000, timeout=config.TIMEOUT_PROFILE_MERGE)))
        except Exception as exc:
            log.warning("Could not place thin-evidence titles, will retry next build: %s", exc)
            answer = None
        if isinstance(answer, dict):
            # Models drop quotes or change case in titles like "Sr.".
            loose = {str(k).strip('"\'').casefold(): v for k, v in answer.items()}
            for title in todo:
                found = answer.get(title, loose.get(title.strip('"\'').casefold()))
                if found is None:
                    saved[title] = VOTE  # skipped: code's vote decides, and it isn't asked again
                    continue
                pick = str(found)
                i = index.get(pick)
                # A pick the region rule blocks fits nowhere it's allowed; never ask again.
                if i is not None and _is_region(themes[i]) and not _in_region(themes[i], (facts or {}).get(title)):
                    pick = "none"
                saved[title] = pick
            _write_json_atomic(saved, config.TASTE_PLACEMENTS_PATH)
    return {t: index.get(saved[t]) for t in titles if t in saved and saved[t] != VOTE}


def place_loves(loves, tags, themes, facts, overrides=None):
    mapping = tag_to_theme(themes)
    overrides = overrides or {}
    carriers = Counter(t for title in loves for t in set(tags.get(title, [])))
    placed = {i: [] for i in range(len(themes))}
    unplaced = []
    for title in sorted(loves, key=str.casefold):
        # Strict both ways: no TMDB fact, no region row, whatever the tags or the AI say.
        allowed = _allowed(title, tags, themes, facts, mapping)
        if title in overrides:
            override = overrides[title]
            if override is None:
                unplaced.append(title)
                continue
            if not _is_region(themes[override]) or _in_region(themes[override], facts.get(title)):
                placed[override].append(title)
                continue
        chosen = min((i for _, i in allowed if _is_region(themes[i])), default=None)
        if chosen is None and allowed:
            votes = _votes(allowed, carriers)
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
    return [r for r in rows if r["id"] not in saved or saved[r["id"]].get("version") != WORDS_VERSION
            or _overlap(r["members"], saved[r["id"]].get("members", [])) < WORDS_KEEP_OVERLAP]


def _spread(members: list[str]) -> list[str]:
    """An even sample across the row; the first 40 alphabetically skew the name."""
    step = max(1, math.ceil(len(members) / WORDS_MEMBERS_SHOWN))
    return members[::step]


def words_prompt(rows: list[dict]) -> str:
    listed = "\n".join(
        f"{r['id']}: {r['label']} ({len(r['members'])} titles; main tags: {', '.join(r.get('top_tags', []))}): "
        f"{', '.join(_spread(r['members']))}" for r in rows)
    return (
        "Write the headline and description for each of a household's taste rows below.\n"
        "name: 2 to 6 words, warm and plain-spoken, like a friend describing them. Not a joke or pun, "
        "and not a dry genre label. The right voice: \"Cosy British mysteries\", \"Hindi stories that feel "
        "like home\", \"Sci-fi that bends your brain\", \"History that leaves a mark\".\n"
        "The name must fit the whole row, judged by its main tags and the spread of titles, not just a few.\n"
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
            saved[row["id"]] = {**words, "members": row["members"], "version": WORDS_VERSION}
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
    doubtful = doubtful_titles(loves, loved_tags, themes, facts)
    overrides = ai_placements(doubtful, loved_tags, themes, client, reset=rethink, facts=facts) if doubtful else {}
    placed, unplaced = place_loves(loves, loved_tags, themes, facts, overrides)
    rows = []
    for i, theme in enumerate(themes):
        if placed[i]:
            top = Counter(t for title in placed[i] for t in loved_tags.get(title, []))
            rows.append({**theme, "members": placed[i],
                         "top_tags": [t for t, _ in top.most_common(WORDS_TAGS_SHOWN)]})
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
