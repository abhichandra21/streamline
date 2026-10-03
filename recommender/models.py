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
    # Which service vote_average came from: "imdb" or "tmdb".
    rating_source: str = "tmdb"
