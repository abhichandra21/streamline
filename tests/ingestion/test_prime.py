import csv
import io
import zipfile
from datetime import datetime, timedelta

import pytest

from recommender.ingestion.prime import parse

_PRIME_FIELDS = [
    "Material Type Description",
    "Seconds Viewed",
    "Playback Start Datetime (UTC)",
    "Title",
    "Profile Type",
]


def _make_prime_csv(rows: list[dict]) -> bytes:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=_PRIME_FIELDS, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow({field: row.get(field, "") for field in _PRIME_FIELDS})
    return buf.getvalue().encode()


def _make_zip(contents: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in contents.items():
            zf.writestr(name, data)
    return buf.getvalue()


def _row(**kwargs) -> dict:
    defaults = {
        "Material Type Description": "Feature",
        "Seconds Viewed": "2400",
        "Playback Start Datetime (UTC)": "2026-01-01T10:00:00Z",
        "Title": "Dune",
        "Profile Type": "adult",
    }
    defaults.update(kwargs)
    return defaults


def _write_export_zip(tmp_path, rows: list[dict], member_name: str = "Viewing History.csv") -> str:
    csv_bytes = _make_prime_csv(rows)
    zip_bytes = _make_zip({member_name: csv_bytes})
    export_path = tmp_path / "prime_export.zip"
    export_path.write_bytes(zip_bytes)
    return str(export_path)


def test_parse_zip_skips_non_feature_material_and_short_watches(tmp_path):
    export_path = _write_export_zip(
        tmp_path,
        [
            _row(Title="Dune"),
            _row(Title="Short Clip", **{"Material Type Description": "Trailer"}),
            _row(Title="Too Short", **{"Seconds Viewed": "1199"}),
        ],
    )

    events = parse(export_path)

    assert [event.title for event in events] == ["Dune"]


def test_parse_zip_classifies_tv_and_movies(tmp_path):
    export_path = _write_export_zip(
        tmp_path,
        [
            _row(Title="Dune"),
            _row(
                Title="Pilot - The Boys, Season 1",
                **{
                    "Seconds Viewed": "2700",
                    "Playback Start Datetime (UTC)": "2026-02-01T12:00:00Z",
                },
            ),
        ],
    )

    events = parse(export_path)

    movie_event = next(event for event in events if event.content_type == "movie")
    tv_event = next(event for event in events if event.content_type == "tv")
    assert movie_event.title == "Dune"
    assert tv_event.series_name == "The Boys"
    assert tv_event.watched_duration == timedelta(seconds=2700)
    assert tv_event.timestamp == datetime(2026, 2, 1, 12, 0, 0)


def test_parse_zip_sets_platform_and_total_duration_none(tmp_path):
    export_path = _write_export_zip(tmp_path, [_row(Title="Dune"), _row(Title="Arrival")])

    events = parse(export_path)

    assert all(event.platform == "prime" for event in events)
    assert all(event.total_duration is None for event in events)


def test_parse_raises_for_non_zip_extension(tmp_path):
    export_path = tmp_path / "prime_export.csv"
    export_path.write_text("not a zip")

    with pytest.raises(ValueError, match=r"\.zip file"):
        parse(str(export_path))


def test_parse_raises_for_missing_file(tmp_path):
    with pytest.raises(FileNotFoundError):
        parse(str(tmp_path / "missing.zip"))


def test_parse_raises_for_corrupt_zip(tmp_path):
    export_path = tmp_path / "bad.zip"
    export_path.write_text("not a zip")

    with pytest.raises(ValueError, match="Invalid zip file"):
        parse(str(export_path))


def test_parse_raises_for_missing_csv_in_zip(tmp_path):
    export_path = tmp_path / "prime_export.zip"
    export_path.write_bytes(_make_zip({"other.txt": b"no csv"}))

    with pytest.raises(ValueError, match="Viewing History.csv"):
        parse(str(export_path))


def test_clean_extraction_bad_zip_after_prior_success_does_not_reuse_stale_csv(tmp_path):
    good_export = tmp_path / "good.zip"
    good_export.write_bytes(_make_zip({"Viewing History.csv": _make_prime_csv([_row(Title="Good Film")])}))

    events = parse(str(good_export))
    assert len(events) == 1

    bad_export = tmp_path / "bad.zip"
    bad_export.write_text("corrupt")

    with pytest.raises(ValueError, match="Invalid zip file"):
        parse(str(bad_export))
