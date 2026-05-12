from datetime import timedelta
from recommender.ingestion.base import classify_title, parse_duration


def test_classify_tv_season():
    ct, series = classify_title(
        "How To Get To Heaven From Belfast: Season 1: Anagnorisis (Episode 8)"
    )
    assert ct == "tv"
    assert series == "How To Get To Heaven From Belfast"


def test_classify_tv_book():
    ct, series = classify_title(
        "Avatar: The Last Airbender: Book 3: Day of the Black Sun (Episode 10)"
    )
    assert ct == "tv"
    assert series == "Avatar: The Last Airbender"


def test_classify_tv_limited_series():
    ct, series = classify_title(
        "Adolescence: Limited Series: Episode 1 (Episode 1)"
    )
    assert ct == "tv"
    assert series == "Adolescence"


def test_classify_tv_year_season():
    ct, series = classify_title(
        "Peaky Blinders: The Immortal Man Podcast: 2026: Episode Title (Episode 1)"
    )
    assert ct == "tv"
    assert series == "Peaky Blinders: The Immortal Man Podcast"


def test_classify_movie():
    ct, series = classify_title("14 Peaks: Nothing Is Impossible")
    assert ct == "movie"
    assert series == "14 Peaks: Nothing Is Impossible"


def test_classify_movie_plain():
    ct, series = classify_title("365 Days")
    assert ct == "movie"
    assert series == "365 Days"


def test_parse_duration():
    assert parse_duration("01:23:45") == timedelta(hours=1, minutes=23, seconds=45)


def test_parse_duration_zero():
    assert parse_duration("00:00:00") == timedelta(0)


def test_hbo_stub_raises():
    from recommender.ingestion.hbo import parse
    try:
        parse("any_path.csv")
        assert False, "Should have raised NotImplementedError"
    except NotImplementedError as e:
        assert "hbo" in str(e).lower() or "HBO" in str(e)
