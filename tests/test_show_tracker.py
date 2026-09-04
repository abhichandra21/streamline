import json
from datetime import date, datetime, timezone

from recommender.tmdb_client import TmdbRateLimitError

from recommender.show_tracker import (
    build_sections,
    last_refresh_at,
    load_snapshots,
    merge_archive_entries,
    refresh_is_due,
    refresh_release_cache,
)


def test_newer_regular_season_inside_lookback_is_suggested():
    archive = [{
        "tmdb_id": 95480,
        "title": "Slow Horses",
        "content_type": "tv",
        "last_watched": "2024-01-15T12:00:00+00:00",
    }]
    snapshots = {
        95480: {
            "tmdb_id": 95480,
            "title": "Slow Horses",
            "status": "Returning Series",
            "seasons": [
                {"season_number": 0, "air_date": "2024-12-01", "episodes": []},
                {"season_number": 5, "air_date": "2026-08-01", "episodes": []},
            ],
        },
    }

    sections = build_sections(
        archive,
        tracking_rows=[],
        snapshots=snapshots,
        today=date(2026, 9, 3),
        lookback_days=730,
    )

    assert sections["ready_now"] == []
    assert sections["coming_soon"] == []
    assert sections["might_be_back"] == [{
        "tmdb_id": 95480,
        "title": "Slow Horses",
        "season_number": 5,
        "air_date": "2026-08-01",
        "poster_path": None,
        "reason": "A new season started",
    }]


def test_followed_show_reports_all_aired_episodes_from_returning_season():
    archive = [{
        "tmdb_id": 95480,
        "title": "Slow Horses",
        "content_type": "tv",
        "last_watched": "2024-01-15T12:00:00+00:00",
    }]
    tracking = [{
        "tmdb_id": 95480,
        "title": "Slow Horses",
        "state": "following",
        "tracking_from_season": 5,
        "caught_up_season": None,
        "caught_up_episode": None,
    }]
    snapshots = {
        95480: {
            "tmdb_id": 95480,
            "title": "Slow Horses",
            "status": "Returning Series",
            "poster_path": "/slow.jpg",
            "seasons": [{
                "season_number": 5,
                "air_date": "2026-08-01",
                "episodes": [
                    {"episode_number": 1, "air_date": "2026-08-01"},
                    {"episode_number": 2, "air_date": "2026-08-08"},
                    {"episode_number": 3, "air_date": "2026-08-15"},
                    {"episode_number": 4, "air_date": "2026-09-05"},
                ],
            }],
        },
    }

    sections = build_sections(
        archive,
        tracking_rows=tracking,
        snapshots=snapshots,
        today=date(2026, 9, 3),
        lookback_days=730,
    )

    assert sections["ready_now"] == [{
        "tmdb_id": 95480,
        "title": "Slow Horses",
        "season_number": 5,
        "available_episode_count": 3,
        "latest_aired_episode": 3,
        "next_air_date": "2026-09-05",
        "next_episode_number": 4,
        "poster_path": "/slow.jpg",
    }]


def test_caught_up_show_moves_to_coming_soon_until_next_episode_airs():
    archive = [{
        "tmdb_id": 95480,
        "title": "Slow Horses",
        "content_type": "tv",
        "last_watched": "2024-01-15T12:00:00+00:00",
    }]
    tracking = [{
        "tmdb_id": 95480,
        "title": "Slow Horses",
        "state": "following",
        "tracking_from_season": 5,
        "caught_up_season": 5,
        "caught_up_episode": 3,
    }]
    snapshots = {
        95480: {
            "tmdb_id": 95480,
            "title": "Slow Horses",
            "status": "Returning Series",
            "poster_path": "/slow.jpg",
            "seasons": [{
                "season_number": 5,
                "air_date": "2026-08-01",
                "episodes": [
                    {"episode_number": 1, "air_date": "2026-08-01"},
                    {"episode_number": 2, "air_date": "2026-08-08"},
                    {"episode_number": 3, "air_date": "2026-08-15"},
                    {"episode_number": 4, "air_date": "2026-09-05"},
                ],
            }],
        },
    }

    sections = build_sections(
        archive,
        tracking_rows=tracking,
        snapshots=snapshots,
        today=date(2026, 9, 3),
        lookback_days=730,
    )

    assert sections["ready_now"] == []
    assert sections["coming_soon"] == [{
        "tmdb_id": 95480,
        "title": "Slow Horses",
        "season_number": 5,
        "next_air_date": "2026-09-05",
        "next_episode_number": 4,
        "poster_path": "/slow.jpg",
    }]


def test_followed_returning_show_without_dates_waits_for_announcement():
    archive = [{
        "tmdb_id": 95396,
        "title": "Severance",
        "content_type": "tv",
        "last_watched": "2025-03-21T12:00:00+00:00",
    }]
    tracking = [{
        "tmdb_id": 95396,
        "title": "Severance",
        "state": "following",
        "tracking_from_season": 3,
        "caught_up_season": 2,
        "caught_up_episode": 10,
    }]
    snapshots = {
        95396: {
            "tmdb_id": 95396,
            "title": "Severance",
            "status": "Returning Series",
            "poster_path": "/severance.jpg",
            "seasons": [{"season_number": 3, "air_date": None, "episodes": []}],
        },
    }

    sections = build_sections(
        archive,
        tracking_rows=tracking,
        snapshots=snapshots,
        today=date(2026, 9, 3),
        lookback_days=730,
    )

    assert sections["coming_soon"] == [{
        "tmdb_id": 95396,
        "title": "Severance",
        "season_number": 3,
        "next_air_date": None,
        "next_episode_number": None,
        "poster_path": "/severance.jpg",
    }]


def test_followed_show_uses_announced_next_season_date_without_episode_rows():
    archive = [{
        "tmdb_id": 95396,
        "title": "Severance",
        "content_type": "tv",
        "last_watched": "2025-03-21T12:00:00+00:00",
    }]
    tracking = [{
        "tmdb_id": 95396,
        "title": "Severance",
        "state": "following",
        "tracking_from_season": 2,
        "caught_up_season": 2,
        "caught_up_episode": 10,
    }]
    snapshots = {
        95396: {
            "tmdb_id": 95396,
            "title": "Severance",
            "status": "Returning Series",
            "poster_path": "/severance.jpg",
            "seasons": [
                {"season_number": 2, "air_date": "2025-01-17", "episodes": []},
                {"season_number": 3, "air_date": "2026-10-12", "episodes": []},
            ],
        },
    }

    sections = build_sections(
        archive,
        tracking_rows=tracking,
        snapshots=snapshots,
        today=date(2026, 9, 3),
        lookback_days=730,
    )

    assert sections["coming_soon"] == [{
        "tmdb_id": 95396,
        "title": "Severance",
        "season_number": 3,
        "next_air_date": "2026-10-12",
        "next_episode_number": None,
        "poster_path": "/severance.jpg",
    }]


def test_caught_up_show_without_a_release_signal_moves_to_quiet_management():
    archive = [{
        "tmdb_id": 100,
        "title": "Limited Show",
        "content_type": "tv",
        "last_watched": "2026-01-01",
    }]
    tracking = [{
        "tmdb_id": 100,
        "title": "Limited Show",
        "state": "following",
        "tracking_from_season": 1,
        "caught_up_season": 1,
        "caught_up_episode": 6,
    }]
    snapshots = {
        100: {
            "tmdb_id": 100,
            "title": "Limited Show",
            "status": None,
            "poster_path": None,
            "seasons": [{"season_number": 1, "air_date": "2026-01-01", "episodes": []}],
        },
    }

    sections = build_sections(
        archive,
        tracking_rows=tracking,
        snapshots=snapshots,
        today=date(2026, 9, 3),
        lookback_days=730,
    )

    assert sections["caught_up"] == [{
        "tmdb_id": 100,
        "title": "Limited Show",
        "poster_path": None,
    }]


def test_followed_ended_show_moves_to_finished_management():
    archive = [{
        "tmdb_id": 1399,
        "title": "Game of Thrones",
        "content_type": "tv",
        "last_watched": "2019-05-19T12:00:00+00:00",
    }]
    tracking = [{
        "tmdb_id": 1399,
        "title": "Game of Thrones",
        "state": "following",
        "tracking_from_season": 8,
        "caught_up_season": 8,
        "caught_up_episode": 6,
    }]
    snapshots = {
        1399: {
            "tmdb_id": 1399,
            "title": "Game of Thrones",
            "status": "Ended",
            "poster_path": "/got.jpg",
            "seasons": [],
        },
    }

    sections = build_sections(
        archive,
        tracking_rows=tracking,
        snapshots=snapshots,
        today=date(2026, 9, 3),
        lookback_days=730,
    )

    assert sections["finished"] == [{
        "tmdb_id": 1399,
        "title": "Game of Thrones",
        "poster_path": "/got.jpg",
    }]


def test_future_regular_season_is_suggested_before_it_starts():
    archive = [{
        "tmdb_id": 95396,
        "title": "Severance",
        "content_type": "tv",
        "last_watched": "2025-03-21T12:00:00+00:00",
    }]
    snapshots = {
        95396: {
            "tmdb_id": 95396,
            "title": "Severance",
            "status": "Returning Series",
            "poster_path": "/severance.jpg",
            "seasons": [
                {"season_number": 2, "air_date": "2025-01-17", "episodes": []},
                {"season_number": 3, "air_date": "2026-10-12", "episodes": []},
            ],
        },
    }

    sections = build_sections(
        archive,
        tracking_rows=[],
        snapshots=snapshots,
        today=date(2026, 9, 3),
        lookback_days=730,
    )

    assert sections["might_be_back"] == [{
        "tmdb_id": 95396,
        "title": "Severance",
        "season_number": 3,
        "air_date": "2026-10-12",
        "poster_path": "/severance.jpg",
        "reason": "A new season was announced",
    }]


def test_newly_observed_undated_season_is_suggested():
    archive = [{
        "tmdb_id": 95396,
        "title": "Severance",
        "content_type": "tv",
        "last_watched": "2025-03-21T12:00:00+00:00",
    }]
    snapshots = {
        95396: {
            "tmdb_id": 95396,
            "title": "Severance",
            "status": "Returning Series",
            "poster_path": "/severance.jpg",
            "seasons": [{
                "season_number": 3,
                "air_date": None,
                "discovered_at": "2026-08-20",
                "episodes": [],
            }],
        },
    }

    sections = build_sections(
        archive,
        tracking_rows=[],
        snapshots=snapshots,
        today=date(2026, 9, 3),
        lookback_days=730,
    )

    assert sections["might_be_back"] == [{
        "tmdb_id": 95396,
        "title": "Severance",
        "season_number": 3,
        "air_date": None,
        "poster_path": "/severance.jpg",
        "reason": "A new season was announced",
    }]


def test_ignored_show_is_only_available_for_manual_refollow():
    archive = [{
        "tmdb_id": 95396,
        "title": "Severance",
        "content_type": "tv",
        "last_watched": "2025-03-21T12:00:00+00:00",
    }]
    tracking = [{
        "tmdb_id": 95396,
        "title": "Severance",
        "state": "ignored",
        "tracking_from_season": 3,
        "caught_up_season": None,
        "caught_up_episode": None,
    }]

    sections = build_sections(
        archive,
        tracking_rows=tracking,
        snapshots={},
        today=date(2026, 9, 3),
        lookback_days=730,
    )

    assert sections["might_be_back"] == []
    assert sections["ignored"] == [{
        "tmdb_id": 95396,
        "title": "Severance",
        "season_number": 3,
        "poster_path": None,
    }]


def test_upcoming_episode_can_surface_a_show_whose_season_already_started():
    archive = [{
        "tmdb_id": 100,
        "title": "Weekly Show",
        "content_type": "tv",
        "last_watched": "2026-08-20T12:00:00+00:00",
    }]
    snapshots = {
        100: {
            "tmdb_id": 100,
            "title": "Weekly Show",
            "status": "Returning Series",
            "poster_path": None,
            "next_episode_to_air": {
                "season_number": 2,
                "episode_number": 5,
                "air_date": "2026-09-10",
            },
            "seasons": [{
                "season_number": 2,
                "air_date": "2026-08-01",
                "episodes": [],
            }],
        },
    }

    sections = build_sections(
        archive,
        tracking_rows=[],
        snapshots=snapshots,
        today=date(2026, 9, 3),
        lookback_days=730,
    )

    assert sections["might_be_back"] == [{
        "tmdb_id": 100,
        "title": "Weekly Show",
        "season_number": 2,
        "air_date": "2026-09-10",
        "poster_path": None,
        "reason": "A new episode is coming",
    }]


def test_archive_merge_uses_latest_watch_and_keeps_distinct_tmdb_ids():
    watch_index = [{
        "tmdb_id": 2316,
        "title": "The Office",
        "content_type": "tv",
        "last_watched": "2024-01-01T00:00:00+00:00",
    }]
    manual = [
        {
            "tmdb_id": 2316,
            "title": "The Office",
            "content_type": "tv",
            "watched_at": "2025-01-01T00:00:00+00:00",
        },
        {
            "tmdb_id": 2996,
            "title": "The Office",
            "content_type": "tv",
            "watched_at": "2023-01-01T00:00:00+00:00",
        },
    ]

    merged = merge_archive_entries(watch_index, manual)

    assert [(row["tmdb_id"], row["last_watched"]) for row in merged] == [
        (2316, "2025-01-01T00:00:00+00:00"),
        (2996, "2023-01-01T00:00:00+00:00"),
    ]


class _ReleaseClient:
    def __init__(self, series):
        self.series = series
        self.calls = []

    def fetch_tv_series_details(self, tmdb_id):
        self.calls.append(("series", tmdb_id))
        return self.series[tmdb_id]

    def fetch_tv_season_details(self, tmdb_id, season_number):
        self.calls.append(("season", tmdb_id, season_number))
        return {
            "season_number": season_number,
            "episodes": [{"episode_number": 1, "air_date": "2026-08-01"}],
        }


def _series(tmdb_id, title, season_number=2):
    return {
        "id": tmdb_id,
        "name": title,
        "status": "Returning Series",
        "poster_path": f"/{tmdb_id}.jpg",
        "seasons": [{"season_number": season_number, "air_date": "2026-08-01"}],
    }


def test_full_refresh_scans_archived_tv_but_skips_ignored_and_movies(tmp_path):
    archive = [
        {"tmdb_id": 1, "title": "One", "content_type": "tv", "last_watched": "2024-01-01"},
        {"tmdb_id": 2, "title": "Two", "content_type": "tv", "last_watched": "2024-01-01"},
        {"tmdb_id": 3, "title": "Movie", "content_type": "movie", "last_watched": "2024-01-01"},
    ]
    tracking = [{"tmdb_id": 2, "state": "ignored"}]
    client = _ReleaseClient({1: _series(1, "One")})

    result = refresh_release_cache(
        archive,
        tracking,
        client,
        tmp_path,
        now=datetime(2026, 9, 3, tzinfo=timezone.utc),
        sleep=lambda _seconds: None,
    )

    assert client.calls == [("series", 1), ("season", 1, 2)]
    assert result == {"refreshed": 1, "failed": 0, "aborted": False, "full_scan": True}
    assert set(load_snapshots(tmp_path)) == {1}
    assert refresh_is_due(archive, tracking, tmp_path, datetime(2026, 9, 3, 1, tzinfo=timezone.utc)) is False


def test_daily_refresh_only_updates_stale_followed_show_when_full_scan_is_fresh(tmp_path):
    archive = [
        {"tmdb_id": 1, "title": "One", "content_type": "tv", "last_watched": "2024-01-01"},
        {"tmdb_id": 2, "title": "Two", "content_type": "tv", "last_watched": "2024-01-01"},
    ]
    tracking = [{
        "tmdb_id": 1,
        "title": "One",
        "state": "following",
        "tracking_from_season": 2,
        "caught_up_season": None,
        "caught_up_episode": None,
    }]
    first_client = _ReleaseClient({1: _series(1, "One"), 2: _series(2, "Two")})
    first_now = datetime(2026, 9, 1, tzinfo=timezone.utc)
    refresh_release_cache(archive, tracking, first_client, tmp_path, now=first_now, sleep=lambda _: None)

    second_client = _ReleaseClient({1: _series(1, "One")})
    result = refresh_release_cache(
        archive,
        tracking,
        second_client,
        tmp_path,
        now=datetime(2026, 9, 2, 1, tzinfo=timezone.utc),
        sleep=lambda _: None,
    )

    assert second_client.calls == [("series", 1), ("season", 1, 2)]
    assert result["full_scan"] is False


def test_weekly_refresh_still_updates_followed_show_after_one_day(tmp_path):
    archive = [{"tmdb_id": 1, "title": "One", "content_type": "tv", "last_watched": "2024-01-01"}]
    tracking = [{
        "tmdb_id": 1,
        "title": "One",
        "state": "following",
        "tracking_from_season": 2,
        "caught_up_season": None,
        "caught_up_episode": None,
    }]
    shows_dir = tmp_path / "shows"
    shows_dir.mkdir()
    (shows_dir / "1.json").write_text(json.dumps({
        "tmdb_id": 1,
        "title": "One",
        "fetched_at": "2026-09-01T00:00:00+00:00",
        "seasons": [],
    }))
    (tmp_path / "manifest.json").write_text(json.dumps({
        "full_refresh_at": "2026-08-27T00:00:00+00:00",
    }))
    client = _ReleaseClient({1: _series(1, "One")})

    result = refresh_release_cache(
        archive,
        tracking,
        client,
        tmp_path,
        now=datetime(2026, 9, 3, tzinfo=timezone.utc),
        sleep=lambda _: None,
    )

    assert client.calls == [("series", 1), ("season", 1, 2)]
    assert result == {"refreshed": 1, "failed": 0, "aborted": False, "full_scan": True}


def test_full_refresh_skips_fresh_snapshot_when_resuming_incomplete_scan(tmp_path):
    archive = [{"tmdb_id": 1, "title": "One", "content_type": "tv", "last_watched": "2024-01-01"}]
    shows_dir = tmp_path / "shows"
    shows_dir.mkdir()
    previous = {
        "tmdb_id": 1,
        "title": "One",
        "fetched_at": "2026-09-02T00:00:00+00:00",
        "seasons": [],
    }
    (shows_dir / "1.json").write_text(json.dumps(previous))
    client = _ReleaseClient({})

    result = refresh_release_cache(
        archive,
        [],
        client,
        tmp_path,
        now=datetime(2026, 9, 3, tzinfo=timezone.utc),
        sleep=lambda _: None,
    )

    assert client.calls == []
    assert result == {"refreshed": 0, "failed": 0, "aborted": False, "full_scan": True}
    assert load_snapshots(tmp_path)[1] == previous


def test_second_429_aborts_scan_and_preserves_previous_snapshot(tmp_path):
    archive = [{"tmdb_id": 1, "title": "One", "content_type": "tv", "last_watched": "2024-01-01"}]
    shows_dir = tmp_path / "shows"
    shows_dir.mkdir()
    previous = {"tmdb_id": 1, "title": "Old", "fetched_at": "2026-01-01T00:00:00+00:00", "seasons": []}
    (shows_dir / "1.json").write_text(json.dumps(previous))

    class LimitedClient:
        def fetch_tv_series_details(self, _tmdb_id):
            raise TmdbRateLimitError(3)

    waits = []
    result = refresh_release_cache(
        archive,
        [],
        LimitedClient(),
        tmp_path,
        now=datetime(2026, 9, 3, tzinfo=timezone.utc),
        sleep=waits.append,
    )

    assert result == {"refreshed": 0, "failed": 0, "aborted": True, "full_scan": True}
    assert waits == [3]
    assert load_snapshots(tmp_path)[1] == previous


def test_non_rate_limit_failure_retries_only_failed_show_after_one_day(tmp_path):
    archive = [
        {"tmdb_id": 1, "title": "One", "content_type": "tv", "last_watched": "2024-01-01"},
        {"tmdb_id": 2, "title": "Two", "content_type": "tv", "last_watched": "2024-01-01"},
    ]

    class PartlyFailingClient(_ReleaseClient):
        def fetch_tv_series_details(self, tmdb_id):
            if tmdb_id == 2:
                self.calls.append(("series", tmdb_id))
                raise OSError("temporary failure")
            return super().fetch_tv_series_details(tmdb_id)

    first_now = datetime(2026, 9, 1, tzinfo=timezone.utc)
    first_client = PartlyFailingClient({1: _series(1, "One")})
    first_result = refresh_release_cache(
        archive,
        [],
        first_client,
        tmp_path,
        now=first_now,
        sleep=lambda _: None,
    )

    assert first_result["failed"] == 1
    assert refresh_is_due(
        archive,
        [],
        tmp_path,
        now=datetime(2026, 9, 1, 12, tzinfo=timezone.utc),
    ) is False
    retry_now = datetime(2026, 9, 2, 1, tzinfo=timezone.utc)
    assert refresh_is_due(archive, [], tmp_path, now=retry_now) is True

    retry_client = _ReleaseClient({2: _series(2, "Two")})
    retry_result = refresh_release_cache(
        archive,
        [],
        retry_client,
        tmp_path,
        now=retry_now,
        sleep=lambda _: None,
    )

    assert retry_client.calls == [("series", 2), ("season", 2, 2)]
    assert retry_result == {"refreshed": 1, "failed": 0, "aborted": False, "full_scan": False}
    assert refresh_is_due(archive, [], tmp_path, now=retry_now) is False


def test_release_requests_are_spaced_by_half_a_second(tmp_path):
    archive = [{"tmdb_id": 1, "title": "One", "content_type": "tv", "last_watched": "2024-01-01"}]
    client = _ReleaseClient({1: _series(1, "One")})
    waits = []

    refresh_release_cache(
        archive,
        [],
        client,
        tmp_path,
        now=datetime(2026, 9, 3, tzinfo=timezone.utc),
        sleep=waits.append,
    )

    assert waits == [0.5]


def test_refresh_reports_progress_for_every_target_including_failures(tmp_path):
    archive = [
        {"tmdb_id": 1, "title": "One", "content_type": "tv", "last_watched": "2024-01-01"},
        {"tmdb_id": 2, "title": "Two", "content_type": "tv", "last_watched": "2024-01-01"},
        {"tmdb_id": 3, "title": "Three", "content_type": "tv", "last_watched": "2024-01-01"},
    ]

    class _PartlyBrokenClient(_ReleaseClient):
        def fetch_tv_series_details(self, tmdb_id):
            if tmdb_id == 2:
                raise RuntimeError("upstream is unhappy")
            return super().fetch_tv_series_details(tmdb_id)

    client = _PartlyBrokenClient({
        1: _series(1, "One"),
        3: _series(3, "Three"),
    })
    seen = []

    result = refresh_release_cache(
        archive,
        [],
        client,
        tmp_path,
        now=datetime(2026, 9, 3, tzinfo=timezone.utc),
        sleep=lambda _seconds: None,
        progress=lambda completed, total: seen.append((completed, total)),
    )

    assert result["refreshed"] == 2
    assert result["failed"] == 1
    # Starts at zero so the bar can render before the first request returns,
    # then advances once per show whether it succeeded or failed.
    assert seen == [(0, 3), (1, 3), (2, 3), (3, 3)]


def test_refresh_progress_is_optional(tmp_path):
    archive = [{"tmdb_id": 1, "title": "One", "content_type": "tv", "last_watched": "2024-01-01"}]
    client = _ReleaseClient({1: _series(1, "One")})

    result = refresh_release_cache(
        archive,
        [],
        client,
        tmp_path,
        now=datetime(2026, 9, 3, tzinfo=timezone.utc),
        sleep=lambda _seconds: None,
    )

    assert result["refreshed"] == 1


def test_last_refresh_at_reads_the_recorded_full_scan_time(tmp_path):
    archive = [{"tmdb_id": 1, "title": "One", "content_type": "tv", "last_watched": "2024-01-01"}]
    client = _ReleaseClient({1: _series(1, "One")})
    now = datetime(2026, 9, 3, 12, 30, tzinfo=timezone.utc)

    assert last_refresh_at(tmp_path) is None

    refresh_release_cache(
        archive, [], client, tmp_path, now=now, sleep=lambda _seconds: None,
    )

    assert last_refresh_at(tmp_path) == now


def test_last_refresh_at_survives_a_missing_or_unreadable_manifest(tmp_path):
    assert last_refresh_at(tmp_path / "nope") is None
    (tmp_path / "manifest.json").write_text("{not json")
    assert last_refresh_at(tmp_path) is None
