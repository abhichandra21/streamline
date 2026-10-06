"""Loved It: pick sets of archive titles for the owner to tap the ones they loved.

Each set spreads across kinds of title the owner hasn't been shown yet, then
leans toward the kinds they tap, and lets kinds they never tap fade. Taps are
the only signal used; how much of something was watched is never one.
"""
import json
import random
from collections import defaultdict
from itertools import zip_longest
from pathlib import Path
from typing import NamedTuple

SET_SIZE = 24
# Fewer shown than this and a group is still being explored.
NEW_SHOWN = 4
# This many shown with no love and a group fades.
COLD_SHOWN = 15
COLD_FACTOR = 0.1
KIDS_GROUP = "Kids & family"
KIDS_FACTOR = 0.3
# A region group smaller than this joins its plain genre group.
MIN_REGION_GROUP = 10

LANGUAGES = {
    "ko": "Korean", "es": "Spanish", "fr": "French", "ja": "Japanese", "de": "German",
    "it": "Italian", "ta": "Tamil", "te": "Telugu", "ml": "Malayalam",
}
# Checked in order against all of a title's genres, so a British drama that is
# also a mystery lands under Crime rather than Drama.
GENRE_ORDER = [
    ({"Crime", "Mystery"}, "Crime"),
    ({"Documentary"}, "Documentary"),
    ({"Animation", "Family", "Kids"}, KIDS_GROUP),
    ({"Comedy"}, "Comedy"),
    ({"Science Fiction", "Fantasy", "Sci-Fi & Fantasy"}, "Sci-fi"),
    ({"Romance"}, "Romance"),
    ({"Action", "Thriller", "Action & Adventure"}, "Action"),
    ({"War", "History", "War & Politics"}, "History"),
]


class Candidate(NamedTuple):
    content_type: str
    tmdb_id: int
    title: str
    year: int | None
    poster_path: str
    group: str
    real: bool
    votes: int
    sources: tuple[str, ...] = ()

    @property
    def key(self) -> str:
        return f"{self.content_type}:{self.tmdb_id}"


def group_key(meta: dict, content_type: str) -> str:
    """Region or language, then main genre, like "British · Crime" or "Drama"."""
    names = [g.get("name", "") for g in meta.get("genres") or []]
    genre = next((label for wanted, label in GENRE_ORDER if wanted & set(names)),
                 names[0] if names else "Other")
    if genre == KIDS_GROUP:
        return genre
    # The first origin country: Hollywood films often list GB among their
    # production countries without being British.
    countries = meta.get("origin_country") or [
        c.get("iso_3166_1") for c in meta.get("production_countries") or []]
    language = meta.get("original_language") or ""
    if countries[:1] == ["GB"]:
        region = "British"
    elif language == "hi":
        region = "Hindi"
    elif language and language != "en":
        region = LANGUAGES.get(language, language.upper())
    else:
        region = ""
    return f"{region} · {genre}" if region else genre


def merge_small_groups(titles: list[Candidate]) -> list[Candidate]:
    """Fold region groups with too few titles into their plain genre group."""
    sizes: dict[str, int] = defaultdict(int)
    for t in titles:
        sizes[t.group] += 1
    return [
        t._replace(group=t.group.split(" · ", 1)[1])
        if " · " in t.group and sizes[t.group] < MIN_REGION_GROUP else t
        for t in titles
    ]


def _year(meta: dict) -> int | None:
    date = meta.get("release_date") or meta.get("first_air_date") or ""
    return int(date[:4]) if date[:4].isdigit() else None


def load_archive(index_entries: list[dict], cache_dir: Path) -> list[Candidate]:
    """Every archive title that has a TMDB ID and a poster, grouped."""
    titles = []
    for e in index_entries:
        ct, tmdb_id = e.get("content_type"), e.get("tmdb_id")
        if not tmdb_id or ct not in ("movie", "tv"):
            continue
        try:
            meta = json.loads((cache_dir / ct / f"{tmdb_id}.json").read_text())
        except (OSError, ValueError):
            continue
        if not meta.get("poster_path"):
            continue
        titles.append(Candidate(
            # TMDB's name: export titles can carry episode text.
            content_type=ct, tmdb_id=tmdb_id, title=meta.get("name") or meta.get("title") or e["title"],
            year=_year(meta),
            poster_path=meta["poster_path"], group=group_key(meta, ct),
            real=any(p not in ("manual", "archive") for p in e.get("platforms") or []),
            votes=meta.get("vote_count") or 0,
            sources=tuple(e.get("platforms") or ()),
        ))
    return merge_small_groups(titles)


def _alternate_types(titles: list[Candidate]) -> list[Candidate]:
    """Best known first, alternating shows and films.

    Films get far more TMDB votes than shows, so one ranking would bury shows.
    """
    by_type = {ct: sorted((t for t in titles if t.content_type == ct), key=lambda t: (-t.votes, t.title))
               for ct in ("tv", "movie")}
    ordered = []
    for pair in zip_longest(by_type["tv"], by_type["movie"]):
        ordered.extend(t for t in pair if t is not None)
    return ordered


def next_set(
    pool: list[Candidate],
    history: dict[str, tuple[int, int]],
    size: int = SET_SIZE,
    rng: random.Random | None = None,
) -> list[Candidate]:
    """Pick the next set. history[group] = (shown, loved) so far.

    A third of the set explores groups shown fewer than NEW_SHOWN times, one
    title per group per round. One slot goes to the least-shown faded group,
    so a late love can still surface. The rest lean toward groups the owner
    loves. Within a group, the best known come first, whatever the source
    (the downloads list can hold gems as well as streaming), shows and films
    alternating.
    """
    rng = rng or random.Random()
    groups: dict[str, list[Candidate]] = defaultdict(list)
    for t in pool:
        groups[t.group].append(t)
    for group, titles in groups.items():
        groups[group] = _alternate_types(titles)

    def stats(group: str) -> tuple[int, int]:
        return history.get(group, (0, 0))

    def is_cold(group: str) -> bool:
        shown, loved = stats(group)
        return shown >= COLD_SHOWN and loved == 0

    def weight(group: str) -> float:
        shown, loved = stats(group)
        w = (loved + 1) / (shown + 2)
        if is_cold(group):
            w *= COLD_FACTOR
        if group == KIDS_GROUP:
            w *= KIDS_FACTOR
        return w

    picked: list[Candidate] = []

    # Explore the big kinds of title first, kids' titles last.
    new = sorted((g for g in groups if stats(g)[0] < NEW_SHOWN),
                 key=lambda g: (g == KIDS_GROUP, stats(g)[0], -len(groups[g]), g))
    explore = size // 3
    while len(picked) < explore and any(groups[g] for g in new):
        for g in new:
            if groups[g] and len(picked) < explore:
                picked.append(groups[g].pop(0))

    cold = sorted((g for g in groups if groups[g] and is_cold(g)), key=lambda g: (stats(g)[0], g))
    if cold and len(picked) < size:
        picked.append(groups[cold[0]].pop(0))

    while len(picked) < size:
        live = [g for g in groups if groups[g]]
        if not live:
            break
        group = rng.choices(live, weights=[weight(g) for g in live])[0]
        picked.append(groups[group].pop(0))
    return picked
