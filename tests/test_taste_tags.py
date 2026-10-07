import json
from collections import Counter

from recommender.taste_tags import (
    TAG_BATCH_SIZE, load_tags, normalize_tag, parse_tag_answer, pending_batches, save_tags,
    tag_prompt, tag_titles, vocabulary,
)
from tests.mock_llm import make_mock_llm, make_mock_llm_sequence


def test_tags_round_trip_and_missing_file(tmp_path):
    path = tmp_path / "tags.json"
    assert load_tags(path) == {}
    save_tags({"Vera": ["british", "cosy mystery"]}, path)
    assert load_tags(path) == {"Vera": ["british", "cosy mystery"]}


def test_vocabulary_counts_titles_per_tag():
    assert vocabulary({"A": ["x", "y"], "B": ["x"]}) == Counter({"x": 2, "y": 1})


def test_normalize_folds_case_space_and_near_spellings_into_the_vocabulary():
    vocab = Counter({"cosy mystery": 5, "espionage": 3})
    assert normalize_tag("  Cosy  Mystery ", vocab) == "cosy mystery"
    assert normalize_tag("cosy mysteries", vocab) == "cosy mystery"
    assert normalize_tag("slow-burn", vocab) == "slow-burn"
    assert normalize_tag("espionages", vocab) == "espionage"


def test_normalize_keeps_different_ideas_apart():
    vocab = Counter({"hindi": 40, "india": 3})
    assert normalize_tag("set in india", vocab) == "set in india"
    assert normalize_tag("indian", vocab) == "indian"
    assert normalize_tag("thriller", Counter({"thrillers": 2})) == "thrillers"



def test_prompt_shows_vocabulary_rules_and_full_enrichment():
    vocab = Counter({"british": 30, "cosy mystery": 9, **{f"t{i}": 1 for i in range(60)}})
    prompt = tag_prompt([("Vera", "# Vera\n\nA long paragraph ending in tone words.")], vocab)
    assert "Existing tags: british, cosy mystery" in prompt
    assert "at least 3" in prompt and "at most 2 new" in prompt
    assert "made in Britain" in prompt and "set or shot" in prompt and "Ted Lasso" in prompt
    assert "ending in tone words" in prompt
    assert "at least 3" not in tag_prompt([("Vera", "x")], Counter({"british": 1}))


def test_parse_keeps_known_titles_normalises_and_caps():
    vocab = Counter({"cosy mystery": 4})
    answer = json.dumps({"Vera": ["Cosy Mysteries", "british", "x", "a b c d e"] + [f"t{i}" for i in range(9)],
                         "Not asked": ["x"]})
    parsed = parse_tag_answer(answer, ["Vera"], vocab)
    assert list(parsed) == ["Vera"]
    assert parsed["Vera"][0] == "cosy mystery"
    assert "a b c d e" not in parsed["Vera"]          # over 4 words
    assert len(parsed["Vera"]) == 8
    assert list(parse_tag_answer(json.dumps({"Sr.": ["x"]}), ['"Sr."'], vocab)) == ['"Sr."']


def test_titles_differing_only_in_case_keep_their_own_tags():
    answer = json.dumps({"succession": ["a"], "Succession": ["b"]})
    assert parse_tag_answer(answer, ["succession", "Succession"], Counter()) == {"succession": ["a"], "Succession": ["b"]}


def test_parse_of_bad_json_returns_nothing():
    assert parse_tag_answer("not json", ["Vera"], Counter()) == {}


def test_pending_batches_skip_tagged_and_unenriched_titles():
    titles = [f"T{i}" for i in range(TAG_BATCH_SIZE + 2)] + ["No enrichment"]
    enrich = {t: "x" for t in titles if t != "No enrichment"}
    batches = pending_batches(titles, enrich, {"T0": ["a"]})
    assert [len(b) for b in batches] == [TAG_BATCH_SIZE, 1]
    assert all(t != "T0" for b in batches for t, _ in b)


def test_tag_titles_saves_answers_and_makes_no_call_when_nothing_is_new(tmp_path):
    path = tmp_path / "tags.json"
    client = make_mock_llm(json.dumps({"Vera": ["british", "cosy mystery", "slow-burn"]}))
    assert tag_titles(["Vera"], {"Vera": "x"}, client, path)["Vera"][0] == "british"
    tag_titles(["Vera"], {"Vera": "x"}, client, path)
    assert client.generate.call_count == 1


def test_a_failed_batch_leaves_titles_untagged_for_next_time(tmp_path):
    client = make_mock_llm_sequence([RuntimeError("down")])
    assert tag_titles(["Vera"], {"Vera": "x"}, client, tmp_path / "t.json") == {}
