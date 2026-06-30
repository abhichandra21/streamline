from datetime import timedelta
from recommender.ingestion.base import (
    classify_title, detect_language_hint, is_bonus_content, parse_duration,
)


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


def test_is_bonus_content_detects_known_keywords():
    assert is_bonus_content("Ferdinand Clip")
    assert is_bonus_content("The Santa Clause Clip")
    assert is_bonus_content("Cars 3 Trailer")
    assert is_bonus_content("Aladdin Featurette")
    assert is_bonus_content("Behind the Scenes: Encanto")
    assert is_bonus_content("Descendants: The Rise of Red Sing-Along")
    assert is_bonus_content("Descendants: The Rise of Red Sing Along")
    assert is_bonus_content("Deleted Scene: Frozen")
    assert is_bonus_content("Deleted Song: 'Desert Moon'")
    assert is_bonus_content("Aladdin's Video Journal: A New Fantastic Point of View")
    assert is_bonus_content("Song Breakdowns: 'Under the Sea'")
    assert is_bonus_content("Promo: Loki Season 2")
    assert is_bonus_content("Promotional: Loki")


def test_is_bonus_content_detects_non_latin_trailer_words():
    assert is_bonus_content("Raya and the Last Dragon טריילר")
    assert is_bonus_content("Laapataa Ladies ट्रेलर")


def test_is_bonus_content_detects_pipe_separated_featurette_markers():
    assert is_bonus_content("Stunts | More from Pandora's Box | Avatar: The Way of Water")
    assert is_bonus_content("Trailer | Wonder Man | Season 1")


def test_is_bonus_content_checks_across_multiple_parts():
    # Pipe markers split across separate fields (e.g. program/season) still
    # count toward the combined featurette-marker threshold.
    assert is_bonus_content("Inside | Pandora's Box", "| Avatar: The Way of Water")


def test_is_bonus_content_false_for_real_titles():
    assert not is_bonus_content("Inception")
    assert not is_bonus_content("Cars 3", "")
    assert not is_bonus_content("Inside")
    assert not is_bonus_content("Promoter")
    # Regression: a bare \b-word match on these would false-positive on real
    # titles where the bonus keyword is just the first word of a longer,
    # unrelated phrase rather than a tag.
    assert not is_bonus_content("Trailer Park Boys")
    assert not is_bonus_content("BTS: Permission to Dance on Stage")
    assert not is_bonus_content("Clipped")
    assert not is_bonus_content("Promotional content for Loki")


def test_detect_language_hint_devanagari():
    assert detect_language_hint("Don") is None  # transliterated, no script
    assert detect_language_hint("डॉन") == "hi"


def test_detect_language_hint_hebrew():
    assert detect_language_hint("טריילר") == "he"


def test_detect_language_hint_arabic():
    assert detect_language_hint("الفيلم") == "ar"


def test_detect_language_hint_japanese_kana_takes_precedence_over_kanji():
    assert detect_language_hint("おはよう") == "ja"
    assert detect_language_hint("ひらがな漢字") == "ja"


def test_detect_language_hint_korean_hangul():
    assert detect_language_hint("기생충") == "ko"


def test_detect_language_hint_chinese_without_kana():
    assert detect_language_hint("流浪地球") == "zh"


def test_detect_language_hint_none_for_latin_titles():
    assert detect_language_hint("Inception") is None
    assert detect_language_hint("") is None

