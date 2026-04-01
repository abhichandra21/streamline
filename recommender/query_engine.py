import json
import re
from dataclasses import dataclass

import anthropic

from .ingestion.base import WatchEvent
from .models import Recommendation
from .tmdb_client import TmdbClient, TmdbMetadata


@dataclass
class QueryIntent:
    genres: list[str]
    origin_countries: list[str]
    languages: list[str]
    mood_descriptors: list[str]
    similar_to: list[str]
    max_runtime_minutes: int | None
    year_from: int | None
    year_to: int | None
    unwatched_only: bool
    special_intent: str | None  # "abandoned", "watchlist", "family", or None
    content_type: str           # "tv", "movie", or "both"
    top_n: int                  # 1 for single recommendation, >1 for "a few" queries


@dataclass
class RecommendContext:
    taste_profile: str
    watch_index: "WatchIndex"
    events: list[WatchEvent]
    tmdb_client: TmdbClient
    anthropic_client: anthropic.Anthropic
    cache_dir: str


def _parse_json_response(text: str) -> dict | list:
    text = text.strip()
    if text.startswith('```'):
        text = re.sub(r'^```(?:json)?\n?', '', text)
        text = re.sub(r'\n?```$', '', text)
    return json.loads(text.strip())


def parse_intent(query: str, client: anthropic.Anthropic) -> QueryIntent:
    """Parse a natural language query into structured intent using Claude Sonnet."""
    message = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=400,
        messages=[{
            "role": "user",
            "content": (
                'Parse this streaming recommendation query into structured intent. '
                'Return ONLY valid JSON with these fields:\n'
                '- genres: list of genre strings e.g. ["crime", "drama"]\n'
                '- origin_countries: list of ISO-3166 alpha-2 codes e.g. ["GB", "IN"]\n'
                '- languages: list of ISO-639-1 codes e.g. ["hi", "en"]\n'
                '- mood_descriptors: list of mood/tone words e.g. ["slow-burn", "feel-good"]\n'
                '- similar_to: list of title names for similarity search\n'
                '- max_runtime_minutes: integer or null\n'
                '- year_from: integer or null\n'
                '- year_to: integer or null\n'
                '- unwatched_only: boolean (default true)\n'
                '- special_intent: one of "abandoned", "watchlist", "family" or null\n'
                '- content_type: "tv", "movie", or "both"\n'
                '- top_n: integer — 1 for single recommendation (default), 3-5 if query implies '
                '"a few" or "some options" or "what should I watch"\n\n'
                f'Query: "{query}"'
            ),
        }],
    )
    data = _parse_json_response(message.content[0].text)
    return QueryIntent(**data)
