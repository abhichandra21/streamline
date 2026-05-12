from datetime import datetime, timedelta
from unittest.mock import patch

import pytest

import config
from recommender.ingestion import disney
from recommender.ingestion.disney import parse


def _rows(*tuples) -> list[dict]:
    """Build a row list from (date, profile, program, season) or
    (date, service, profile, program, season) tuples."""
    out: list[dict] = []
    for t in tuples:
        if len(t) == 5:
            d, svc, p, prog, s = t
        else:
            d, p, prog, s = t
            svc = "disney"
        out.append({"date": d, "service": svc, "profile": p, "program": prog, "season": s})
    return out


@pytest.fixture
def pdf_path(tmp_path):
    path = tmp_path / "disney.pdf"
    path.write_bytes(b"%PDF-1.4 stub")
    return str(path)


def test_classifies_tv_when_season_column_holds_series_name(pdf_path, monkeypatch):
    monkeypatch.setattr(config, "DISNEY_PROFILES", [])
    rows = _rows(
        ("2026-04-08", "Abhishek", "Uptight (Oliver's Alright)", "Hannah Montana"),
    )
    with patch.object(disney, "_extract_rows", return_value=rows):
        events = parse(pdf_path)
    assert len(events) == 1
    e = events[0]
    assert e.content_type == "tv"
    assert e.series_name == "Hannah Montana"
    assert e.title == "Uptight (Oliver's Alright)"
    assert e.timestamp == datetime(2026, 4, 8)
    assert e.platform == "disney"
    assert e.profile == "Abhishek"


def test_classifies_movie_when_season_blank(pdf_path, monkeypatch):
    monkeypatch.setattr(config, "DISNEY_PROFILES", [])
    rows = _rows(("2026-04-05", "Abhishek", "Cars 3", ""))
    with patch.object(disney, "_extract_rows", return_value=rows):
        events = parse(pdf_path)
    assert [e.content_type for e in events] == ["movie"]
    assert events[0].series_name == "Cars 3"


def test_drops_trailers(pdf_path, monkeypatch):
    monkeypatch.setattr(config, "DISNEY_PROFILES", [])
    rows = _rows(
        ("2026-04-04", "Abhishek", "Cars 3 Trailer", ""),
        ("2026-04-04", "Abhishek", "Cars 3", ""),
        ("2026-01-25", "Abhishek", "Trailer | Wonder Man | Season 1", ""),
    )
    with patch.object(disney, "_extract_rows", return_value=rows):
        events = parse(pdf_path)
    assert [e.series_name for e in events] == ["Cars 3"]


def test_profile_filter_keeps_only_allowed(pdf_path, monkeypatch):
    monkeypatch.setattr(config, "DISNEY_PROFILES", ["Abhishek"])
    rows = _rows(
        ("2026-04-02", "Abhishek", "Cars", ""),
        ("2026-04-02", "Ashish", "Frozen", ""),
        ("2026-04-02", "Rhonda", "Daddy Putdown", "Bluey: Shorts Season 1"),
    )
    with patch.object(disney, "_extract_rows", return_value=rows):
        events = parse(pdf_path)
    assert {e.profile for e in events} == {"Abhishek"}
    assert {e.series_name for e in events} == {"Cars"}


def test_empty_profile_filter_keeps_all(pdf_path, monkeypatch):
    monkeypatch.setattr(config, "DISNEY_PROFILES", [])
    rows = _rows(
        ("2026-04-02", "Abhishek", "Cars", ""),
        ("2026-04-02", "Ashish", "Frozen", ""),
    )
    with patch.object(disney, "_extract_rows", return_value=rows):
        events = parse(pdf_path)
    assert {e.profile for e in events} == {"Abhishek", "Ashish"}


def test_collapses_same_day_duplicate_episodes(pdf_path, monkeypatch):
    monkeypatch.setattr(config, "DISNEY_PROFILES", [])
    rows = _rows(
        ("2025-11-11", "Rhonda", "Daddy Putdown", "Bluey: Shorts Season 1"),
        ("2025-11-11", "Rhonda", "Daddy Putdown", "Bluey: Shorts Season 1"),
        ("2025-11-11", "Rhonda", "Muffin Unboxing", "Bluey: Shorts Season 1"),
        ("2025-11-12", "Rhonda", "Daddy Putdown", "Bluey: Shorts Season 1"),
    )
    with patch.object(disney, "_extract_rows", return_value=rows):
        events = parse(pdf_path)
    # Same series same day collapses to 1 event regardless of episode title;
    # next day produces a fresh event.
    assert len(events) == 2
    assert {e.timestamp.date().isoformat() for e in events} == {
        "2025-11-11",
        "2025-11-12",
    }
    assert {e.series_name for e in events} == {"Bluey: Shorts Season 1"}


def test_synthesizes_durations_from_manual_config(pdf_path, monkeypatch):
    monkeypatch.setattr(config, "DISNEY_PROFILES", [])
    monkeypatch.setattr(config, "MANUAL_TV_DURATION_MINUTES", 45)
    monkeypatch.setattr(config, "MANUAL_MOVIE_DURATION_MINUTES", 120)
    rows = _rows(
        ("2026-04-08", "Abhishek", "Uptight", "Hannah Montana"),
        ("2026-04-05", "Abhishek", "Cars 3", ""),
    )
    with patch.object(disney, "_extract_rows", return_value=rows):
        events = parse(pdf_path)
    tv = next(e for e in events if e.content_type == "tv")
    mv = next(e for e in events if e.content_type == "movie")
    assert tv.watched_duration == timedelta(minutes=45)
    assert tv.total_duration == timedelta(minutes=45)
    assert mv.watched_duration == timedelta(minutes=120)
    assert mv.total_duration == timedelta(minutes=120)


def test_skips_rows_with_blank_program_and_season(pdf_path, monkeypatch):
    monkeypatch.setattr(config, "DISNEY_PROFILES", [])
    rows = _rows(("2026-04-08", "Abhishek", "", ""))
    with patch.object(disney, "_extract_rows", return_value=rows):
        events = parse(pdf_path)
    assert events == []


def test_drops_rows_from_other_services(pdf_path, monkeypatch):
    monkeypatch.setattr(config, "DISNEY_PROFILES", [])
    rows = _rows(
        ("2026-04-08", "disney", "Abhishek", "Cars 3", ""),
        ("2026-04-08", "espn", "Abhishek", "SportsCenter", ""),
        ("2026-04-08", "ESPN", "Abhishek", "Monday Night Football", ""),
        ("2026-04-08", "hulu", "Abhishek", "The Bear", ""),
    )
    with patch.object(disney, "_extract_rows", return_value=rows):
        events = parse(pdf_path)
    assert [e.series_name for e in events] == ["Cars 3"]


def test_tv_event_title_is_episode_distinct_across_days(pdf_path, monkeypatch):
    """Different days/episodes of a series must carry distinct titles so
    signals.compute_scores() doesn't treat them as rewatches of one episode."""
    monkeypatch.setattr(config, "DISNEY_PROFILES", [])
    rows = _rows(
        ("2026-04-08", "Abhishek", "Uptight (Oliver's Alright)", "Hannah Montana"),
        ("2026-04-07", "Abhishek", "Bye Bye Ball", "Hannah Montana"),
        ("2026-04-06", "Abhishek", "Yet Another Side of Me", "Hannah Montana"),
    )
    with patch.object(disney, "_extract_rows", return_value=rows):
        events = parse(pdf_path)
    assert {e.series_name for e in events} == {"Hannah Montana"}
    titles = [e.title for e in events]
    assert len(set(titles)) == 3, f"expected 3 distinct episode titles, got {titles}"


def test_corrupt_pdf_raises_value_error(tmp_path):
    """pdfplumber raises pdfminer/OSError on bad PDFs; the parser must
    normalize those into ValueError so the setup loader reports it as a
    provider failure instead of crashing."""
    bad = tmp_path / "corrupt.pdf"
    bad.write_bytes(b"not a real pdf")
    with pytest.raises(ValueError, match="Failed to parse Disney"):
        parse(str(bad))


def test_raises_for_missing_file(tmp_path):
    with pytest.raises(FileNotFoundError):
        parse(str(tmp_path / "missing.pdf"))


def test_raises_for_non_pdf_extension(tmp_path):
    bad = tmp_path / "export.zip"
    bad.write_bytes(b"stub")
    with pytest.raises(ValueError, match=r"\.pdf file"):
        parse(str(bad))
