"""Query history storage tests — exercised through the module's public API."""

import json
import sqlite3
from pathlib import Path

import pytest

from recommender import history
from recommender.models import Recommendation


@pytest.fixture
def store(tmp_path, monkeypatch):
    """A history store on its own database with no legacy JSON file."""
    monkeypatch.setattr(history, "LEGACY_HISTORY_PATH", tmp_path / "query_history.json")
    return str(tmp_path / "streamline.db")


def _rec(title: str) -> Recommendation:
    return Recommendation(
        title=title,
        content_type="tv",
        score=8.25,
        vote_average=7.9,
        genres=["Drama", "Thriller"],
        explanation="Because you liked slow burns.",
        streaming_providers=["Netflix", "Prime Video", "Hulu", "Max", "Peacock"],
    )


def test_record_then_load(store):
    history.record("british crime", [_rec("Slow Horses")], "anthropic", "1k tokens",
                   db_path=store)

    entries = history.load(db_path=store)
    assert len(entries) == 1
    entry = entries[0]
    assert entry["query"] == "british crime"
    assert entry["provider"] == "anthropic"
    assert entry["usage"] == "1k tokens"
    assert entry["timestamp"]
    assert entry["results"][0]["title"] == "Slow Horses"
    # Providers stay capped at four, as before.
    assert entry["results"][0]["streaming_providers"] == [
        "Netflix", "Prime Video", "Hulu", "Max",
    ]


def test_load_is_newest_first(store):
    for query in ("first", "second", "third"):
        history.record(query, [], "anthropic", "", db_path=store)

    assert [e["query"] for e in history.load(db_path=store)] == ["third", "second", "first"]


def test_load_respects_limit(store):
    for query in ("first", "second", "third"):
        history.record(query, [], "anthropic", "", db_path=store)

    assert [e["query"] for e in history.load(limit=2, db_path=store)] == ["third", "second"]


def test_load_empty_store(store):
    assert history.load(db_path=store) == []


def test_record_accepts_enriched_dicts(store):
    item = {
        "title": "Ripley",
        "content_type": "tv",
        "score": 9.123456,
        "vote_average": 8.1,
        "genres": ["Drama", "Crime"],
        "explanation": "Patricia Highsmith, slowly.",
        "streaming_providers": ["Netflix"],
        "tmdb_id": 111803,
        "poster": "/poster.jpg",
        "tmdb_url": "https://tmdb/x",
        "imdb_url": "https://imdb/x",
    }
    history.record("stylish thriller", [item], "gemini", "2k tokens", db_path=store)

    result = history.load(db_path=store)[0]["results"][0]
    assert result["tmdb_id"] == 111803
    assert result["score"] == 9.123
    assert result["genres"] == ["Drama", "Crime"]
    assert result["poster"] == "/poster.jpg"


def test_metadata_allowlist(store):
    history.record(
        "mood match", [], "anthropic", "",
        metadata={
            "source": "wizard",
            "label": "cosy | evening",
            "summary": {"mood": "cosy"},
            "intent_dict": {"genres": ["comedy"], "top_n": 5},
            "context_note": "after dinner",
            "refinement": True,
            "secret": "should not be stored",
        },
        db_path=store,
    )

    entry = history.load(db_path=store)[0]
    assert entry["source"] == "wizard"
    assert entry["label"] == "cosy | evening"
    assert entry["summary"] == {"mood": "cosy"}
    assert entry["intent_dict"] == {"genres": ["comedy"], "top_n": 5}
    assert entry["context_note"] == "after dinner"
    assert entry["refinement"] is True
    assert "secret" not in entry


def test_delete_by_timestamp(store):
    history.record("keep", [], "anthropic", "", db_path=store)
    history.record("drop", [], "anthropic", "", db_path=store)
    target = history.load(db_path=store)[0]["timestamp"]

    assert history.delete(target, db_path=store) is True
    assert [e["query"] for e in history.load(db_path=store)] == ["keep"]


def test_delete_unknown_timestamp(store):
    history.record("keep", [], "anthropic", "", db_path=store)

    assert history.delete("2020-01-01T00:00:00+00:00", db_path=store) is False
    assert len(history.load(db_path=store)) == 1


def test_retention_cap(store):
    for i in range(history.MAX_ENTRIES + 15):
        history.record(f"query {i}", [], "anthropic", "", db_path=store)

    entries = history.load(db_path=store)
    assert len(entries) == history.MAX_ENTRIES
    assert entries[0]["query"] == f"query {history.MAX_ENTRIES + 14}"
    assert entries[-1]["query"] == "query 15"


# ── Legacy JSON migration ───────────────────────────────────────────────────

def _legacy_entries() -> list[dict]:
    return [
        {
            "timestamp": "2026-01-01T10:00:00+00:00",
            "query": "old search",
            "provider": "anthropic",
            "usage": "900 tokens",
            "results": [{
                "title": "Bodyguard",
                "content_type": "tv",
                "score": 7.5,
                "vote_average": 7.4,
                "genres": ["Thriller"],
                "explanation": "Tense.",
                "streaming_providers": ["Netflix"],
                "tmdb_id": 76648,
                "poster": "/bodyguard.jpg",
                "tmdb_url": "https://tmdb/bodyguard",
                "imdb_url": "https://imdb/bodyguard",
            }],
        },
        {
            "timestamp": "2026-01-02T11:00:00+00:00",
            "query": "Mood Match",
            "provider": "gemini",
            "usage": "1.2k tokens",
            "results": [],
            "source": "wizard",
            "label": "funny | short",
            "summary": {"mood": "funny"},
            "intent_dict": {"genres": ["comedy"], "top_n": 3},
            "context_note": "late night",
            "unknown_future_key": {"nested": [1, 2, 3]},
        },
    ]


@pytest.fixture
def legacy(tmp_path, monkeypatch):
    """A legacy JSON history file wired up as the migration source."""
    path = tmp_path / "query_history.json"
    path.write_text(json.dumps(_legacy_entries()), encoding="utf-8")
    monkeypatch.setattr(history, "LEGACY_HISTORY_PATH", path)
    return path


def test_migration_imports_legacy_json(legacy, tmp_path):
    db = str(tmp_path / "streamline.db")

    entries = history.load(db_path=db)

    assert [e["query"] for e in entries] == ["Mood Match", "old search"]
    wizard, old = entries
    assert wizard["source"] == "wizard"
    assert wizard["intent_dict"] == {"genres": ["comedy"], "top_n": 3}
    # Unknown keys survive rather than being dropped in translation.
    assert wizard["unknown_future_key"] == {"nested": [1, 2, 3]}
    assert old["results"][0]["tmdb_id"] == 76648
    assert old["results"][0]["poster"] == "/bodyguard.jpg"


def test_migration_keeps_legacy_file_as_backup(legacy, tmp_path):
    db = str(tmp_path / "streamline.db")
    original = legacy.read_text(encoding="utf-8")

    history.load(db_path=db)

    backup = Path(str(legacy) + ".migrated")
    assert not legacy.exists()
    assert backup.read_text(encoding="utf-8") == original


def test_migration_runs_once(legacy, tmp_path):
    db = str(tmp_path / "streamline.db")

    history.load(db_path=db)
    # A restored backup must not be imported a second time.
    Path(str(legacy) + ".migrated").rename(legacy)
    history.load(db_path=db)
    history.record("new search", [], "anthropic", "", db_path=db)

    assert [e["query"] for e in history.load(db_path=db)] == [
        "new search", "Mood Match", "old search",
    ]


def test_missing_legacy_file_is_not_an_error(store):
    assert history.load(db_path=store) == []


def test_malformed_legacy_file_is_reported_and_left_alone(tmp_path, monkeypatch):
    path = tmp_path / "query_history.json"
    path.write_text('[{"query": "half written"', encoding="utf-8")
    monkeypatch.setattr(history, "LEGACY_HISTORY_PATH", path)
    db = str(tmp_path / "streamline.db")

    with pytest.raises(history.MigrationFailed):
        history.load(db_path=db)

    assert path.read_text(encoding="utf-8") == '[{"query": "half written"'
    assert not Path(str(path) + ".migrated").exists()
    # Validation runs before anything is written, so the store is not even created.
    assert not Path(db).exists()


def test_malformed_legacy_file_leaves_an_existing_database_alone(tmp_path, monkeypatch):
    path = tmp_path / "query_history.json"
    path.write_text('[{"query": "half written"', encoding="utf-8")
    monkeypatch.setattr(history, "LEGACY_HISTORY_PATH", path)

    db = str(tmp_path / "streamline.db")
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE watch_events (id INTEGER PRIMARY KEY)")
    conn.commit()
    before = _table_names(conn)
    conn.close()

    with pytest.raises(history.MigrationFailed):
        history.load(db_path=db)

    conn = sqlite3.connect(db)
    try:
        assert _table_names(conn) == before
    finally:
        conn.close()


def _table_names(conn: sqlite3.Connection) -> set[str]:
    return {
        row[0]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }


def test_legacy_file_of_wrong_shape_is_reported(tmp_path, monkeypatch):
    path = tmp_path / "query_history.json"
    path.write_text('{"entries": []}', encoding="utf-8")
    monkeypatch.setattr(history, "LEGACY_HISTORY_PATH", path)

    with pytest.raises(history.MigrationFailed):
        history.load(db_path=str(tmp_path / "streamline.db"))

    assert path.exists()


def test_concurrent_connections_keep_every_committed_entry(store):
    """Overlapping writers, each on its own connection the way the CLI and web
    UI reach the store. 40 entries stays under the cap so retention cannot
    hide a lost write."""
    import threading

    writers, per_writer = 8, 5
    start = threading.Barrier(writers)
    failures: list = []

    def write(writer: int) -> None:
        start.wait(timeout=10)
        for n in range(per_writer):
            try:
                history.record(f"writer {writer} entry {n}", [], "anthropic", "",
                               db_path=store)
            except Exception as exc:  # noqa: BLE001 - reported below
                failures.append(exc)

    threads = [threading.Thread(target=write, args=(w,)) for w in range(writers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)

    assert not failures, failures
    queries = [e["query"] for e in history.load(db_path=store)]
    assert len(queries) == writers * per_writer
    assert set(queries) == {
        f"writer {w} entry {n}" for w in range(writers) for n in range(per_writer)
    }


def test_store_resolves_configured_event_db(tmp_path, monkeypatch):
    import config

    db = str(tmp_path / "configured.db")
    monkeypatch.setattr(config, "EVENT_DB_PATH", db)
    monkeypatch.setattr(history, "LEGACY_HISTORY_PATH", tmp_path / "query_history.json")

    history.record("configured path", [], "anthropic", "")

    assert Path(db).exists()
    assert [e["query"] for e in history.load()] == ["configured path"]


class _BarrieredPath:
    """A legacy path whose read blocks until every racing caller is past the
    pre-check, so first-use migration is genuinely interleaved."""

    def __init__(self, real: Path, barrier):
        self._real = real
        self._barrier = barrier

    @property
    def suffix(self) -> str:
        return self._real.suffix

    def exists(self) -> bool:
        return self._real.exists()

    def read_text(self, **kwargs) -> str:
        self._barrier.wait(timeout=10)
        return self._real.read_text(**kwargs)

    def with_suffix(self, suffix: str) -> Path:
        return self._real.with_suffix(suffix)

    def rename(self, target):
        return self._real.rename(target)

    def __str__(self) -> str:
        return str(self._real)


def test_concurrent_first_use_imports_once(legacy, tmp_path, monkeypatch):
    import threading

    db = str(tmp_path / "streamline.db")
    barrier = threading.Barrier(3)
    monkeypatch.setattr(history, "_legacy_path",
                        lambda: _BarrieredPath(legacy, barrier))

    results: list = []

    def open_history() -> None:
        try:
            results.append(len(history.load(db_path=db)))
        except Exception as exc:  # noqa: BLE001 - recorded, asserted on below
            results.append(exc)

    threads = [threading.Thread(target=open_history) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert not any(isinstance(r, Exception) for r in results), results
    queries = [e["query"] for e in history.load(db_path=db)]
    assert queries == ["Mood Match", "old search"]
    assert Path(str(legacy) + ".migrated").exists()


def test_legacy_file_appearing_after_first_use_is_ignored(tmp_path, monkeypatch):
    """A restored backup or a copy from another machine is not a migration
    source once the store is SQLite-native — importing it would merge two
    histories."""
    legacy_path = tmp_path / "query_history.json"
    monkeypatch.setattr(history, "LEGACY_HISTORY_PATH", legacy_path)
    db = str(tmp_path / "streamline.db")

    history.record("native search", [], "anthropic", "", db_path=db)

    legacy_path.write_text(json.dumps(_legacy_entries()), encoding="utf-8")
    assert [e["query"] for e in history.load(db_path=db)] == ["native search"]
    # Left exactly where it was: not read, not moved aside.
    assert legacy_path.exists()
    assert not Path(str(legacy_path) + ".migrated").exists()


def test_empty_legacy_file_migrates_to_an_empty_store(tmp_path, monkeypatch):
    """The old store wrote a 0-byte file whenever delete() ran before anything
    was recorded, and an interrupted write leaves the same. There is nothing
    to recover, so it must not brick the store."""
    legacy_path = tmp_path / "query_history.json"
    legacy_path.write_bytes(b"")
    monkeypatch.setattr(history, "LEGACY_HISTORY_PATH", legacy_path)
    db = str(tmp_path / "streamline.db")

    assert history.load(db_path=db) == []

    history.record("after upgrade", [], "anthropic", "", db_path=db)
    assert [e["query"] for e in history.load(db_path=db)] == ["after upgrade"]
    # Treated as a completed migration: moved aside, not read again.
    assert not legacy_path.exists()
    assert Path(str(legacy_path) + ".migrated").exists()


def test_legacy_entry_with_a_non_string_timestamp_is_reported(tmp_path, monkeypatch):
    legacy_path = tmp_path / "query_history.json"
    legacy_path.write_text(
        json.dumps([{"timestamp": {"nested": 1}, "query": "q", "results": []}]),
        encoding="utf-8",
    )
    monkeypatch.setattr(history, "LEGACY_HISTORY_PATH", legacy_path)
    db = str(tmp_path / "streamline.db")

    with pytest.raises(history.MigrationFailed):
        history.load(db_path=db)

    assert legacy_path.exists()
    assert not Path(db).exists()


def test_legacy_entry_keeps_an_odd_results_payload_verbatim(tmp_path, monkeypatch):
    """Payloads are stored as they arrived. The old JSON store served this
    entry unchanged too, so the migration does not get to reinterpret it."""
    legacy_path = tmp_path / "query_history.json"
    odd = {"timestamp": "2026-01-01T00:00:00+00:00", "query": "q",
           "results": "not-a-result-list"}
    legacy_path.write_text(json.dumps([odd]), encoding="utf-8")
    monkeypatch.setattr(history, "LEGACY_HISTORY_PATH", legacy_path)
    db = str(tmp_path / "streamline.db")

    assert history.load(db_path=db) == [odd]
