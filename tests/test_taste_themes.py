import json

import pytest

from recommender.taste_themes import (
    MAX_THEME_TAGS, MAX_THEMES, build_themes, load_ignored, load_themes, map_new_tags,
    parse_themes, tag_to_theme, theme_prompt,
)
from tests.mock_llm import make_mock_llm, make_mock_llm_sequence


def _answer(themes):
    return json.dumps({"themes": themes})


def test_parse_applies_caps_and_first_theme_wins():
    themes = [{"id": f"t{i}", "label": f"T{i}", "tags": ["shared", f"own{i}"]} for i in range(MAX_THEMES + 3)]
    themes[0]["tags"] = [f"x{i}" for i in range(MAX_THEME_TAGS + 5)] + ["shared"]
    parsed = parse_themes(_answer(themes))
    assert len(parsed) == MAX_THEMES
    assert len(parsed[0]["tags"]) == MAX_THEME_TAGS
    assert tag_to_theme(parsed)["own1"] == 1
    assert sum("shared" in t["tags"] for t in parsed) == 1


def test_parse_reads_region_language_and_family():
    parsed = parse_themes(_answer([{"id": "uk", "label": "British", "region": "gb", "tags": ["british"]},
                                   {"id": "kids", "label": "Kids", "family": True, "tags": ["kids"]}]))
    assert parsed[0]["region"] == "GB" and parsed[0]["language"] == ""
    assert parsed[1]["family"] is True


def test_prompt_lists_tags_with_counts_examples_rules_and_old_themes():
    prompt = theme_prompt({"Vera": ["british", "cosy mystery"], "Luther": ["british"]},
                          [{"id": "uk", "label": "British", "tags": ["british"]}])
    assert "british (2): Luther; Vera" in prompt
    assert "one taste" in prompt and "at most 30 tags" in prompt
    assert '"id": "uk"' in prompt


def test_build_saves_and_rejects_empty(tmp_path):
    path = tmp_path / "themes.json"
    build_themes({"A": ["x"]}, make_mock_llm(_answer([{"id": "a", "label": "A", "tags": ["x"]}])), path, [])
    assert load_themes(path)[0]["id"] == "a"
    with pytest.raises(ValueError):
        build_themes({"A": ["x"]}, make_mock_llm(_answer([])), tmp_path / "other.json", [])


def test_tags_the_theme_step_left_out_are_ignored(tmp_path):
    path = tmp_path / "themes.json"
    build_themes({"A": ["x", "based on book"]},
                 make_mock_llm(_answer([{"id": "a", "label": "A", "tags": ["x"]}])), path, [])
    assert load_ignored(path) == {"based on book"}


def test_new_tags_are_appended_to_existing_themes_only(tmp_path):
    path = tmp_path / "themes.json"
    themes = [{"id": "spy", "label": "Spies", "region": "", "language": "", "family": False, "tags": ["espionage"]}]
    client = make_mock_llm(json.dumps({"kgb": "spy", "puppets": "none", "x": "made-up"}))
    updated = map_new_tags(themes, ["kgb", "puppets", "x"], client, path)
    assert updated[0]["tags"] == ["espionage", "kgb"]
    assert load_themes(path)[0]["tags"] == ["espionage", "kgb"]
    assert load_ignored(path) == {"puppets", "x"}


def test_a_tag_the_answer_skipped_is_asked_again_not_ignored(tmp_path):
    path = tmp_path / "themes.json"
    themes = [{"id": "spy", "label": "Spies", "region": "", "language": "", "family": False, "tags": ["espionage"]}]
    map_new_tags(themes, ["kgb", "mole"], make_mock_llm(json.dumps({"kgb": "spy"})), path)
    assert load_ignored(path) == set()


def test_failed_new_tag_call_keeps_themes(tmp_path):
    themes = [{"id": "spy", "label": "Spies", "region": "", "language": "", "family": False, "tags": ["espionage"]}]
    client = make_mock_llm_sequence([RuntimeError("down")])
    assert map_new_tags(themes, ["kgb"], client, tmp_path / "t.json") == themes


def test_duplicate_theme_ids_get_a_suffix():
    parsed = parse_themes(_answer([{"id": "a", "label": "A", "tags": ["x"]}, {"id": "a", "label": "B", "tags": ["y"]}]))
    assert [t["id"] for t in parsed] == ["a", "a-2"]


def test_prompt_counts_moods_and_relationships_as_tastes():
    prompt = theme_prompt({"A": ["feel-good"]}, [])
    assert "Moods and relationships are tastes" in prompt
    assert "based on novel" in prompt and "decade" in prompt
