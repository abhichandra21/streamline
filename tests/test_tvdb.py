import json
import os
import time
from unittest.mock import MagicMock, patch

import requests

from recommender.tvdb import TvdbClient


def _response(status=200, body=None):
    resp = MagicMock()
    resp.status_code = status
    resp.ok = status < 400
    resp.json.return_value = body
    return resp


LOGIN = _response(body={"data": {"token": "t"}})


def _series(airs_time="23:00", country="usa", network="latestNetwork", home=None):
    data = {"id": 278518, "airsTime": airs_time, network: {"name": "HBO", "country": country}}
    if home:
        data["originalCountry"] = home
    return _response(body={"data": data})


def _external_ids(calls=None, tvdb_id=278518):
    def fetch():
        if calls is not None:
            calls.append(1)
        return {"imdb_id": "tt3530232", "tvdb_id": tvdb_id}
    return fetch


def _client(tmp_path):
    return TvdbClient(tmp_path, "key")


def test_show_time_is_read_in_the_network_country_timezone_and_cached(tmp_path):
    client = _client(tmp_path)
    calls = []
    with patch("recommender.tvdb.requests.post", return_value=LOGIN), \
            patch("recommender.tvdb.requests.get", return_value=_series()) as get:
        assert client.show_time(7, _external_ids(calls)) == {"time": "23:00", "timezone": "America/New_York"}
        assert client.show_time(7, _external_ids(calls)) == {"time": "23:00", "timezone": "America/New_York"}

    assert get.call_count == 1
    assert get.call_args.args[0].endswith("/series/278518/extended")
    assert get.call_args.kwargs["headers"] == {"Authorization": "Bearer t"}
    assert len(calls) == 1


def test_streaming_release_hour_is_kept(tmp_path):
    # Apple TV+ drops Slow Horses at midnight Eastern.
    with patch("recommender.tvdb.requests.post", return_value=LOGIN), \
            patch("recommender.tvdb.requests.get", return_value=_series("00:00")):
        assert _client(tmp_path).show_time(7, _external_ids()) == {"time": "00:00", "timezone": "America/New_York"}


def test_apple_midnight_show_is_out_the_evening_before(tmp_path):
    # Slow Horses: TheTVDB says 00:00, Apple puts it out at 9 PM Eastern the night before.
    body = _series("00:00", "usa", home="gbr")
    body.json.return_value["data"]["latestNetwork"]["name"] = "Apple TV"
    with patch("recommender.tvdb.requests.post", return_value=LOGIN), \
            patch("recommender.tvdb.requests.get", return_value=body):
        assert _client(tmp_path).show_time(7, _external_ids()) == {
            "time": "21:00", "timezone": "America/New_York", "days_early": 1}


def test_apple_show_with_its_own_time_keeps_it(tmp_path):
    body = _series("21:00", "usa")
    body.json.return_value["data"]["latestNetwork"]["name"] = "Apple TV"
    with patch("recommender.tvdb.requests.post", return_value=LOGIN), \
            patch("recommender.tvdb.requests.get", return_value=body):
        assert _client(tmp_path).show_time(7, _external_ids()) == {"time": "21:00", "timezone": "America/New_York"}


def test_uk_show_uses_london_time(tmp_path):
    with patch("recommender.tvdb.requests.post", return_value=LOGIN), \
            patch("recommender.tvdb.requests.get", return_value=_series("21:00", "gbr")):
        assert _client(tmp_path).show_time(7, _external_ids())["timezone"] == "Europe/London"


def test_home_country_wins_over_a_us_streamer(tmp_path):
    # Panchayat on Prime Video: TheTVDB lists 00:00, India time.
    with patch("recommender.tvdb.requests.post", return_value=LOGIN), \
            patch("recommender.tvdb.requests.get", return_value=_series("00:00", "usa", home="ind")):
        assert _client(tmp_path).show_time(7, _external_ids()) == {"time": "00:00", "timezone": "Asia/Kolkata"}


def test_netflix_show_is_out_at_3_am_eastern_whatever_tvdb_lists(tmp_path):
    # Dept. Q: TheTVDB lists 03:00 for a UK show; Black Doves 04:00.
    for listed in ("03:00", "04:00", ""):
        body = _series(listed, "usa", home="gbr")
        body.json.return_value["data"]["latestNetwork"]["name"] = "Netflix"
        with patch("recommender.tvdb.requests.post", return_value=LOGIN), \
                patch("recommender.tvdb.requests.get", return_value=body):
            assert TvdbClient(tmp_path / listed, "key").show_time(7, _external_ids()) == {
                "time": "03:00", "timezone": "America/New_York"}


def test_original_network_is_used_when_there_is_no_latest(tmp_path):
    with patch("recommender.tvdb.requests.post", return_value=LOGIN), \
            patch("recommender.tvdb.requests.get", return_value=_series(network="originalNetwork")):
        assert _client(tmp_path).show_time(7, _external_ids())["time"] == "23:00"


def test_no_time_or_unknown_country_has_no_show_time(tmp_path):
    with patch("recommender.tvdb.requests.post", return_value=LOGIN), \
            patch("recommender.tvdb.requests.get", return_value=_series("")):
        assert _client(tmp_path).show_time(7, _external_ids()) is None
    with patch("recommender.tvdb.requests.post", return_value=LOGIN), \
            patch("recommender.tvdb.requests.get", return_value=_series("21:00", "zzz")):
        assert _client(tmp_path).show_time(8, _external_ids()) is None


def test_show_without_a_tvdb_id_is_not_looked_up(tmp_path):
    with patch("recommender.tvdb.requests.post") as post, patch("recommender.tvdb.requests.get") as get:
        assert _client(tmp_path).show_time(7, _external_ids(tvdb_id=None)) is None
    post.assert_not_called()
    get.assert_not_called()


def test_no_match_is_cached_too(tmp_path):
    client = _client(tmp_path)
    with patch("recommender.tvdb.requests.post", return_value=LOGIN), \
            patch("recommender.tvdb.requests.get", return_value=_response(404)) as get:
        assert client.show_time(7, _external_ids()) is None
        assert client.show_time(7, _external_ids()) is None

    assert get.call_count == 1


def test_one_login_serves_every_show(tmp_path):
    client = _client(tmp_path)
    with patch("recommender.tvdb.requests.post", return_value=LOGIN) as post, \
            patch("recommender.tvdb.requests.get", return_value=_series()):
        client.show_time(7, _external_ids())
        client.show_time(8, _external_ids())

    assert post.call_count == 1


def test_bad_key_is_tried_once_and_saves_nothing(tmp_path):
    client = _client(tmp_path)
    with patch("recommender.tvdb.requests.post", return_value=_response(401)) as post, \
            patch("recommender.tvdb.requests.get") as get:
        assert client.show_time(7, _external_ids()) is None
        assert client.show_time(8, _external_ids()) is None

    assert post.call_count == 1
    get.assert_not_called()
    assert not list(tmp_path.iterdir())


def test_a_week_old_answer_is_checked_again(tmp_path):
    with patch("recommender.tvdb.requests.post", return_value=LOGIN), \
            patch("recommender.tvdb.requests.get", return_value=_series()):
        _client(tmp_path).show_time(7, _external_ids())
    old = time.time() - 8 * 86400
    os.utime(tmp_path / "7.json", (old, old))

    with patch("recommender.tvdb.requests.post", return_value=LOGIN), \
            patch("recommender.tvdb.requests.get", return_value=_series("20:00")):
        assert _client(tmp_path).show_time(7, _external_ids())["time"] == "20:00"


def test_failure_keeps_the_last_known_time(tmp_path):
    with patch("recommender.tvdb.requests.post", return_value=LOGIN), \
            patch("recommender.tvdb.requests.get", return_value=_series()):
        _client(tmp_path).show_time(7, _external_ids())
    old = time.time() - 8 * 86400
    os.utime(tmp_path / "7.json", (old, old))

    with patch("recommender.tvdb.requests.post", return_value=LOGIN), \
            patch("recommender.tvdb.requests.get", side_effect=requests.Timeout("boom")):
        assert _client(tmp_path).show_time(7, _external_ids())["time"] == "23:00"


def test_failure_with_nothing_cached_is_no_time_and_is_not_saved(tmp_path):
    with patch("recommender.tvdb.requests.post", return_value=LOGIN), \
            patch("recommender.tvdb.requests.get", return_value=_response(503)):
        assert _client(tmp_path).show_time(7, _external_ids()) is None

    assert not (tmp_path / "7.json").exists()


def test_failed_external_id_fetch_is_no_time(tmp_path):
    def broken():
        raise RuntimeError("tmdb down")

    with patch("recommender.tvdb.requests.get") as get:
        assert _client(tmp_path).show_time(7, broken) is None
    get.assert_not_called()


def test_corrupt_cache_file_is_fetched_again(tmp_path):
    (tmp_path / "7.json").write_text("{not json")

    with patch("recommender.tvdb.requests.post", return_value=LOGIN), \
            patch("recommender.tvdb.requests.get", return_value=_series()):
        assert _client(tmp_path).show_time(7, _external_ids())["time"] == "23:00"

    assert json.loads((tmp_path / "7.json").read_text())["show_time"]["time"] == "23:00"
