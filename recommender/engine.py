from dataclasses import dataclass

from .tmdb_client import TmdbMetadata
from .taste_profile import build_tag_vector


@dataclass
class Recommendation:
    title: str
    content_type: str
    score: float
    vote_average: float
    genres: list[str]
    because_you_watched: str


def _dot(profile: dict[str, float], tag_vector: dict[str, float]) -> float:
    return sum(profile.get(tag, 0.0) * weight for tag, weight in tag_vector.items())


def _find_because(
    candidate_vector: dict[str, float],
    watched_metadata: dict[str, TmdbMetadata],
) -> str:
    best_title, best_score = "", -1.0
    for title, meta in watched_metadata.items():
        s = _dot(candidate_vector, build_tag_vector(meta))
        if s > best_score:
            best_score, best_title = s, title
    return best_title


def recommend(
    content_type: str,
    taste_profile: dict[str, float],
    candidates: list[TmdbMetadata],
    watched_titles: set[str],
    watched_metadata: dict[str, TmdbMetadata],
    top_n: int = 10,
    min_vote_count: int = 100,
) -> list[Recommendation]:
    """Score and rank candidates against taste profile, excluding watched titles."""
    results = []
    for meta in candidates:
        if meta.title in watched_titles:
            continue
        if meta.vote_count < min_vote_count:
            continue
        candidate_vector = build_tag_vector(meta)
        score = _dot(taste_profile, candidate_vector) + 0.1 * (meta.vote_average / 10.0)
        results.append(Recommendation(
            title=meta.title,
            content_type=content_type,
            score=score,
            vote_average=meta.vote_average,
            genres=meta.genres,
            because_you_watched=_find_because(candidate_vector, watched_metadata),
        ))
    results.sort(key=lambda r: r.score, reverse=True)
    return results[:top_n]
