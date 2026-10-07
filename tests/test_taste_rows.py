import json

from recommender.taste_rows import (
    build_tag_profile, coverage_warnings, place_loves, region_facts, rows_needing_words, words_prompt,
)
from tests.mock_llm import make_mock_llm_sequence

THEMES = [
    {"id": "uk", "label": "British", "region": "GB", "language": "", "family": False, "tags": ["british"]},
    {"id": "hi", "label": "Hindi", "region": "", "language": "hi", "family": False, "tags": ["hindi"]},
    {"id": "crime", "label": "Crime", "region": "", "language": "", "family": False,
     "tags": ["police procedural", "serial killer"]},
    {"id": "action", "label": "Action", "region": "", "language": "", "family": False, "tags": ["car chase"]},
]


def test_region_needs_both_tmdb_and_a_region_tag():
    tags = {"Line of Duty": ["british", "police procedural", "serial killer"],
            "Baby Driver": ["car chase"]}
    facts = {"Line of Duty": {"countries": {"GB"}, "language": "en"},
             "Baby Driver": {"countries": {"US", "GB"}, "language": "en"}}
    placed, unplaced = place_loves(list(tags), tags, THEMES, facts)
    assert placed[0] == ["Line of Duty"]          # GB fact + british tag beats two crime tags
    assert placed[3] == ["Baby Driver"]           # GB money, no british tag: by vote
    assert unplaced == []


def test_a_british_tag_without_the_fact_never_reaches_the_british_row():
    tags = {"Ted Lasso": ["british", "police procedural"], "Only british": ["british"]}
    facts = {"Ted Lasso": {"countries": {"US"}, "language": "en"},
             "Only british": {"countries": {"US"}, "language": "en"}}
    placed, unplaced = place_loves(list(tags), tags, THEMES, facts)
    assert placed[0] == [] and placed[2] == ["Ted Lasso"]
    assert unplaced == ["Only british"]


def test_region_order_follows_the_theme_map_not_tag_order():
    tags = {"Indian Summers": ["hindi", "british"]}
    facts = {"Indian Summers": {"countries": {"GB"}, "language": "hi"}}
    assert place_loves(list(tags), tags, THEMES, facts)[0][0] == ["Indian Summers"]


def test_language_region_uses_original_language():
    tags = {"Panchayat": ["hindi"], "Lion": ["serial killer"]}
    facts = {"Panchayat": {"countries": {"IN"}, "language": "hi"}, "Lion": {"countries": {"IN"}, "language": "en"}}
    placed, _ = place_loves(list(tags), tags, THEMES, facts)
    assert placed[1] == ["Panchayat"] and placed[2] == ["Lion"]


def test_rare_tags_outvote_common_ones_and_ties_go_to_the_earlier_theme():
    tags = {"A": ["car chase", "police procedural"], "B": ["police procedural"], "C": ["police procedural"]}
    placed, _ = place_loves(list(tags), tags, THEMES, {})
    assert "A" in placed[3]                       # car chase (1 title) outweighs police procedural (3)
    tie = {"T": ["serial killer", "car chase"]}
    assert place_loves(["T"], tie, THEMES, {})[0][2] == ["T"]


def test_titles_with_no_mapped_tag_are_reported_not_dropped():
    placed, unplaced = place_loves(["X"], {"X": ["based on book"]}, THEMES, {})
    assert unplaced == ["X"] and all(not v for v in placed.values())
    assert place_loves(["Untagged"], {}, THEMES, {})[1] == ["Untagged"]


def test_coverage_warnings():
    loves = [f"L{i}" for i in range(10)]
    tags = {t: ["serial killer"] for t in loves}
    placed, unplaced = place_loves(loves, tags, THEMES, {})
    warnings = coverage_warnings(loves, unplaced, placed)
    assert any("third" in w for w in warnings)
    tags = {t: ["based on book"] for t in loves}
    placed, unplaced = place_loves(loves, tags, THEMES, {})
    assert any("no theme" in w for w in coverage_warnings(loves, unplaced, placed))


def test_region_facts_for_films_shows_and_collections(tmp_path):
    (tmp_path / "movie").mkdir()
    (tmp_path / "tv").mkdir()
    (tmp_path / "movie" / "1.json").write_text(json.dumps({
        "origin_country": ["US"], "production_countries": [{"iso_3166_1": "GB"}], "original_language": "en"}))
    (tmp_path / "tv" / "2.json").write_text(json.dumps({
        "origin_country": ["US"], "networks": [{"name": "BBC One", "origin_country": "GB"}],
        "production_countries": [{"iso_3166_1": "US"}], "original_language": "en"}))
    entries = [{"title": "Film", "content_type": "movie", "tmdb_id": 1},
               {"title": "Show", "content_type": "tv", "tmdb_id": 2}, {"title": "No id"}]
    facts = region_facts(entries, tmp_path, {"Saga Collection (2 films)": ["Film", "Missing"]})
    assert facts["Film"] == {"countries": {"US"}, "language": "en"}     # co-producers don't count
    assert facts["Show"] == {"countries": {"GB", "US"}, "language": "en"}
    assert facts["Saga Collection (2 films)"] == facts["Film"]
    assert "No id" not in facts


def test_words_are_kept_while_members_mostly_overlap():
    from recommender.taste_rows import WORDS_VERSION
    saved = {"uk": {"members": [f"T{i}" for i in range(10)], "name": "Cosy British mysteries", "version": WORDS_VERSION}}
    same = [{"id": "uk", "members": [f"T{i}" for i in range(10)] + ["New"]}]
    moved = [{"id": "uk", "members": ["A", "B", "C"]}]
    assert rows_needing_words(same, saved) == []
    assert rows_needing_words(moved, saved) == moved
    assert rows_needing_words([{"id": "new", "members": ["A"]}], saved)[0]["id"] == "new"


def test_words_prompt_has_the_voice_rules_and_members():
    prompt = words_prompt([{"id": "uk", "label": "British", "members": ["Vera", "Luther"]}])
    assert "Cosy British mysteries" in prompt and "Not a joke" in prompt
    assert "second person" in prompt and "Vera, Luther" in prompt


def _setup_paths(tmp_path, monkeypatch):
    import config
    for name in ("TASTE_TAGS_PATH", "TASTE_THEMES_PATH", "TASTE_WORDS_PATH", "TASTE_PLACEMENTS_PATH"):
        monkeypatch.setattr(config, name, str(tmp_path / f"{name}.json"))
    monkeypatch.setattr(config, "CACHE_DIR", str(tmp_path / "tmdb"))


def test_build_tag_profile_end_to_end_then_no_calls_on_rerun(tmp_path, monkeypatch):
    _setup_paths(tmp_path, monkeypatch)
    loves = {"Vera": 2.0, "Luther": 2.0, "Kids Film": 2.0}
    enrich = {t: "x" for t in loves}
    answers = [
        json.dumps({"Vera": ["cosy mystery", "based on book"], "Luther": ["cosy mystery"], "Kids Film": ["kids"]}),
        json.dumps({"themes": [{"id": "cosy", "label": "Cosy mysteries", "tags": ["cosy mystery"]},
                               {"id": "kids", "label": "Kids", "family": True, "tags": ["kids"]}]}),
        # One usable tag each, so the AI places them once.
        json.dumps({"Vera": "cosy", "Luther": "cosy", "Kids Film": "kids"}),
        json.dumps({"cosy": {"name": "Cosy British mysteries", "description": "You love a vicar.",
                             "positive_traits": ["cosy"], "mood_states": ["unwind"]},
                    "kids": {"name": "Family movie night", "description": "Saturday mornings."}}),
    ]
    client = make_mock_llm_sequence(answers)
    profile, warnings = build_tag_profile(loves, enrich, [], {}, client, ["Bad Show"])
    assert [c["name"] for c in profile["clusters"]] == ["Cosy British mysteries", "Family movie night"]
    assert profile["clusters"][0]["members"] == ["Luther", "Vera"]
    assert profile["clusters"][1]["co_viewing"] == "family"
    assert "Bad Show" in profile["negative_preferences"][0]["label"]
    rerun = make_mock_llm_sequence([])
    again, _ = build_tag_profile(loves, enrich, [], {}, rerun, ["Bad Show"])
    assert again == profile
    assert rerun.generate.call_count == 0          # "based on book" was ignored, not re-asked


def test_failed_words_keep_the_rows_with_their_labels(tmp_path, monkeypatch):
    _setup_paths(tmp_path, monkeypatch)
    answers = [json.dumps({"Vera": ["cosy mystery"]}),
               json.dumps({"themes": [{"id": "cosy", "label": "Cosy mysteries", "tags": ["cosy mystery"]}]}),
               json.dumps({"Vera": "cosy"}),
               "not json"]
    profile, _ = build_tag_profile({"Vera": 2.0}, {"Vera": "x"}, [], {}, make_mock_llm_sequence(answers), None)
    assert profile["clusters"][0]["label"] == "Cosy mysteries" and profile["clusters"][0]["name"] == ""


def test_streamer_made_british_show_uses_its_origin_country(tmp_path):
    (tmp_path / "tv").mkdir()
    (tmp_path / "tv" / "3.json").write_text(json.dumps({
        "origin_country": ["GB"], "networks": [{"name": "Netflix", "origin_country": "US"}],
        "original_language": "en"}))
    facts = region_facts([{"title": "Black Doves", "content_type": "tv", "tmdb_id": 3}], tmp_path, {})
    assert "GB" in facts["Black Doves"]["countries"]


def test_unplaced_region_only_titles_count_in_the_warning():
    loves = [f"L{i}" for i in range(5)]
    tags = {t: ["british"] for t in loves}
    facts = {t: {"countries": {"US"}, "language": "en"} for t in loves}
    placed, unplaced = place_loves(loves, tags, THEMES, facts)
    assert any("5 of 5 loves fit no theme" in w for w in coverage_warnings(loves, unplaced, placed))


def test_thin_evidence_titles_take_the_saved_ai_placement():
    tags = {"Game of Thrones": ["fantasy adventure"], "Strong": ["police procedural", "serial killer"]}
    placed, unplaced = place_loves(list(tags), tags, THEMES, {}, overrides={"Game of Thrones": 3})
    assert placed[3] == ["Game of Thrones"] and placed[2] == ["Strong"]


def test_an_ai_placement_cannot_break_the_region_rule():
    tags = {"Ted Lasso": ["sports comedy"]}
    facts = {"Ted Lasso": {"countries": {"US"}, "language": "en"}}
    placed, unplaced = place_loves(list(tags), tags, THEMES, facts, overrides={"Ted Lasso": 0})
    assert placed[0] == [] and unplaced == ["Ted Lasso"]


def test_doubtful_titles_are_those_with_under_two_usable_tags():
    from recommender.taste_rows import doubtful_titles
    tags = {"A": ["car chase"], "B": ["car chase", "serial killer"], "C": ["based on book"], "D": ["british"]}
    facts = {"D": {"countries": {"US"}, "language": "en"}}
    assert doubtful_titles(list(tags), tags, THEMES, facts) == ["A", "C", "D"]


def test_ai_placements_are_saved_and_never_asked_twice(tmp_path, monkeypatch):
    from recommender.taste_rows import ai_placements
    _setup_paths(tmp_path, monkeypatch)
    client = make_mock_llm_sequence([json.dumps({"A": "action", "C": "none", "Z": "crime"})])
    tags = {"A": ["car chase"], "C": ["based on book"]}
    assert ai_placements(["A", "C"], tags, THEMES, client) == {"A": 3, "C": None}
    rerun = make_mock_llm_sequence([])
    assert ai_placements(["A", "C"], tags, THEMES, rerun) == {"A": 3, "C": None}
    assert rerun.generate.call_count == 0


def test_words_rewrite_once_when_the_words_version_changes():
    from recommender.taste_rows import WORDS_VERSION
    row = [{"id": "uk", "members": ["A"]}]
    assert rows_needing_words(row, {"uk": {"members": ["A"], "version": WORDS_VERSION - 1}}) == row
    assert rows_needing_words(row, {"uk": {"members": ["A"], "version": WORDS_VERSION}}) == []


def test_words_prompt_spreads_members_and_shows_the_row_tags():
    members = [f"T{i:03d}" for i in range(100)]
    prompt = words_prompt([{"id": "uk", "label": "British", "members": members,
                            "top_tags": ["british", "police procedural"]}])
    assert "T000" in prompt and "T099" in prompt and "T001" not in prompt
    assert "british, police procedural" in prompt


def test_an_ai_none_keeps_the_title_out_instead_of_a_stray_tag():
    tags = {"Room": ["car chase"]}
    placed, unplaced = place_loves(list(tags), tags, THEMES, {}, overrides={"Room": None})
    assert unplaced == ["Room"] and placed[3] == []
