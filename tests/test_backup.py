import sqlite3
import zipfile
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from recommender import backup


@pytest.fixture
def state(tmp_path, monkeypatch):
    """A checkout in tmp_path with a database, settings, keys and overrides."""
    data = tmp_path / "data"
    data.mkdir()
    db = data / "streamline.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE title_ratings (title TEXT, rating TEXT)")
    conn.execute("INSERT INTO title_ratings VALUES ('Veep', 'more')")
    conn.commit()
    conn.close()
    (tmp_path / "config.local.yaml").write_text("provider: anthropic\n")
    (data / "keys.env").write_text("TMDB_API_KEY=abc\n")
    (tmp_path / ".env").write_text("TMDB_API_KEY=placeholder\n")
    (data / "overrides.json").write_text("{}")
    monkeypatch.setattr(backup.config, "__file__", str(tmp_path / "config.py"))
    monkeypatch.setattr(backup.config, "EVENT_DB_PATH", str(db))
    monkeypatch.setattr(backup.config, "KEYS_PATH", data / "keys.env")
    monkeypatch.setattr(backup.config, "OVERRIDES_PATH", str(data / "overrides.json"))
    monkeypatch.setattr(backup.config, "MANUAL_TV_PATH", str(data / "manual" / "tv.csv"))
    monkeypatch.setattr(backup.config, "MANUAL_MOVIES_PATH", str(data / "manual" / "movies.csv"))
    cache = tmp_path / "recommender" / "cache"
    (cache / "enrichments" / "tv").mkdir(parents=True)
    (cache / "taste_profile.txt").write_text("You love British crime.")
    (cache / "enrichments" / "tv" / "1.txt").write_text("notes")
    for name in ("WATCH_INDEX_PATH", "TASTE_PROFILE_PATH", "STRUCTURED_TASTE_PROFILE_PATH", "TASTE_TAGS_PATH",
                 "TASTE_THEMES_PATH", "TASTE_WORDS_PATH", "TASTE_PLACEMENTS_PATH"):
        file = getattr(backup.config, name).rsplit("/", 1)[-1]
        monkeypatch.setattr(backup.config, name, str(cache / file))
    monkeypatch.setattr(backup.config, "ENRICHMENT_CACHE_DIR", str(cache / "enrichments"))
    (cache / "tmdb" / "tv").mkdir(parents=True)
    (cache / "tmdb" / "tv" / "1.json").write_text("{}")
    (cache / "tmdb" / "find").mkdir()
    (cache / "tmdb" / "find" / "skip.json").write_text("{}")
    monkeypatch.setattr(backup.config, "CACHE_DIR", str(cache / "tmdb"))
    monkeypatch.setattr(backup.config, "BACKUP_KEEP", 7)
    monkeypatch.setattr(backup.config, "BACKUP_FOLDER", None)
    monkeypatch.setattr(backup.config, "BACKUP_RCLONE_REMOTE", None)
    monkeypatch.setattr(backup, "BACKUP_DIR", data / "backups")
    return tmp_path


def _at(minutes):
    return datetime(2026, 10, 19, 3, 0) + timedelta(minutes=minutes)


def test_backup_holds_everything_needed_to_restore(state):
    status = backup.make_backup(_at(0))

    path = backup.BACKUP_DIR / status["file"]
    with zipfile.ZipFile(path) as zf:
        assert sorted(zf.namelist()) == [
            ".env", "config.local.yaml", "data/keys.env", "data/overrides.json", "data/streamline.db",
            "recommender/cache/enrichments/tv/1.txt", "recommender/cache/taste_profile.txt",
            "recommender/cache/tmdb/tv/1.json"]
        zf.extract("data/streamline.db", state / "restored")
    conn = sqlite3.connect(state / "restored" / "data" / "streamline.db")
    assert conn.execute("SELECT title, rating FROM title_ratings").fetchall() == [("Veep", "more")]
    conn.close()


def test_backup_keeps_only_the_newest(state, monkeypatch):
    monkeypatch.setattr(backup.config, "BACKUP_KEEP", 3)
    for minute in range(5):
        backup.make_backup(_at(minute))

    names = [row["name"] for row in backup.list_backups()]
    assert names == [f"streamline-backup-20261019-03{m:02d}00.zip" for m in (4, 3, 2)]


def test_folder_copy_and_its_own_pruning(state, monkeypatch, tmp_path):
    nas = tmp_path / "nas"
    nas.mkdir()
    (nas / "family-photos.zip").write_text("not ours")
    monkeypatch.setattr(backup.config, "BACKUP_FOLDER", str(nas))
    monkeypatch.setattr(backup.config, "BACKUP_KEEP", 2)
    for minute in range(3):
        status = backup.make_backup(_at(minute))

    assert status["destinations"]["folder"] == {"where": str(nas), "ok": True, "error": None}
    assert sorted(p.name for p in nas.iterdir()) == [
        "family-photos.zip", "streamline-backup-20261019-030100.zip", "streamline-backup-20261019-030200.zip"]


def test_a_missing_folder_is_reported_and_the_local_backup_still_saved(state, monkeypatch, tmp_path):
    monkeypatch.setattr(backup.config, "BACKUP_FOLDER", str(tmp_path / "unmounted"))

    status = backup.make_backup(_at(0))

    assert status["destinations"]["folder"]["ok"] is False
    assert "mounted" in status["destinations"]["folder"]["error"]
    assert (backup.BACKUP_DIR / status["file"]).is_file()
    assert backup.last_status() == status


def test_cloud_upload_through_rclone_and_pruning(state, monkeypatch):
    calls = []
    remote_files = ["streamline-backup-20261018-030000.zip", "streamline-backup-20261017-030000.zip",
                    "notes.txt"]

    def fake_rclone(*args):
        calls.append(args)
        if args[0] == "lsf":
            return "\n".join(remote_files + ["streamline-backup-20261019-030000.zip"]) + "\n"
        return ""
    monkeypatch.setattr(backup, "_rclone", fake_rclone)
    monkeypatch.setattr(backup.config, "BACKUP_RCLONE_REMOTE", "gdrive:Streamline/")
    monkeypatch.setattr(backup.config, "BACKUP_KEEP", 2)

    status = backup.make_backup(_at(0))

    assert status["destinations"]["cloud"]["ok"] is True
    assert calls[0][0] == "copyto"
    assert calls[0][2] == "gdrive:Streamline/streamline-backup-20261019-030000.zip"
    assert calls[-1] == ("deletefile", "gdrive:Streamline/streamline-backup-20261017-030000.zip")


def test_rclone_failure_is_reported_in_plain_words(state, monkeypatch):
    monkeypatch.setattr(backup.shutil, "which", lambda name: None)
    monkeypatch.setattr(backup.config, "BACKUP_RCLONE_REMOTE", "gdrive:Streamline")

    status = backup.make_backup(_at(0))

    assert status["destinations"]["cloud"] == {
        "where": "gdrive:Streamline", "ok": False, "error": "rclone is not installed on the server."}


def test_due_once_a_day(state):
    assert backup.is_due(_at(0))
    backup.make_backup(_at(0))
    assert not backup.is_due(_at(60 * 23))
    assert backup.is_due(_at(60 * 24))


def test_only_one_backup_at_a_time(state):
    import fcntl
    backup.BACKUP_DIR.mkdir(parents=True)
    with open(backup.BACKUP_DIR / ".lock", "w") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(backup.BackupError, match="already running"):
            backup.make_backup(_at(0))


def test_cli_reports_a_failed_copy(state, monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(backup.config, "BACKUP_FOLDER", str(tmp_path / "unmounted"))

    assert backup.main([]) == 1
    out = capsys.readouterr()
    assert "Saved" in out.out
    assert "failed" in out.err


def test_a_failed_backup_is_recorded_next_to_the_last_good_one(state, monkeypatch):
    good = backup.make_backup(_at(0))

    def full_disk(target):
        Path(target).write_text("half")
        raise OSError("No space left on device")
    monkeypatch.setattr(backup, "_write_zip", full_disk)
    with pytest.raises(backup.BackupError, match="No space left"):
        backup.make_backup(_at(60 * 24))

    status = backup.last_status()
    assert status["file"] == good["file"]
    assert status["failure"] == {"at": "2026-10-20T03:00:00", "error": "No space left on device"}
    assert [p.name for p in backup.BACKUP_DIR.glob("*.partial")] == []
    assert backup.is_due(_at(60 * 25))


def test_the_next_good_backup_clears_the_failure(state, monkeypatch):
    real = backup._write_zip

    def fail_once(target):
        raise OSError("disk unplugged")
    monkeypatch.setattr(backup, "_write_zip", fail_once)
    with pytest.raises(backup.BackupError):
        backup.make_backup(_at(0))
    monkeypatch.setattr(backup, "_write_zip", real)

    assert "failure" not in backup.make_backup(_at(5))
    assert "failure" not in backup.last_status()


def test_restored_app_counts_as_set_up(state, monkeypatch):
    """Without the watch index a restore lands on Get started and needs the exports."""
    index = state / "recommender" / "cache" / "watch_index.json"
    index.write_text("[]")
    monkeypatch.setattr(backup.config, "WATCH_INDEX_PATH", str(index))

    status = backup.make_backup(_at(0))

    with zipfile.ZipFile(backup.BACKUP_DIR / status["file"]) as zf:
        assert {"recommender/cache/watch_index.json", "recommender/cache/taste_profile.txt"} <= set(zf.namelist())
