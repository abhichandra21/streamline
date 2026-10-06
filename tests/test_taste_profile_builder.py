from datetime import datetime, timedelta

from recommender.ingestion.base import WatchEvent
from recommender.taste_profile_builder import _merge_profiles, build
from tests.mock_llm import make_mock_llm, make_mock_llm_sequence


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


def test_merge_profiles_caps_final_profile_to_fifteen_sections():
    cluster_list = "\n".join(f"{i}. Cluster {i}" for i in range(1, 29))
    final_profile = "\n".join(
        f"## {i}. Cluster {i}\nYou like pattern {i}."
        for i in range(1, 18)
    )
    client = make_mock_llm_sequence([cluster_list, final_profile])

    result = _merge_profiles(["batch profile"], client)

    headings = [line for line in result.splitlines() if line.startswith("## ")]
    assert len(headings) == 15
    assert headings[-1] == "## 15. Cluster 15"
    assert "## 16. Cluster 16" not in result


def test_merge_profiles_reorders_family_sections_after_personal_before_capping():
    family_clusters = [
        "Disney/Pixar Animated Canon",
        "Disney Channel & Family Television",
        "Christmas & Holiday Content",
    ]
    personal_clusters = [
        "British Crime & Police Drama",
        "Prestige American Institutional Drama",
        "Spy & Espionage Thriller",
        "Romantic Period Drama",
        "Musical Drama & Biography",
        "Investigative Documentary & True Story",
        "Philosophical Science Fiction",
        "Nordic Noir & Remote Setting Crime",
        "Political Satire & Current Affairs Media",
        "Food, Culinary & Travel Documentary",
        "South Asian Cinema & Indian Content",
        "Psychological & Puzzle-Box Thriller",
        "Slow-Burn Prestige Crime & Noir",
    ]
    all_clusters = family_clusters + personal_clusters
    cluster_list = "\n".join(
        f"{index}. {cluster}"
        for index, cluster in enumerate(all_clusters, start=1)
    )
    final_profile = "\n".join(
        f"## {index}. {cluster}\nYou like {cluster}."
        for index, cluster in enumerate(all_clusters, start=1)
    )
    client = make_mock_llm_sequence([cluster_list, final_profile])

    result = _merge_profiles(["batch profile"], client)

    headings = [line for line in result.splitlines() if line.startswith("## ")]
    assert len(headings) == 15
    assert headings[0] == "## 1. British Crime & Police Drama"
    assert headings[12] == "## 13. Slow-Burn Prestige Crime & Noir"
    assert headings[13] == "## 14. Disney/Pixar Animated Canon"
    assert headings[14] == "## 15. Disney Channel & Family Television"
    assert "Christmas & Holiday Content" not in result
    for index, heading in enumerate(headings, start=1):
        assert heading.startswith(f"## {index}. ")

    final_prompt = client.generate.call_args_list[1].args[0]
    cluster_block = final_prompt.split("CONSOLIDATED CLUSTERS:\n", 1)[1]
    cluster_block = cluster_block.split("\n\nWrite the final", 1)[0]
    assert cluster_block.splitlines()[0] == "1. British Crime & Police Drama"


def _dated(title, day):
    e = make_event(title)
    e.timestamp = datetime(2024, 1, day)
    return e


def test_sort_scored_ties_go_to_most_recent():
    from recommender.taste_profile_builder import sort_scored
    events = [_dated("Old", 1), _dated("Newest", 20), _dated("Mid", 10), _dated("Liked", 2)]
    scores = {"Old": 1.0, "Newest": 1.0, "Mid": 1.0, "Liked": 1.3}
    enrichments = {t: "x" for t in scores}
    assert [t for t, _ in sort_scored(events, scores, enrichments)] == ["Liked", "Newest", "Mid", "Old"]


def test_equal_weight_prompt_when_signals_off(monkeypatch):
    import config
    monkeypatch.setattr(config, "USE_VIEWING_SIGNALS", False)
    client = make_mock_llm("profile")
    build([make_event("A")], {"A": 1.0}, {"A": "x"}, client)
    prompt = client.generate.call_args[0][0]
    assert "equal baseline weight of 1.0" in prompt
    assert "engagement" not in prompt
    assert "finish" not in prompt


def test_engagement_prompt_when_signals_on(monkeypatch):
    import config
    monkeypatch.setattr(config, "USE_VIEWING_SIGNALS", True)
    client = make_mock_llm("profile")
    build([make_event("A")], {"A": 0.5}, {"A": "x"}, client)
    prompt = client.generate.call_args[0][0]
    assert "sorted by engagement score" in prompt
    assert "consistently finish" in prompt


def test_sort_scored_ignores_synthetic_timestamps():
    from recommender.taste_profile_builder import sort_scored
    manual = _dated("Manual", 28)
    manual.platform = "manual"
    events = [_dated("Real", 5), manual]
    scores = {"Real": 1.0, "Manual": 1.0}
    enrichments = {t: "x" for t in scores}
    assert [t for t, _ in sort_scored(events, scores, enrichments)] == ["Real", "Manual"]


def test_batch_fingerprint_changes_with_mode(monkeypatch):
    import config
    from recommender.taste_profile_builder import _batch_fingerprint
    scored = [("A", 1.0)]
    monkeypatch.setattr(config, "USE_VIEWING_SIGNALS", False)
    off = _batch_fingerprint(scored)
    monkeypatch.setattr(config, "USE_VIEWING_SIGNALS", True)
    assert _batch_fingerprint(scored) != off
