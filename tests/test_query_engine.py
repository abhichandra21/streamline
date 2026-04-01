import json
from unittest.mock import MagicMock

from recommender.query_engine import QueryIntent, parse_intent


def make_intent_client(intent_dict: dict):
    client = MagicMock()
    msg = MagicMock()
    msg.content = [MagicMock(text=json.dumps(intent_dict))]
    client.messages.create.return_value = msg
    return client


BRITISH_CRIME_INTENT = {
    "genres": ["crime", "drama"],
    "origin_countries": ["GB"],
    "languages": [],
    "mood_descriptors": ["slow-burn"],
    "similar_to": [],
    "max_runtime_minutes": None,
    "year_from": None,
    "year_to": None,
    "unwatched_only": True,
    "special_intent": None,
    "content_type": "tv",
    "top_n": 1,
}


def test_parse_intent_returns_query_intent():
    client = make_intent_client(BRITISH_CRIME_INTENT)
    intent = parse_intent("good British crime drama", client)
    assert isinstance(intent, QueryIntent)


def test_parse_intent_extracts_genres():
    client = make_intent_client(BRITISH_CRIME_INTENT)
    intent = parse_intent("good British crime drama", client)
    assert "crime" in intent.genres
    assert "drama" in intent.genres


def test_parse_intent_extracts_origin_country():
    client = make_intent_client(BRITISH_CRIME_INTENT)
    intent = parse_intent("good British crime drama", client)
    assert "GB" in intent.origin_countries


def test_parse_intent_uses_sonnet():
    client = make_intent_client(BRITISH_CRIME_INTENT)
    parse_intent("good British crime drama", client)
    call_kwargs = client.messages.create.call_args[1]
    assert call_kwargs["model"] == "claude-sonnet-4-6"


def test_parse_intent_handles_markdown_wrapped_json():
    client = MagicMock()
    msg = MagicMock()
    msg.content = [MagicMock(text="```json\n" + json.dumps(BRITISH_CRIME_INTENT) + "\n```")]
    client.messages.create.return_value = msg
    intent = parse_intent("good British crime drama", client)
    assert intent.genres == ["crime", "drama"]


def test_parse_intent_abandoned_special():
    abandoned_intent = {**BRITISH_CRIME_INTENT, "special_intent": "abandoned",
                        "genres": [], "origin_countries": [],
                        "similar_to": ["Tandav"], "content_type": "tv", "top_n": 1}
    client = make_intent_client(abandoned_intent)
    intent = parse_intent("I started Tandav and stopped", client)
    assert intent.special_intent == "abandoned"
    assert "Tandav" in intent.similar_to


def test_parse_intent_bollywood():
    bollywood_intent = {
        "genres": ["romance"], "origin_countries": ["IN"],
        "languages": ["hi"], "mood_descriptors": ["feel-good"],
        "similar_to": [], "max_runtime_minutes": None,
        "year_from": 1990, "year_to": 1999,
        "unwatched_only": True, "special_intent": None, "content_type": "movie",
        "top_n": 1,
    }
    client = make_intent_client(bollywood_intent)
    intent = parse_intent("feel-good Bollywood romance from the 90s", client)
    assert "hi" in intent.languages
    assert intent.year_from == 1990
    assert intent.content_type == "movie"
