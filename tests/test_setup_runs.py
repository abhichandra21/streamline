import json
import sys
from unittest.mock import patch

import pytest

from recommender import setup_runs


@pytest.fixture(autouse=True)
def paths(tmp_path, monkeypatch):
    monkeypatch.setattr(setup_runs, "LOCK_PATH", tmp_path / "setup.lock")
    monkeypatch.setattr(setup_runs, "STATUS_PATH", tmp_path / "setup_status.json")
    monkeypatch.setattr(setup_runs, "LOG_DIR", tmp_path / "logs")
    yield
    if setup_runs._held_lock:
        setup_runs._held_lock.close()
        setup_runs._held_lock = None


def test_a_held_lock_shows_as_running_and_blocks_a_second_run():
    assert not setup_runs.is_running()
    assert setup_runs.acquire_lock()

    assert setup_runs.is_running()
    assert not setup_runs.start("refresh_data")


def test_action_comes_from_the_flags():
    assert setup_runs.action_for([]) == "build"
    assert setup_runs.action_for(["--refresh-data", "--debug"]) == "refresh_data"
    assert setup_runs.action_for(["--check-exports"]) == "check_exports"
    assert setup_runs.action_for(["--ingest-only"]) == "import_exports"


def test_states_from_status_and_lock():
    assert setup_runs.current()["state"] is None

    status = setup_runs.record_start("refresh_data")
    assert setup_runs.current()["state"] == "stopped"  # started, lock free: the process died

    setup_runs.acquire_lock()
    assert setup_runs.current()["state"] == "running"

    setup_runs.record_end(status, ok=False)
    assert setup_runs.current()["state"] == "failed"
    setup_runs.record_end(status, ok=True)
    assert setup_runs.current()["state"] == "done"


def test_start_runs_setup_with_the_actions_flags_and_a_log(tmp_path):
    with patch("recommender.setup_runs.subprocess.Popen") as popen:
        popen.return_value.poll.return_value = 1  # exited at once: no waiting
        assert setup_runs.start("refresh_profile")

    cmd = popen.call_args.args[0]
    assert cmd[-3:] == ["-m", "recommender.setup", "--refresh-profile"]
    log = popen.call_args.kwargs["env"][setup_runs.LOG_ENV]
    assert log.startswith(str(tmp_path / "logs" / "setup-"))
    assert popen.call_args.kwargs["start_new_session"] is True


def test_log_tail_reads_the_runs_log(tmp_path):
    log = tmp_path / "run.log"
    log.write_text("".join(f"line {n}\n" for n in range(20)))

    assert setup_runs.log_tail({"log": str(log)}, lines=2) == ["line 18", "line 19"]
    assert setup_runs.log_tail({"log": None}) == []


def test_setup_records_a_failed_run(tmp_path):
    from recommender import setup

    def fail():
        sys.exit(1)

    with patch("recommender.setup.run_check_exports", fail), \
            patch("recommender.log.setup_logging"), pytest.raises(SystemExit) as exc:
        setup.main(["--check-exports"])

    assert exc.value.code == 1
    status = json.loads((tmp_path / "setup_status.json").read_text())
    assert status["action"] == "check_exports"
    assert status["ok"] is False


def test_a_second_setup_stops_while_one_runs(tmp_path):
    from recommender import setup

    setup_runs.acquire_lock()
    with patch("recommender.setup.run_ingest_only") as run, \
            patch("recommender.log.setup_logging"), pytest.raises(SystemExit) as exc:
        setup.main(["--ingest-only"])

    assert exc.value.code == 1
    run.assert_not_called()
