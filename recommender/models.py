from dataclasses import dataclass


@dataclass
class Recommendation:
    title: str
    content_type: str
    score: float
    vote_average: float
    genres: list[str]
    explanation: str
