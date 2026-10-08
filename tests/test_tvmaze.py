import json
import os
import time
from unittest.mock import MagicMock, patch

import requests

from recommender.tvmaze import TvmazeClient


def _response(status=200, body=None):
    resp = MagicMock()
    resp.status_code = status
    resp.ok = status < 400
    resp.json.return_value = body
    return resp


def _show(schedule_time="23:00", timezone_name="America/New_York", channel="network"):
    return {
        "id": 263,
        "schedule": {"time": schedule_time, "days": ["Sunday"]},
        channel: {"name": "HBO", "country": {"timezone": timezone_name}},
    }


def _external_ids(calls=None):
    def fetch():
        if calls is not None:
            calls.append(1)
        return {"imdb_id": "tt3530232", "tvdb_id": 278518}
    return fetch


def test_show_time_comes_from_the_schedule_and_is_cached(tmp_path):
    client = TvmazeClient(tmp_path)
    calls = []
    with patch("recommender.tvmaze.requests.get", return_value=_response(body=_show())) as get:
        assert client.show_time(7, _external_ids(calls)) == {"time": "23:00", "timezone": "America/New_York"}
        assert client.show_time(7, _external_ids(calls)) == {"time": "23:00", "timezone": "America/New_York"}

    assert get.call_count == 1
    assert get.call_args.kwargs["params"] == {"imdb": "tt3530232"}
    assert len(calls) == 1


def test_tvdb_id_is_the_fallback_when_imdb_has_no_match(tmp_path):
    responses = [_response(404), _response(body=_show())]
    with patch("recommender.tvmaze.requests.get", side_effect=responses) as get:
        assert TvmazeClient(tmp_path).show_time(7, _external_ids())["time"] == "23:00"

    assert get.call_args.kwargs["params"] == {"thetvdb": 278518}


def test_streaming_show_without_a_schedule_time_has_no_show_time(tmp_path):
    body = _show(schedule_time="", channel="webChannel")
    with patch("recommender.tvmaze.requests.get", return_value=_response(body=body)):
        assert TvmazeClient(tmp_path).show_time(7, _external_ids()) is None


def test_web_channel_timezone_is_used_when_there_is_no_network(tmp_path):
    body = _show(timezone_name="Europe/London", channel="webChannel")
    with patch("recommender.tvmaze.requests.get", return_value=_response(body=body)):
        assert TvmazeClient(tmp_path).show_time(7, _external_ids())["timezone"] == "Europe/London"


def test_no_match_is_cached_too(tmp_path):
    client = TvmazeClient(tmp_path)
    with patch("recommender.tvmaze.requests.get", return_value=_response(404)) as get:
        assert client.show_time(7, _external_ids()) is None
        assert client.show_time(7, _external_ids()) is None

    assert get.call_count == 2  # IMDb then TVDB, on the first call only


def test_a_week_old_answer_is_checked_again(tmp_path):
    client = TvmazeClient(tmp_path)
    with patch("recommender.tvmaze.requests.get", return_value=_response(body=_show())):
        client.show_time(7, _external_ids())
    old = time.time() - 8 * 86400
    os.utime(tmp_path / "7.json", (old, old))

    with patch("recommender.tvmaze.requests.get", return_value=_response(body=_show("20:00"))):
        assert client.show_time(7, _external_ids())["time"] == "20:00"


def test_failure_keeps_the_last_known_time(tmp_path):
    client = TvmazeClient(tmp_path)
    with patch("recommender.tvmaze.requests.get", return_value=_response(body=_show())):
        client.show_time(7, _external_ids())
    old = time.time() - 8 * 86400
    os.utime(tmp_path / "7.json", (old, old))

    with patch("recommender.tvmaze.requests.get", side_effect=requests.Timeout("boom")):
        assert client.show_time(7, _external_ids())["time"] == "23:00"


def test_failure_with_nothing_cached_is_no_time_and_is_not_saved(tmp_path):
    with patch("recommender.tvmaze.requests.get", return_value=_response(503)):
        assert TvmazeClient(tmp_path).show_time(7, _external_ids()) is None

    assert not (tmp_path / "7.json").exists()


def test_failed_external_id_fetch_is_no_time(tmp_path):
    def broken():
        raise RuntimeError("tmdb down")

    with patch("recommender.tvmaze.requests.get") as get:
        assert TvmazeClient(tmp_path).show_time(7, broken) is None
    get.assert_not_called()


def test_corrupt_cache_file_is_fetched_again(tmp_path):
    (tmp_path / "7.json").write_text("{not json")

    with patch("recommender.tvmaze.requests.get", return_value=_response(body=_show())):
        assert TvmazeClient(tmp_path).show_time(7, _external_ids())["time"] == "23:00"

    assert json.loads((tmp_path / "7.json").read_text())["show_time"]["time"] == "23:00"
