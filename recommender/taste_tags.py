"""Taste tags: a few short phrases per loved title, written once and kept.

Tags turn each title's enrichment into something code can group. They are
saved forever, so grouping never re-rolls; only new titles are tagged.
"""
from __future__ import annotations

import json
import logging
import os
import re
import tempfile
from collections import Counter
from pathlib import Path

from rapidfuzz import fuzz

import config
from .structured_profile import _strip_json_fence as _strip_fence

log = logging.getLogger("recommender.taste_tags")

# Spelling variants closer than this join the existing tag. "india"/"indian" and
# "cozy"/"cosy" sit just under it; the theme step folds those, so don't lower it.
# Plurals are matched separately by _singular ("series" becomes "sery" on both sides).
TAG_MATCH_SCORE = 92


def load_tags(path: str | Path) -> dict[str, list[str]]:
    source = Path(path)
    if not source.exists():
        return {}
    try:
        data = json.loads(source.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("Ignoring unreadable taste tags at %s: %s", source, exc)
        return {}
    return {str(k): [str(t) for t in v] for k, v in data.items() if isinstance(v, list)}


def save_tags(tags: dict[str, list[str]], path: str | Path) -> None:
    _write_json_atomic(dict(sorted(tags.items())), path)


def vocabulary(tags: dict[str, list[str]]) -> Counter:
    return Counter(t for ts in tags.values() for t in set(ts))


def _singular(tag: str) -> str:
    words = tag.split()
    last = words[-1]
    if last.endswith("ies") and len(last) > 4:
        last = last[:-3] + "y"
    elif last.endswith("s") and not last.endswith("ss") and len(last) > 3:
        last = last[:-1]
    return " ".join(words[:-1] + [last])


def normalize_tag(tag: str, vocab: Counter) -> str:
    cleaned = re.sub(r"\s+", " ", str(tag)).strip().lower()
    if not cleaned or cleaned in vocab:
        return cleaned
    single = _singular(cleaned)
    for known in vocab:
        if _singular(known) == single:
            return known
    # Fuzzy only catches spelling slips; true synonyms are the theme step's job.
    close = [(fuzz.ratio(single, _singular(known)), vocab[known], known) for known in vocab]
    close = [c for c in close if c[0] >= TAG_MATCH_SCORE]
    return max(close)[2] if close else cleaned


def _write_json_atomic(data: object, path: str | Path) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=target.parent, suffix=".tmp")
    with os.fdopen(fd, "w") as handle:
        json.dump(data, handle, indent=1, sort_keys=True)
    os.replace(tmp, target)


TAG_BATCH_SIZE = 25
VOCAB_SHOWN = 200
# Below this the vocabulary is too thin to demand reuse (the very first batches).
MIN_VOCAB_FOR_REUSE = 50
MAX_TAGS = 8
MAX_TAG_WORDS = 4


def tag_prompt(batch: list[tuple[str, str]], vocab: Counter) -> str:
    shown = ", ".join(t for t, _ in sorted(vocab.items(), key=lambda kv: (-kv[1], kv[0]))[:VOCAB_SHOWN])
    blocks = "\n\n".join(f"### {title}\n{re.sub(r'^#.*\n+', '', text).strip()}" for title, text in batch)
    return (
        "Tag titles a household loved, so an app can group them into the household's tastes.\n"
        "For each title write 5 to 8 taste tags: short lowercase phrases of 1 to 3 words naming what "
        "someone who loves it responds to: subgenre, tone or mood, setting (place, era), format when it "
        "matters (documentary, stand-up, reality), and \"kids\" for children's titles.\n"
        "Add a country or language tag when it is not American or English-language, like \"hindi\" or "
        "\"korean\". Use \"british\" only for titles made in Britain (BBC, ITV, Channel 4 and the like, "
        "on any service), never for titles merely set or shot there. Keep \"set in india\" apart from \"hindi\".\n"
        "For example, Ted Lasso (American, set in England) and 1917 are not british; Grand Designs is.\n"
        + (
            "Use at least 3 tags from the existing tags below and at most 2 new ones per title. "
            "Never write a spelling or synonym variant of an existing tag.\n"
            f"Existing tags: {shown}\n\n"
            if len(vocab) >= MIN_VOCAB_FOR_REUSE else "\n"
        )
        + "Return ONLY JSON mapping each title exactly as written after ### to its list of tags.\n\n"
        + blocks
    )


def parse_tag_answer(text: str, titles: list[str], vocab: Counter) -> dict[str, list[str]]:
    try:
        data = json.loads(_strip_fence(text))
    except json.JSONDecodeError:
        log.warning("Taste tag answer was not JSON")
        return {}
    if not isinstance(data, dict):
        return {}
    # Exact first; models sometimes drop quotes or change case in a title like "Sr.".
    exact = set(titles)
    loose = {t.strip('"\'').casefold(): t for t in titles}
    parsed = {}
    for answered, raw in data.items():
        title = answered if answered in exact else loose.get(str(answered).strip('"\'').casefold())
        if title is None or not isinstance(raw, list):
            continue
        cleaned = []
        for tag in raw:
            tag = normalize_tag(tag, vocab)
            if tag and len(tag.split()) <= MAX_TAG_WORDS and tag not in cleaned:
                cleaned.append(tag)
        if cleaned:
            parsed[title] = cleaned[:MAX_TAGS]
    return parsed


def pending_batches(titles: list[str], enrichments: dict[str, str], tags: dict) -> list[list[tuple[str, str]]]:
    todo = [(t, enrichments[t]) for t in sorted(set(titles), key=str.casefold)
            if t not in tags and enrichments.get(t)]
    return [todo[i:i + TAG_BATCH_SIZE] for i in range(0, len(todo), TAG_BATCH_SIZE)]


def apply_answer(text: str, batch: list[tuple[str, str]], path: str | Path) -> int:
    """Check one batch answer and save its tags; returns how many titles were saved."""
    tags = load_tags(path)
    parsed = parse_tag_answer(text, [t for t, _ in batch], vocabulary(tags))
    tags.update(parsed)
    save_tags(tags, path)
    return len(parsed)


def tag_titles(titles: list[str], enrichments: dict[str, str], client, path: str | Path) -> dict[str, list[str]]:
    """Tag every title not tagged yet; returns all saved tags. A failed batch is retried next build."""
    for batch in pending_batches(titles, enrichments, load_tags(path)):
        prompt = tag_prompt(batch, vocabulary(load_tags(path)))
        try:
            text = client.generate(prompt, role="fast", max_tokens=4000, timeout=config.TIMEOUT_PROFILE_MERGE)
        except Exception as exc:
            log.warning("Taste tagging batch failed, will retry next build: %s", exc)
            continue
        apply_answer(text, batch, path)
    return load_tags(path)
