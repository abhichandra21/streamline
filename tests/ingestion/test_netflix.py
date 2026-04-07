import csv
import io
import zipfile
from datetime import datetime, timedelta

import pytest

from recommender.ingestion.netflix import parse

_NETFLIX_FIELDS = [
    "Duration",
    "Start Time",
    "Profile Name",
    "Country",
    "Bookmark",
    "Latest Bookmark",
    "Supplemental Video Type",
    "Attributes",
    "Device Type",
    "Title",
]


def _make_netflix_csv(rows: list[dict]) -> bytes:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=_NETFLIX_FIELDS, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow({field: row.get(field, "") for field in _NETFLIX_FIELDS})
    return buf.getvalue().encode()


def _make_zip(contents: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in contents.items():
            zf.writestr(name, data)
    return buf.getvalue()


def _row(**kwargs) -> dict:
    defaults = {
        "Duration": "01:30:00",
        "Start Time": "2026-01-01 12:00:00",
        "Profile Name": "Main",
        "Country": "US",
        "Bookmark": "01:30:00",
        "Latest Bookmark": "01:30:00",
        "Supplemental Video Type": "",
        "Attributes": "",
        "Device Type": "Roku",
        "Title": "Inception",
    }
    defaults.update(kwargs)
    return defaults


def _write_export_zip(tmp_path, rows: list[dict], member_name: str = "ViewingActivity.csv") -> str:
    csv_bytes = _make_netflix_csv(rows)
    zip_bytes = _make_zip({member_name: csv_bytes})
    export_path = tmp_path / "netflix_export.zip"
    export_path.write_bytes(zip_bytes)
    return str(export_path)


def test_parse_zip_skips_trailers_and_short_watches(tmp_path):
    export_path = _write_export_zip(
        tmp_path,
        [
            _row(Title="Inception"),
            _row(Title="Trailer: Inception", **{"Supplemental Video Type": "Trailer"}),
            _row(Title="Search Party", Duration="00:04:59"),
        ],
    )

    events = parse(export_path)

    assert [event.title for event in events] == ["Inception"]


def test_parse_zip_classifies_tv_and_movies(tmp_path):
    export_path = _write_export_zip(
        tmp_path,
        [
            _row(Title="14 Peaks: Nothing Is Impossible"),
            _row(
                Title="Avatar: The Last Airbender: Book 3: Day of Black Sun: Part 1: The Invasion (Episode 1)",
                Duration="00:45:03",
                **{"Start Time": "2026-03-16 20:22:18"},
            ),
        ],
    )

    events = parse(export_path)

    movie_event = next(event for event in events if event.content_type == "movie")
    tv_event = next(event for event in events if event.content_type == "tv")
    assert movie_event.title == "14 Peaks: Nothing Is Impossible"
    assert tv_event.series_name == "Avatar: The Last Airbender"
    assert tv_event.watched_duration == timedelta(minutes=45, seconds=3)
    assert tv_event.timestamp == datetime(2026, 3, 16, 20, 22, 18)


def test_parse_zip_sets_platform_and_total_duration_none(tmp_path):
    export_path = _write_export_zip(tmp_path, [_row(Title="Inception"), _row(Title="Interstellar")])

    events = parse(export_path)

    assert all(event.platform == "netflix" for event in events)
    assert all(event.total_duration is None for event in events)


def test_parse_raises_for_non_zip_extension(tmp_path):
    export_path = tmp_path / "netflix_export.csv"
    export_path.write_text("not a zip")

    with pytest.raises(ValueError, match=r"\.zip file"):
        parse(str(export_path))


def test_parse_raises_for_missing_file(tmp_path):
    with pytest.raises(FileNotFoundError):
        parse(str(tmp_path / "missing.zip"))


def test_parse_raises_for_corrupt_zip(tmp_path):
    export_path = tmp_path / "corrupt.zip"
    export_path.write_text("not a zip archive")

    with pytest.raises(ValueError, match="Invalid zip file"):
        parse(str(export_path))


def test_parse_raises_when_csv_missing_from_zip(tmp_path):
    export_path = tmp_path / "netflix_export.zip"
    export_path.write_bytes(_make_zip({"SomeOtherFile.csv": b"wrong file"}))

    with pytest.raises(ValueError, match="ViewingActivity.csv"):
        parse(str(export_path))


def test_clean_extraction_bad_zip_after_prior_success_does_not_reuse_stale_csv(tmp_path):
    good_export = tmp_path / "good.zip"
    good_export.write_bytes(_make_zip({"ViewingActivity.csv": _make_netflix_csv([_row(Title="Inception")])}))

    events = parse(str(good_export))
    assert len(events) == 1

    bad_export = tmp_path / "bad.zip"
    bad_export.write_text("corrupt")

    with pytest.raises(ValueError, match="Invalid zip file"):
        parse(str(bad_export))
