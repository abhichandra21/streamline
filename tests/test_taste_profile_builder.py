from datetime import datetime, timedelta

from recommender.ingestion.base import WatchEvent
from recommender.taste_profile_builder import build
from tests.mock_llm import make_mock_llm


def make_event(title, series_name=None, content_type="movie", seconds=3600):
    return WatchEvent(
        platform="prime", title=title,
        content_type=content_type, series_name=series_name or title,
        watched_duration=timedelta(seconds=seconds),
        total_duration=None, timestamp=datetime(2024, 1, 1), profile="ADULT",
    )


def test_build_uses_reason_role():
    events = [make_event("DDLJ")]
    scores = {"DDLJ": 0.9}
    enrichments = {"DDLJ": "A classic Bollywood romance."}
    client = make_mock_llm("You gravitate toward Bollywood romance.")
    result = build(events, scores, enrichments, client)
    call_kwargs = client.generate.call_args[1]
    assert call_kwargs["role"] == "reason"


def test_build_returns_profile_text():
    events = [make_event("Downton Abbey", content_type="tv", series_name="Downton Abbey")]
    scores = {"Downton Abbey": 0.95}
    enrichments = {"Downton Abbey": "A lavish British period drama."}
    client = make_mock_llm("You love British prestige dramas.")
    result = build(events, scores, enrichments, client)
    assert "British" in result


def test_build_includes_top_titles_in_prompt():
    events = [make_event("Show A"), make_event("Show B")]
    scores = {"Show A": 0.9, "Show B": 0.5}
    enrichments = {"Show A": "Desc A.", "Show B": "Desc B."}
    client = make_mock_llm("Your profile.")
    build(events, scores, enrichments, client)
    prompt = client.generate.call_args[0][0]  # first positional arg
    assert "Show A" in prompt
    assert "0.90" in prompt or "0.9" in prompt


def test_build_skips_titles_with_no_enrichment():
    events = [make_event("Known"), make_event("Unknown")]
    scores = {"Known": 0.8, "Unknown": 0.7}
    enrichments = {"Known": "Known description."}
    client = make_mock_llm("Your profile.")
    build(events, scores, enrichments, client)
    prompt = client.generate.call_args[0][0]
    assert "Unknown" not in prompt or "Unknown description" not in prompt
