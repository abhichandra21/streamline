"""Tests for Apple TV ingestion (tests/TEST_PLAN.md section 1)."""
import csv
import io
import zipfile
from datetime import datetime, timedelta

import pytest

from recommender.ingestion.apple_tv import _build_title, _classify, parse

_APPLE_TV_FIELDS = [
    "Action Type",
    "Content Sub-Type",
    "Feature Play Duration",
    "UTC Start Time",
    "UTC End Time",
    "Content Episode Name",
    "Content Title",
    "Content Season Name",
    "Episode Number",
    "Content Length MS",
]


def _make_csv(rows: list[dict]) -> bytes:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=_APPLE_TV_FIELDS, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow({f: row.get(f, "") for f in _APPLE_TV_FIELDS})
    return buf.getvalue().encode()


def _make_zip(contents: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in contents.items():
            zf.writestr(name, data)
    return buf.getvalue()


def _stop_row(**kwargs) -> dict:
    defaults = {
        "Action Type": "stop",
        "Content Sub-Type": "",
        "Feature Play Duration": "600000",
        "UTC Start Time": "2026-01-01T10:00:00",
        "UTC End Time": "",
        "Content Episode Name": "",
        "Content Title": "The Movie",
        "Content Season Name": "",
        "Episode Number": "",
        "Content Length MS": "0",
    }
    defaults.update(kwargs)
    return defaults


def _simple_zip(tmp_path, rows: list[dict]) -> str:
    """Write a flat zip with Video Play Activity.csv and return its path."""
    csv_bytes = _make_csv(rows)
    zip_bytes = _make_zip({"Video Play Activity.csv": csv_bytes})
    p = tmp_path / "export.zip"
    p.write_bytes(zip_bytes)
    return str(p)


# ── 1a: Nested zip extraction ──────────────────────────────────────────────

def test_nested_zip_extraction(tmp_path):
    """Outer zip containing Apple_Media_Services.zip containing the CSV."""
    csv_bytes = _make_csv([
        _stop_row(**{
            "Content Episode Name": "Breaking Bad",
            "Content Title": "Pilot",
            "Content Season Name": "Season 1",
            "Episode Number": "1.0",
        })
    ])
    inner_zip = _make_zip({"Stores Activity/Other Activity/Video Play Activity.csv": csv_bytes})
    outer_zip = _make_zip({"Apple_Media_Services.zip": inner_zip})
    zip_path = tmp_path / "Apple_Media_Services_Information_Part_1_of_2.zip"
    zip_path.write_bytes(outer_zip)

    events = parse(str(zip_path))
    assert len(events) == 1
    assert events[0].platform == "apple_tv"
    assert events[0].series_name == "Breaking Bad"


# ── 1b: Zip validation — rejects non-.zip files ────────────────────────────

def test_parse_rejects_non_zip_extension(tmp_path):
    csv_file = tmp_path / "export.csv"
    csv_file.write_text("not a zip")
    with pytest.raises(ValueError, match=r"\.zip"):
        parse(str(csv_file))


# ── 1c: Zip validation — rejects missing files ─────────────────────────────

def test_parse_raises_file_not_found(tmp_path):
    with pytest.raises(FileNotFoundError):
        parse(str(tmp_path / "nonexistent.zip"))


# ── 1e: CSV missing inside zip ─────────────────────────────────────────────

def test_parse_raises_if_no_csv_in_zip(tmp_path):
    zip_bytes = _make_zip({"other_file.txt": b"no csv here"})
    zip_path = tmp_path / "export.zip"
    zip_path.write_bytes(zip_bytes)
    with pytest.raises(ValueError, match="Video Play Activity.csv"):
        parse(str(zip_path))


# ── 1f: Classification — TV episodes vs movies ─────────────────────────────

def test_classify_tv_episode_with_season():
    content_type, series_name = _classify({
        "Content Episode Name": "Breaking Bad",
        "Content Season Name": "Season 1",
        "Episode Number": "1.0",
        "Content Title": "Pilot",
    })
    assert content_type == "tv"
    assert series_name == "Breaking Bad"


def test_classify_tv_episode_with_episode_number_only():
    content_type, series_name = _classify({
        "Content Episode Name": "Severance",
        "Content Season Name": "",
        "Episode Number": "2.0",
        "Content Title": "Half Loop",
    })
    assert content_type == "tv"
    assert series_name == "Severance"


def test_classify_movie_no_show_name():
    content_type, series_name = _classify({
        "Content Episode Name": "",
        "Content Season Name": "",
        "Episode Number": "",
        "Content Title": "Inception",
    })
    assert content_type == "movie"
    assert series_name == "Inception"


def test_classify_standalone_no_season_or_episode():
    """Show name present but no season/episode -> treated as movie."""
    content_type, series_name = _classify({
        "Content Episode Name": "Ted Lasso Special",
        "Content Season Name": "",
        "Episode Number": "0",
        "Content Title": "Behind the Scenes",
    })
    assert content_type == "movie"
    assert series_name == "Ted Lasso Special"


# ── 1g: Title building ─────────────────────────────────────────────────────

def test_build_title_movie():
    title = _build_title(
        {"Content Season Name": "", "Content Title": "Inception", "Episode Number": ""},
        "movie",
        "Inception",
    )
    assert title == "Inception"


def test_build_title_tv_with_season_and_episode():
    title = _build_title(
        {"Content Season Name": "Season 1", "Content Title": "Pilot", "Episode Number": "1.0"},
        "tv",
        "Breaking Bad",
    )
    assert title == "Breaking Bad: Season 1: E1: Pilot"


def test_build_title_tv_episode_title_same_as_show_is_omitted():
    title = _build_title(
        {"Content Season Name": "Season 1", "Content Title": "Breaking Bad", "Episode Number": "1.0"},
        "tv",
        "Breaking Bad",
    )
    assert title == "Breaking Bad: Season 1: E1"


def test_build_title_tv_no_season_no_episode():
    title = _build_title(
        {"Content Season Name": "", "Content Title": "Filler", "Episode Number": ""},
        "tv",
        "Some Show",
    )
    assert title == "Some Show: Filler"


# ── 1h: Dedup — keeps highest duration ─────────────────────────────────────

def test_dedup_keeps_highest_duration_for_same_event(tmp_path):
    rows = [
        _stop_row(**{
            "Feature Play Duration": "600000",
            "UTC Start Time": "2026-01-01T10:00:00",
            "Content Episode Name": "Breaking Bad",
            "Content Title": "Pilot",
            "Content Season Name": "Season 1",
            "Episode Number": "1.0",
        }),
        _stop_row(**{
            "Feature Play Duration": "900000",
            "UTC Start Time": "2026-01-01T10:00:00",
            "Content Episode Name": "Breaking Bad",
            "Content Title": "Pilot",
            "Content Season Name": "Season 1",
            "Episode Number": "1.0",
        }),
    ]
    events = parse(_simple_zip(tmp_path, rows))
    assert len(events) == 1
    assert events[0].watched_duration == timedelta(milliseconds=900000)


def test_dedup_keeps_distinct_events_at_different_timestamps(tmp_path):
    rows = [
        _stop_row(**{
            "Feature Play Duration": "600000",
            "UTC Start Time": "2026-01-01T10:00:00",
            "Content Episode Name": "Breaking Bad",
            "Content Title": "Pilot",
        }),
        _stop_row(**{
            "Feature Play Duration": "600000",
            "UTC Start Time": "2026-01-02T10:00:00",
            "Content Episode Name": "Breaking Bad",
            "Content Title": "Pilot",
        }),
    ]
    events = parse(_simple_zip(tmp_path, rows))
    assert len(events) == 2


# ── 1i: Filtering ──────────────────────────────────────────────────────────

def test_skips_non_stop_action_type(tmp_path):
    rows = [_stop_row(**{"Action Type": "play"})]
    assert parse(_simple_zip(tmp_path, rows)) == []


def test_skips_featured_promo_subtype(tmp_path):
    rows = [_stop_row(**{"Content Sub-Type": "FeaturedPromo"})]
    assert parse(_simple_zip(tmp_path, rows)) == []


def test_skips_preview_subtype(tmp_path):
    rows = [_stop_row(**{"Content Sub-Type": "Preview"})]
    assert parse(_simple_zip(tmp_path, rows)) == []


def test_sets_language_hint_from_series_name(tmp_path):
    rows = [_stop_row(**{"Content Title": "डॉन"})]
    events = parse(_simple_zip(tmp_path, rows))
    assert len(events) == 1
    assert events[0].language_hint == "hi"


def test_skips_bonus_content_by_title_text(tmp_path):
    rows = [
        _stop_row(**{"Content Title": "Aladdin's Video Journal: A New Fantastic Point of View"}),
        _stop_row(**{
            "Content Episode Name": "Ted Lasso",
            "Content Title": "Behind the Scenes",
        }),
        _stop_row(**{"Content Title": "The Movie"}),
    ]
    events = parse(_simple_zip(tmp_path, rows))
    assert [e.title for e in events] == ["The Movie"]


def test_skips_watch_under_five_minutes(tmp_path):
    rows = [_stop_row(**{"Feature Play Duration": "240000"})]  # 4 minutes
    assert parse(_simple_zip(tmp_path, rows)) == []


def test_includes_watch_at_exactly_five_minutes(tmp_path):
    rows = [_stop_row(**{"Feature Play Duration": "300000"})]  # exactly 5 min
    events = parse(_simple_zip(tmp_path, rows))
    assert len(events) == 1


# ── 1j: Timestamp parsing ──────────────────────────────────────────────────

def test_timestamp_uses_utc_start_time(tmp_path):
    rows = [_stop_row(**{
        "UTC Start Time": "2026-03-15T08:30:00",
        "UTC End Time": "2026-03-15T08:45:00",
    })]
    events = parse(_simple_zip(tmp_path, rows))
    assert len(events) == 1
    assert events[0].timestamp == datetime(2026, 3, 15, 8, 30, 0)


def test_timestamp_falls_back_to_utc_end_time_when_start_missing(tmp_path):
    rows = [_stop_row(**{
        "UTC Start Time": "",
        "UTC End Time": "2026-03-15T09:00:00",
    })]
    events = parse(_simple_zip(tmp_path, rows))
    assert len(events) == 1
    assert events[0].timestamp == datetime(2026, 3, 15, 9, 0, 0)


def test_skips_row_with_no_timestamp(tmp_path):
    rows = [_stop_row(**{"UTC Start Time": "", "UTC End Time": ""})]
    assert parse(_simple_zip(tmp_path, rows)) == []
