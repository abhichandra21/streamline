import math
from .tmdb_client import TmdbMetadata

TAG_WEIGHTS = {
    "genre": 1.0,
    "keyword": 1.5,
    "lang": 0.8,
    "person": 0.5,
}


def build_tag_vector(metadata: TmdbMetadata) -> dict[str, float]:
    """Build a weighted tag vector for a single title."""
    vector: dict[str, float] = {}
    for genre in metadata.genres:
        vector[f"genre:{genre.lower()}"] = TAG_WEIGHTS["genre"]
    for kw in metadata.keywords:
        vector[f"keyword:{kw.lower()}"] = TAG_WEIGHTS["keyword"]
    vector[f"lang:{metadata.original_language}"] = TAG_WEIGHTS["lang"]
    if metadata.creator_or_director:
        vector[f"person:{metadata.creator_or_director.lower()}"] = TAG_WEIGHTS["person"]
    for person in metadata.cast:
        vector[f"person:{person.lower()}"] = TAG_WEIGHTS["person"]
    return vector


def build_profile(
    scores: dict[str, float],
    metadata: dict[str, TmdbMetadata],
) -> dict[str, float]:
    """
    Build a normalized taste profile vector.

    scores:   {title -> implicit_score (0-1)}
    metadata: {title -> TmdbMetadata}
    Returns:  {tag -> normalized_weight} — L2 normalized
    """
    profile: dict[str, float] = {}
    for title, score in scores.items():
        if title not in metadata:
            continue
        for tag, weight in build_tag_vector(metadata[title]).items():
            profile[tag] = profile.get(tag, 0.0) + score * weight

    magnitude = math.sqrt(sum(v * v for v in profile.values()))
    if magnitude > 0:
        profile = {tag: v / magnitude for tag, v in profile.items()}

    return profile
