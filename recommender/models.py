from dataclasses import dataclass, field


@dataclass
class Recommendation:
    title: str
    content_type: str
    score: float
    vote_average: float
    genres: list[str]
    explanation: str
    streaming_providers: list[str] = field(default_factory=list)
