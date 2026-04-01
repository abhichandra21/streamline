# LLM Query-Driven Recommender Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the batch tag-based recommender with an LLM-powered system that answers natural language queries like "give me a good British crime drama I haven't watched."

**Architecture:** Two-phase pipeline — offline setup (Claude Haiku enriches titles, Claude Sonnet builds a natural-language taste profile, watch history is indexed for exclusion) and online query handling (Claude Sonnet parses intent, TMDB `discover` endpoint fetches targeted candidates, Claude Sonnet re-ranks and explains).

**Tech Stack:** Python 3.10, `anthropic` SDK (new), `requests`, TMDB API, existing ingestion parsers

---

## File Map

| Action | File | Purpose |
|--------|------|---------|
| Create | `recommender/models.py` | `Recommendation` dataclass (moved from deleted `engine.py`) |
| Create | `recommender/watch_index.py` | Build/save/load exclusion index from watch history |
| Create | `recommender/enricher.py` | Claude Haiku semantic descriptions per title, cached |
| Create | `recommender/taste_profile_builder.py` | Claude Sonnet natural-language taste profile |
| Create | `recommender/query_engine.py` | `QueryIntent`, intent parsing, candidate ranking |
| Create | `recommender/setup.py` | Offline orchestration (enrichment + profile + index) |
| Create | `tests/test_models.py` | Tests for Recommendation |
| Create | `tests/test_watch_index.py` | Tests for watch index build/normalize/lookup |
| Create | `tests/test_enricher.py` | Tests for enrichment with mocked Claude |
| Create | `tests/test_taste_profile_builder.py` | Tests for profile build with mocked Claude |
| Create | `tests/test_query_engine.py` | Tests for intent parsing and ranking with mocked Claude |
| Modify | `recommender/tmdb_client.py` | Add genre maps + `search_by_filters()` method |
| Modify | `config.py` | Add `ANTHROPIC_API_KEY`, enrichment/profile/index paths |
| Modify | `requirements.txt` | Add `anthropic>=0.40.0` |
| Rewrite | `recommender/main.py` | Query-driven CLI (single-shot + interactive) |
| Rewrite | `tests/test_main.py` | Tests for new CLI |
| Delete | `recommender/engine.py` | Replaced by `query_engine.py` |
| Delete | `recommender/taste_profile.py` | Replaced by `taste_profile_builder.py` |
| Delete | `tests/test_engine.py` | Tests for deleted module |
| Delete | `tests/test_taste_profile.py` | Tests for deleted module |
| Modify | `tests/ingestion/test_base.py` | Remove `test_prime_stub_raises` (stub is now real) |

---

## Task 1: Dependencies and cleanup

**Files:**
- Modify: `requirements.txt`
- Delete: `recommender/engine.py`, `recommender/taste_profile.py`
- Delete: `tests/test_engine.py`, `tests/test_taste_profile.py`
- Modify: `tests/ingestion/test_base.py`

- [ ] **Step 1: Add anthropic to requirements.txt**

Replace the full contents of `requirements.txt` with:
```
requests>=2.31.0
python-dateutil>=2.8.2
pytest>=7.4.0
anthropic>=0.40.0
```

- [ ] **Step 2: Install the new dependency**

```bash
pip install anthropic>=0.40.0
```

Expected: installs without error.

- [ ] **Step 3: Delete dead modules**

```bash
rm recommender/engine.py recommender/taste_profile.py
rm tests/test_engine.py tests/test_taste_profile.py
```

- [ ] **Step 4: Remove the stale prime stub test from tests/ingestion/test_base.py**

Delete the `test_prime_stub_raises` function (lines that read):
```python
def test_prime_stub_raises():
    from recommender.ingestion.prime import parse
    try:
        parse("any_path.csv")
        assert False, "Should have raised NotImplementedError"
    except NotImplementedError as e:
        assert "prime" in str(e).lower() or "Prime" in str(e)
```

- [ ] **Step 5: Verify remaining tests still pass**

```bash
python -m pytest tests/ -q
```

Expected: all remaining tests pass (the import errors from deleted modules are gone).

- [ ] **Step 6: Commit**

```bash
git add -A
git commit -m "chore: remove engine/taste_profile modules, add anthropic dependency"
```

---

## Task 2: models.py and config.py

**Files:**
- Create: `recommender/models.py`
- Create: `tests/test_models.py`
- Modify: `config.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_models.py`:
```python
from recommender.models import Recommendation


def test_recommendation_fields():
    rec = Recommendation(
        title="Broadchurch",
        content_type="tv",
        score=0.91,
        vote_average=8.4,
        genres=["Crime", "Drama"],
        explanation="Fits your love of slow-burn British crime with strong characters.",
    )
    assert rec.title == "Broadchurch"
    assert rec.content_type == "tv"
    assert rec.score == 0.91
    assert rec.vote_average == 8.4
    assert rec.genres == ["Crime", "Drama"]
    assert "British" in rec.explanation


def test_recommendation_is_dataclass():
    from dataclasses import fields
    field_names = {f.name for f in fields(Recommendation)}
    assert field_names == {"title", "content_type", "score", "vote_average", "genres", "explanation"}
```

- [ ] **Step 2: Run test to verify it fails**

```bash
python -m pytest tests/test_models.py -v
```

Expected: FAIL with `ModuleNotFoundError: No module named 'recommender.models'`

- [ ] **Step 3: Create recommender/models.py**

```python
from dataclasses import dataclass


@dataclass
class Recommendation:
    title: str
    content_type: str
    score: float
    vote_average: float
    genres: list[str]
    explanation: str
```

- [ ] **Step 4: Run test to verify it passes**

```bash
python -m pytest tests/test_models.py -v
```

Expected: 2 tests PASS.

- [ ] **Step 5: Update config.py**

Add these lines at the bottom of `config.py`:
```python
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

ENRICHMENT_CACHE_DIR = os.path.join(os.path.dirname(__file__), "recommender/cache/enrichments")
TASTE_PROFILE_PATH = os.path.join(os.path.dirname(__file__), "recommender/cache/taste_profile.txt")
WATCH_INDEX_PATH = os.path.join(os.path.dirname(__file__), "recommender/cache/watch_index.json")
```

- [ ] **Step 6: Verify config loads correctly**

```bash
python -c "import config; print(bool(config.ANTHROPIC_API_KEY)); print(config.WATCH_INDEX_PATH)"
```

Expected: prints `True` and a valid path ending in `watch_index.json`.

- [ ] **Step 7: Commit**

```bash
git add recommender/models.py tests/test_models.py config.py
git commit -m "feat: add Recommendation model and config paths for LLM pipeline"
```

---

## Task 3: watch_index.py

**Files:**
- Create: `recommender/watch_index.py`
- Create: `tests/test_watch_index.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_watch_index.py`:
```python
import json
from datetime import datetime, timedelta

import pytest

from recommender.ingestion.base import WatchEvent
from recommender.tmdb_client import TmdbMetadata
from recommender.watch_index import WatchIndex
from recommender import watch_index as wi


def make_event(title, series_name=None, content_type="movie"):
    return WatchEvent(
        platform="prime",
        title=title,
        content_type=content_type,
        series_name=series_name or title,
        watched_duration=timedelta(hours=1),
        total_duration=None,
        timestamp=datetime.now(),
        profile="ADULT",
    )


def make_meta(tmdb_id, title, content_type="movie"):
    return TmdbMetadata(
        tmdb_id=tmdb_id, content_type=content_type, title=title,
        genres=[], keywords=[], cast=[],
        original_language="en", vote_average=0.0, vote_count=0,
    )


def test_build_includes_movie_titles():
    events = [make_event("Dilwale Dulhania Le Jayenge (English Subtitled)")]
    index = wi.build(events, {})
    assert "dilwale dulhania le jayenge" in index.normalized_titles


def test_build_strips_parentheticals():
    events = [make_event("Oppenheimer (4K UHD)")]
    index = wi.build(events, {})
    assert "oppenheimer" in index.normalized_titles
    assert "oppenheimer (4k uhd)" not in index.normalized_titles


def test_build_includes_tv_series_names():
    events = [make_event("Episode 1-Downton Abbey - Season 3", series_name="Downton Abbey", content_type="tv")]
    index = wi.build(events, {})
    assert "downton abbey" in index.normalized_titles


def test_build_deduplicates():
    events = [
        make_event("Chef"),
        make_event("Chef"),
        make_event("Chef (Hindi)"),
    ]
    index = wi.build(events, {})
    assert len([t for t in index.normalized_titles if "chef" in t]) == 1


def test_build_stores_tmdb_id():
    events = [make_event("Fleabag", series_name="Fleabag", content_type="tv")]
    meta = make_meta(tmdb_id=67452, title="Fleabag", content_type="tv")
    index = wi.build(events, {"Fleabag": meta})
    assert 67452 in index.tmdb_ids


def test_is_watched_by_tmdb_id():
    meta = make_meta(tmdb_id=12345, title="Fleabag", content_type="tv")
    index = WatchIndex(tmdb_ids={12345}, normalized_titles=set(), entries=[])
    assert index.is_watched(meta) is True


def test_is_watched_by_title_fallback():
    meta = make_meta(tmdb_id=0, title="Downton Abbey", content_type="tv")
    index = WatchIndex(tmdb_ids=set(), normalized_titles={"downton abbey"}, entries=[])
    assert index.is_watched(meta) is True


def test_is_watched_false_for_unknown():
    meta = make_meta(tmdb_id=99999, title="Broadchurch", content_type="tv")
    index = WatchIndex(tmdb_ids={12345}, normalized_titles={"fleabag"}, entries=[])
    assert index.is_watched(meta) is False


def test_save_and_load_roundtrip(tmp_path):
    path = str(tmp_path / "index.json")
    events = [make_event("Fleabag", series_name="Fleabag", content_type="tv")]
    meta = make_meta(tmdb_id=67452, title="Fleabag", content_type="tv")
    index = wi.build(events, {"Fleabag": meta})
    wi.save(index, path)
    loaded = wi.load(path)
    assert 67452 in loaded.tmdb_ids
    assert "fleabag" in loaded.normalized_titles
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
python -m pytest tests/test_watch_index.py -v
```

Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Create recommender/watch_index.py**

```python
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from .ingestion.base import WatchEvent
from .tmdb_client import TmdbMetadata


def _normalize(title: str) -> str:
    """Lowercase, strip parenthetical suffixes and edition markers."""
    title = title.lower()
    title = re.sub(r'\s*\([^)]*\)', '', title)
    return title.strip()


@dataclass
class WatchIndex:
    tmdb_ids: set[int]
    normalized_titles: set[str]
    entries: list[dict]

    def is_watched(self, candidate: TmdbMetadata) -> bool:
        """Check by TMDB ID first; fall back to normalized title match."""
        if candidate.tmdb_id and candidate.tmdb_id in self.tmdb_ids:
            return True
        return _normalize(candidate.title) in self.normalized_titles


def build(events: list[WatchEvent], metadata: dict[str, TmdbMetadata]) -> WatchIndex:
    """Build exclusion index from watch events. metadata provides TMDB IDs."""
    tmdb_ids: set[int] = set()
    normalized_titles: set[str] = set()
    entries: list[dict] = []
    seen_keys: set[str] = set()

    for e in events:
        key = e.series_name if e.content_type == 'tv' else e.title
        normalized_titles.add(_normalize(e.title))
        if e.content_type == 'tv':
            normalized_titles.add(_normalize(e.series_name))
        if key not in seen_keys:
            seen_keys.add(key)
            meta = metadata.get(key)
            tmdb_id = meta.tmdb_id if meta else 0
            if tmdb_id:
                tmdb_ids.add(tmdb_id)
            entries.append({
                "tmdb_id": tmdb_id,
                "title": key,
                "content_type": e.content_type,
            })

    return WatchIndex(tmdb_ids=tmdb_ids, normalized_titles=normalized_titles, entries=entries)


def save(index: WatchIndex, path: str) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(index.entries))


def load(path: str) -> WatchIndex:
    entries = json.loads(Path(path).read_text())
    tmdb_ids = {e["tmdb_id"] for e in entries if e.get("tmdb_id")}
    normalized_titles = {_normalize(e["title"]) for e in entries}
    return WatchIndex(tmdb_ids=tmdb_ids, normalized_titles=normalized_titles, entries=entries)
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
python -m pytest tests/test_watch_index.py -v
```

Expected: 10 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add recommender/watch_index.py tests/test_watch_index.py
git commit -m "feat: watch index for cross-platform watched title exclusion"
```

---

## Task 4: tmdb_client.py — dynamic search by filters

**Files:**
- Modify: `recommender/tmdb_client.py`
- Modify: `tests/test_tmdb_client.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_tmdb_client.py`:
```python
from unittest.mock import patch, MagicMock
from recommender.tmdb_client import TmdbClient, MOVIE_GENRE_IDS, TV_GENRE_IDS


def test_genre_maps_have_crime():
    assert 'crime' in MOVIE_GENRE_IDS
    assert 'crime' in TV_GENRE_IDS
    assert MOVIE_GENRE_IDS['crime'] == 80
    assert TV_GENRE_IDS['crime'] == 80


def test_genre_maps_have_drama():
    assert 'drama' in MOVIE_GENRE_IDS
    assert 'drama' in TV_GENRE_IDS
    assert MOVIE_GENRE_IDS['drama'] == 18


def test_search_by_filters_calls_discover(tmp_path):
    client = TmdbClient(api_key="test", cache_dir=str(tmp_path))

    discover_response = {"results": [{"id": 1}, {"id": 2}]}
    details_response = {
        "id": 1, "name": "Test Show", "genres": [{"name": "Crime"}],
        "keywords": {"results": []}, "credits": {"cast": [], "crew": []},
        "created_by": [], "episode_run_time": [45],
        "original_language": "en", "vote_average": 8.0, "vote_count": 500,
    }

    with patch.object(client, '_get') as mock_get:
        mock_get.side_effect = [discover_response, details_response, details_response]
        results = client.search_by_filters(
            content_type="tv",
            genres=["crime"],
            origin_countries=["GB"],
            size=2,
        )

    discover_call = mock_get.call_args_list[0]
    assert discover_call[0][0] == "discover/tv"
    assert "80" in discover_call[1]['params']['with_genres']
    assert discover_call[1]['params']['with_origin_country'] == "GB"


def test_search_by_filters_movie(tmp_path):
    client = TmdbClient(api_key="test", cache_dir=str(tmp_path))

    discover_response = {"results": [{"id": 10}]}
    details_response = {
        "id": 10, "title": "Test Movie", "genres": [{"name": "Drama"}],
        "keywords": {"keywords": []}, "credits": {"cast": [], "crew": []},
        "runtime": 120, "original_language": "hi",
        "vote_average": 7.5, "vote_count": 300,
    }

    with patch.object(client, '_get') as mock_get:
        mock_get.side_effect = [discover_response, details_response]
        results = client.search_by_filters(
            content_type="movie",
            languages=["hi"],
            size=1,
        )

    discover_call = mock_get.call_args_list[0]
    assert discover_call[0][0] == "discover/movie"
    assert discover_call[1]['params']['with_original_language'] == "hi"
```

- [ ] **Step 2: Run new tests to verify they fail**

```bash
python -m pytest tests/test_tmdb_client.py -v -k "genre_maps or search_by_filters"
```

Expected: FAIL with `ImportError: cannot import name 'MOVIE_GENRE_IDS'`

- [ ] **Step 3: Add genre maps and search_by_filters to recommender/tmdb_client.py**

Add after the `TMDB_BASE` constant at the top of the file:
```python
MOVIE_GENRE_IDS: dict[str, int] = {
    'action': 28, 'adventure': 12, 'animation': 16, 'comedy': 35,
    'crime': 80, 'documentary': 99, 'drama': 18, 'family': 10751,
    'fantasy': 14, 'history': 36, 'horror': 27, 'music': 10402,
    'mystery': 9648, 'romance': 10749, 'sci-fi': 878, 'thriller': 53,
    'war': 10752, 'western': 37,
}

TV_GENRE_IDS: dict[str, int] = {
    'action': 10759, 'adventure': 10759, 'animation': 16, 'comedy': 35,
    'crime': 80, 'documentary': 99, 'drama': 18, 'family': 10751,
    'kids': 10762, 'mystery': 9648, 'sci-fi': 10765, 'thriller': 80,
    'war': 10768, 'western': 37,
}
```

Add this method inside the `TmdbClient` class, after `get_metadata`:
```python
def search_by_filters(
    self,
    content_type: str,
    genres: list[str] | None = None,
    origin_countries: list[str] | None = None,
    languages: list[str] | None = None,
    year_from: int | None = None,
    year_to: int | None = None,
    size: int = 50,
) -> list[TmdbMetadata]:
    """Fetch candidates from TMDB discover endpoint using explicit filters."""
    prefix = "tv" if content_type == "tv" else "movie"
    genre_map = TV_GENRE_IDS if content_type == "tv" else MOVIE_GENRE_IDS
    date_field = "first_air_date" if content_type == "tv" else "primary_release_date"

    params: dict = {"sort_by": "vote_average.desc", "vote_count.gte": 100}

    if genres:
        ids = [str(genre_map[g.lower()]) for g in genres if g.lower() in genre_map]
        if ids:
            params["with_genres"] = ",".join(ids)

    if origin_countries:
        params["with_origin_country"] = "|".join(origin_countries)

    if languages:
        params["with_original_language"] = "|".join(languages)

    if year_from:
        params[f"{date_field}.gte"] = f"{year_from}-01-01"

    if year_to:
        params[f"{date_field}.lte"] = f"{year_to}-12-31"

    candidates: dict[int, TmdbMetadata] = {}
    page = 1
    while len(candidates) < size:
        params["page"] = page
        data = self._get(f"discover/{prefix}", params=params)
        results = data.get("results", [])
        if not results:
            break
        for item in results:
            tmdb_id = item["id"]
            if tmdb_id in candidates:
                continue
            cached = self._load_cache(content_type, tmdb_id)
            if cached:
                candidates[tmdb_id] = self._parse_metadata(cached, content_type)
            else:
                try:
                    details = self._fetch_details(tmdb_id, content_type)
                    self._save_cache(content_type, tmdb_id, details)
                    candidates[tmdb_id] = self._parse_metadata(details, content_type)
                    time.sleep(0.05)
                except Exception:
                    continue
            if len(candidates) >= size:
                break
        page += 1

    return list(candidates.values())
```

- [ ] **Step 4: Run all tmdb_client tests**

```bash
python -m pytest tests/test_tmdb_client.py -v
```

Expected: all tests PASS.

- [ ] **Step 5: Commit**

```bash
git add recommender/tmdb_client.py tests/test_tmdb_client.py
git commit -m "feat: add search_by_filters to TmdbClient with genre/country/language support"
```

---

## Task 5: enricher.py

**Files:**
- Create: `recommender/enricher.py`
- Create: `tests/test_enricher.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_enricher.py`:
```python
import pytest
from pathlib import Path
from unittest.mock import MagicMock

from recommender.tmdb_client import TmdbMetadata
from recommender.enricher import enrich, enrich_batch


def make_meta(title, tmdb_id=42, content_type="tv", genres=None, keywords=None):
    return TmdbMetadata(
        tmdb_id=tmdb_id, content_type=content_type, title=title,
        genres=genres or ["Drama"], keywords=keywords or ["mystery"],
        cast=["Actor One"], creator_or_director="Director One",
        original_language="en", vote_average=8.0, vote_count=500,
    )


def make_mock_client(response_text: str):
    client = MagicMock()
    msg = MagicMock()
    msg.content = [MagicMock(text=response_text)]
    client.messages.create.return_value = msg
    return client


def test_enrich_calls_haiku(tmp_path):
    meta = make_meta("Broadchurch")
    client = make_mock_client("A slow-burn British crime drama set in a coastal town.")
    result = enrich(meta, str(tmp_path), client)
    assert "slow-burn" in result
    call_kwargs = client.messages.create.call_args[1]
    assert call_kwargs["model"] == "claude-haiku-4-5-20251001"


def test_enrich_caches_result(tmp_path):
    meta = make_meta("Broadchurch", tmdb_id=99)
    client = make_mock_client("A slow-burn British crime drama.")
    enrich(meta, str(tmp_path), client)
    cache_file = tmp_path / "tv" / "99.txt"
    assert cache_file.exists()
    assert "slow-burn" in cache_file.read_text()


def test_enrich_uses_cache_on_second_call(tmp_path):
    meta = make_meta("Broadchurch", tmdb_id=99)
    client = make_mock_client("A slow-burn British crime drama.")
    enrich(meta, str(tmp_path), client)
    enrich(meta, str(tmp_path), client)
    assert client.messages.create.call_count == 1


def test_enrich_fallback_on_api_error(tmp_path):
    meta = make_meta("Broadchurch", genres=["Crime", "Drama"], keywords=["mystery"])
    client = MagicMock()
    client.messages.create.side_effect = Exception("API error")
    result = enrich(meta, str(tmp_path), client)
    assert "Crime" in result or "mystery" in result


def test_enrich_unknown_title_uses_slug_cache(tmp_path):
    meta = TmdbMetadata(
        tmdb_id=0, content_type="movie", title="My Unknown Film",
        genres=["Drama"], keywords=[], cast=[],
        original_language="en", vote_average=0.0, vote_count=0,
    )
    client = make_mock_client("An unknown film.")
    enrich(meta, str(tmp_path), client)
    cache_file = tmp_path / "unknown" / "my-unknown-film.txt"
    assert cache_file.exists()


def test_enrich_batch_skips_failed(tmp_path):
    meta1 = make_meta("Show A", tmdb_id=1)
    meta2 = make_meta("Show B", tmdb_id=2)
    client = MagicMock()
    good_msg = MagicMock()
    good_msg.content = [MagicMock(text="Good description.")]
    client.messages.create.side_effect = [good_msg, Exception("fail")]
    result = enrich_batch({"Show A": meta1, "Show B": meta2}, str(tmp_path), client)
    assert "Show A" in result
    assert "Show B" in result  # fallback used
    assert result["Show A"] == "Good description."
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
python -m pytest tests/test_enricher.py -v
```

Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Create recommender/enricher.py**

```python
import re
from pathlib import Path

import anthropic

from .tmdb_client import TmdbMetadata


def _cache_path(metadata: TmdbMetadata, cache_dir: str) -> Path:
    if metadata.tmdb_id:
        return Path(cache_dir) / metadata.content_type / f"{metadata.tmdb_id}.txt"
    slug = re.sub(r'[^a-z0-9]+', '-', metadata.title.lower()).strip('-')
    return Path(cache_dir) / 'unknown' / f"{slug}.txt"


def _fallback_description(metadata: TmdbMetadata) -> str:
    parts = metadata.genres + metadata.keywords
    if metadata.original_language:
        parts.append(metadata.original_language)
    if metadata.creator_or_director:
        parts.append(metadata.creator_or_director)
    return ' '.join(parts)


def enrich(metadata: TmdbMetadata, cache_dir: str, client: anthropic.Anthropic) -> str:
    """Return a semantic description for a title, using cache if available."""
    path = _cache_path(metadata, cache_dir)
    if path.exists():
        return path.read_text()

    tmdb_info = (
        f"Title: {metadata.title}\n"
        f"Type: {metadata.content_type}\n"
        f"Genres: {', '.join(metadata.genres)}\n"
        f"Keywords: {', '.join(metadata.keywords)}\n"
        f"Cast: {', '.join(metadata.cast)}\n"
        f"Language: {metadata.original_language}"
    )
    if metadata.creator_or_director:
        tmdb_info += f"\nDirector/Creator: {metadata.creator_or_director}"

    try:
        message = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=200,
            messages=[{
                "role": "user",
                "content": (
                    "Write a 2-3 sentence description of this title capturing its mood, "
                    "pacing, tone, cultural flavor, and themes. Be specific, not generic.\n\n"
                    + tmdb_info
                ),
            }],
        )
        description = message.content[0].text.strip()
    except Exception:
        description = _fallback_description(metadata)

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(description)
    return description


def enrich_batch(
    titles_metadata: dict[str, TmdbMetadata],
    cache_dir: str,
    client: anthropic.Anthropic,
) -> dict[str, str]:
    """Enrich a batch of titles. Falls back gracefully on individual failures."""
    result = {}
    for title, metadata in titles_metadata.items():
        result[title] = enrich(metadata, cache_dir, client)
    return result
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
python -m pytest tests/test_enricher.py -v
```

Expected: 7 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add recommender/enricher.py tests/test_enricher.py
git commit -m "feat: Claude Haiku title enricher with disk cache"
```

---

## Task 6: taste_profile_builder.py

**Files:**
- Create: `recommender/taste_profile_builder.py`
- Create: `tests/test_taste_profile_builder.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_taste_profile_builder.py`:
```python
from datetime import datetime, timedelta
from unittest.mock import MagicMock

from recommender.ingestion.base import WatchEvent
from recommender.taste_profile_builder import build


def make_event(title, series_name=None, content_type="movie", seconds=3600):
    return WatchEvent(
        platform="prime", title=title,
        content_type=content_type, series_name=series_name or title,
        watched_duration=timedelta(seconds=seconds),
        total_duration=None, timestamp=datetime(2024, 1, 1), profile="ADULT",
    )


def make_mock_client(response_text: str):
    client = MagicMock()
    msg = MagicMock()
    msg.content = [MagicMock(text=response_text)]
    client.messages.create.return_value = msg
    return client


def test_build_calls_sonnet():
    events = [make_event("DDLJ")]
    scores = {"DDLJ": 0.9}
    enrichments = {"DDLJ": "A classic Bollywood romance."}
    client = make_mock_client("You gravitate toward Bollywood romance.")
    result = build(events, scores, enrichments, client)
    call_kwargs = client.messages.create.call_args[1]
    assert call_kwargs["model"] == "claude-sonnet-4-6"


def test_build_returns_profile_text():
    events = [make_event("Downton Abbey", content_type="tv", series_name="Downton Abbey")]
    scores = {"Downton Abbey": 0.95}
    enrichments = {"Downton Abbey": "A lavish British period drama."}
    client = make_mock_client("You love British prestige dramas.")
    result = build(events, scores, enrichments, client)
    assert "British" in result


def test_build_includes_top_titles_in_prompt():
    events = [make_event("Show A"), make_event("Show B")]
    scores = {"Show A": 0.9, "Show B": 0.5}
    enrichments = {"Show A": "Desc A.", "Show B": "Desc B."}
    client = make_mock_client("Your profile.")
    build(events, scores, enrichments, client)
    prompt = client.messages.create.call_args[1]["messages"][0]["content"]
    assert "Show A" in prompt
    assert "0.90" in prompt or "0.9" in prompt


def test_build_skips_titles_with_no_enrichment():
    events = [make_event("Known"), make_event("Unknown")]
    scores = {"Known": 0.8, "Unknown": 0.7}
    enrichments = {"Known": "Known description."}
    client = make_mock_client("Your profile.")
    build(events, scores, enrichments, client)
    prompt = client.messages.create.call_args[1]["messages"][0]["content"]
    assert "Unknown" not in prompt or "Unknown description" not in prompt
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
python -m pytest tests/test_taste_profile_builder.py -v
```

Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Create recommender/taste_profile_builder.py**

```python
import anthropic

from .ingestion.base import WatchEvent


def build(
    events: list[WatchEvent],
    scores: dict[str, float],
    enrichments: dict[str, str],
    client: anthropic.Anthropic,
) -> str:
    """Build a natural-language taste profile using Claude Sonnet."""
    scored = sorted(
        [(title, score) for title, score in scores.items() if title in enrichments],
        key=lambda x: -x[1],
    )[:50]

    lines = [
        f"- {title} (score: {score:.2f}): {enrichments[title]}"
        for title, score in scored
    ]
    history_str = '\n'.join(lines)

    message = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=800,
        messages=[{
            "role": "user",
            "content": (
                "Analyze this person's streaming watch history and write a detailed taste profile.\n"
                "Identify distinct taste clusters, preferences for tone/pacing/culture, "
                "what they consistently finish, and notable patterns.\n"
                "Write in second person (\"You gravitate toward...\").\n\n"
                f"Watch history (sorted by engagement score):\n{history_str}"
            ),
        }],
    )
    return message.content[0].text.strip()
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
python -m pytest tests/test_taste_profile_builder.py -v
```

Expected: 4 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add recommender/taste_profile_builder.py tests/test_taste_profile_builder.py
git commit -m "feat: Claude Sonnet natural-language taste profile builder"
```

---

## Task 7: query_engine.py — QueryIntent and parse_intent

**Files:**
- Create: `recommender/query_engine.py` (partial — QueryIntent + parse_intent only)
- Create: `tests/test_query_engine.py` (partial)

- [ ] **Step 1: Write the failing tests**

Create `tests/test_query_engine.py`:
```python
import json
from unittest.mock import MagicMock

from recommender.query_engine import QueryIntent, parse_intent


def make_intent_client(intent_dict: dict):
    client = MagicMock()
    msg = MagicMock()
    msg.content = [MagicMock(text=json.dumps(intent_dict))]
    client.messages.create.return_value = msg
    return client


BRITISH_CRIME_INTENT = {
    "genres": ["crime", "drama"],
    "origin_countries": ["GB"],
    "languages": [],
    "mood_descriptors": ["slow-burn"],
    "similar_to": [],
    "max_runtime_minutes": None,
    "year_from": None,
    "year_to": None,
    "unwatched_only": True,
    "special_intent": None,
    "content_type": "tv",
    "top_n": 1,
}


def test_parse_intent_returns_query_intent():
    client = make_intent_client(BRITISH_CRIME_INTENT)
    intent = parse_intent("good British crime drama", client)
    assert isinstance(intent, QueryIntent)


def test_parse_intent_extracts_genres():
    client = make_intent_client(BRITISH_CRIME_INTENT)
    intent = parse_intent("good British crime drama", client)
    assert "crime" in intent.genres
    assert "drama" in intent.genres


def test_parse_intent_extracts_origin_country():
    client = make_intent_client(BRITISH_CRIME_INTENT)
    intent = parse_intent("good British crime drama", client)
    assert "GB" in intent.origin_countries


def test_parse_intent_uses_sonnet():
    client = make_intent_client(BRITISH_CRIME_INTENT)
    parse_intent("good British crime drama", client)
    call_kwargs = client.messages.create.call_args[1]
    assert call_kwargs["model"] == "claude-sonnet-4-6"


def test_parse_intent_handles_markdown_wrapped_json():
    client = MagicMock()
    msg = MagicMock()
    msg.content = [MagicMock(text="```json\n" + json.dumps(BRITISH_CRIME_INTENT) + "\n```")]
    client.messages.create.return_value = msg
    intent = parse_intent("good British crime drama", client)
    assert intent.genres == ["crime", "drama"]


def test_parse_intent_abandoned_special():
    abandoned_intent = {**BRITISH_CRIME_INTENT, "special_intent": "abandoned",
                        "genres": [], "origin_countries": [],
                        "similar_to": ["Tandav"], "content_type": "tv", "top_n": 1}
    client = make_intent_client(abandoned_intent)
    intent = parse_intent("I started Tandav and stopped", client)
    assert intent.special_intent == "abandoned"
    assert "Tandav" in intent.similar_to


def test_parse_intent_bollywood():
    bollywood_intent = {
        "genres": ["romance"], "origin_countries": ["IN"],
        "languages": ["hi"], "mood_descriptors": ["feel-good"],
        "similar_to": [], "max_runtime_minutes": None,
        "year_from": 1990, "year_to": 1999,
        "unwatched_only": True, "special_intent": None, "content_type": "movie",
        "top_n": 1,
    }
    client = make_intent_client(bollywood_intent)
    intent = parse_intent("feel-good Bollywood romance from the 90s", client)
    assert "hi" in intent.languages
    assert intent.year_from == 1990
    assert intent.content_type == "movie"
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
python -m pytest tests/test_query_engine.py -v
```

Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Create recommender/query_engine.py with QueryIntent and parse_intent**

```python
import json
import re
from dataclasses import dataclass

import anthropic

from .ingestion.base import WatchEvent
from .models import Recommendation
from .tmdb_client import TmdbClient, TmdbMetadata


@dataclass
class QueryIntent:
    genres: list[str]
    origin_countries: list[str]
    languages: list[str]
    mood_descriptors: list[str]
    similar_to: list[str]
    max_runtime_minutes: int | None
    year_from: int | None
    year_to: int | None
    unwatched_only: bool
    special_intent: str | None  # "abandoned", "watchlist", "family", or None
    content_type: str           # "tv", "movie", or "both"
    top_n: int                  # 1 for single recommendation, >1 for "a few" queries


@dataclass
class RecommendContext:
    taste_profile: str
    watch_index: "WatchIndex"
    events: list[WatchEvent]
    tmdb_client: TmdbClient
    anthropic_client: anthropic.Anthropic
    cache_dir: str


def _parse_json_response(text: str) -> dict | list:
    text = text.strip()
    if text.startswith('```'):
        text = re.sub(r'^```(?:json)?\n?', '', text)
        text = re.sub(r'\n?```$', '', text)
    return json.loads(text.strip())


def parse_intent(query: str, client: anthropic.Anthropic) -> QueryIntent:
    """Parse a natural language query into structured intent using Claude Sonnet."""
    message = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=400,
        messages=[{
            "role": "user",
            "content": (
                'Parse this streaming recommendation query into structured intent. '
                'Return ONLY valid JSON with these fields:\n'
                '- genres: list of genre strings e.g. ["crime", "drama"]\n'
                '- origin_countries: list of ISO-3166 alpha-2 codes e.g. ["GB", "IN"]\n'
                '- languages: list of ISO-639-1 codes e.g. ["hi", "en"]\n'
                '- mood_descriptors: list of mood/tone words e.g. ["slow-burn", "feel-good"]\n'
                '- similar_to: list of title names for similarity search\n'
                '- max_runtime_minutes: integer or null\n'
                '- year_from: integer or null\n'
                '- year_to: integer or null\n'
                '- unwatched_only: boolean (default true)\n'
                '- special_intent: one of "abandoned", "watchlist", "family" or null\n'
                '- content_type: "tv", "movie", or "both"\n'
                '- top_n: integer — 1 for single recommendation (default), 3-5 if query implies '
                '"a few" or "some options" or "what should I watch"\n\n'
                f'Query: "{query}"'
            ),
        }],
    )
    data = _parse_json_response(message.content[0].text)
    return QueryIntent(**data)
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
python -m pytest tests/test_query_engine.py -v
```

Expected: 8 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add recommender/query_engine.py tests/test_query_engine.py
git commit -m "feat: QueryIntent dataclass and Claude Sonnet intent parser"
```

---

## Task 8: query_engine.py — ask() and rank_candidates()

**Files:**
- Modify: `recommender/query_engine.py` (add `ask`, `rank_candidates`, `_handle_abandoned`)
- Modify: `tests/test_query_engine.py` (add ranking and ask tests)

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_query_engine.py`:
```python
from datetime import timedelta
from datetime import datetime

from recommender.tmdb_client import TmdbMetadata
from recommender.watch_index import WatchIndex
from recommender.query_engine import RecommendContext, ask, rank_candidates


def make_meta(title, tmdb_id=1, content_type="tv", genres=None, vote_avg=8.0):
    return TmdbMetadata(
        tmdb_id=tmdb_id, content_type=content_type, title=title,
        genres=genres or ["Crime", "Drama"], keywords=[],
        cast=[], creator_or_director=None,
        original_language="en", vote_average=vote_avg, vote_count=500,
    )


def make_rank_client(ranked_list: list):
    client = MagicMock()
    msg = MagicMock()
    msg.content = [MagicMock(text=json.dumps(ranked_list))]
    client.messages.create.return_value = msg
    return client


def test_rank_candidates_returns_recommendations():
    candidates = [make_meta("Broadchurch", tmdb_id=1), make_meta("Hinterland", tmdb_id=2)]
    enrichments = {"Broadchurch": "Dark coastal crime.", "Hinterland": "Welsh noir."}
    ranked = [
        {"title": "Broadchurch", "explanation": "Fits your taste.", "score": 0.92},
        {"title": "Hinterland", "explanation": "Similar tone.", "score": 0.85},
    ]
    client = make_rank_client(ranked)
    results = rank_candidates("British crime drama", "taste profile", candidates, enrichments, client)
    assert len(results) == 2
    assert results[0].title == "Broadchurch"
    assert results[0].score == 0.92
    assert "Fits your taste" in results[0].explanation
    assert results[0].content_type == "tv"
    assert results[0].vote_average == 8.0


def test_rank_candidates_uses_sonnet():
    candidates = [make_meta("Broadchurch")]
    enrichments = {"Broadchurch": "Desc."}
    ranked = [{"title": "Broadchurch", "explanation": "Good.", "score": 0.9}]
    client = make_rank_client(ranked)
    rank_candidates("query", "profile", candidates, enrichments, client)
    call_kwargs = client.messages.create.call_args[1]
    assert call_kwargs["model"] == "claude-sonnet-4-6"


def test_rank_candidates_skips_unknown_titles():
    candidates = [make_meta("Broadchurch")]
    enrichments = {"Broadchurch": "Desc."}
    ranked = [
        {"title": "Broadchurch", "explanation": "Good.", "score": 0.9},
        {"title": "Phantom Title", "explanation": "Hallucinated.", "score": 0.95},
    ]
    client = make_rank_client(ranked)
    results = rank_candidates("query", "profile", candidates, enrichments, client)
    titles = [r.title for r in results]
    assert "Phantom Title" not in titles
    assert "Broadchurch" in titles


def test_ask_excludes_watched_titles():
    meta_watched = make_meta("Broadchurch", tmdb_id=1)
    meta_new = make_meta("Hinterland", tmdb_id=2)

    mock_tmdb = MagicMock()
    mock_tmdb.search_by_filters.return_value = [meta_watched, meta_new]
    mock_tmdb.get_metadata.return_value = None

    intent_json = json.dumps({
        "genres": ["crime"], "origin_countries": ["GB"], "languages": [],
        "mood_descriptors": [], "similar_to": [], "max_runtime_minutes": None,
        "year_from": None, "year_to": None,
        "unwatched_only": True, "special_intent": None, "content_type": "tv",
        "top_n": 1,
    })
    ranked_json = json.dumps([
        {"title": "Hinterland", "explanation": "Great fit.", "score": 0.88}
    ])

    mock_anthropic = MagicMock()
    intent_msg = MagicMock()
    intent_msg.content = [MagicMock(text=intent_json)]
    rank_msg = MagicMock()
    rank_msg.content = [MagicMock(text=ranked_json)]
    mock_anthropic.messages.create.side_effect = [intent_msg, rank_msg]

    ctx = RecommendContext(
        taste_profile="taste profile text",
        watch_index=WatchIndex(tmdb_ids={1}, normalized_titles={"broadchurch"}, entries=[]),
        events=[],
        tmdb_client=mock_tmdb,
        anthropic_client=mock_anthropic,
        cache_dir="/tmp/test_cache",
    )

    with patch('recommender.query_engine.enrich_batch', return_value={"Hinterland": "Welsh noir."}):
        results = ask("British crime drama", ctx)

    titles = [r.title for r in results]
    assert "Broadchurch" not in titles
    assert "Hinterland" in titles
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
python -m pytest tests/test_query_engine.py -v -k "rank_candidates or test_ask"
```

Expected: FAIL with `ImportError: cannot import name 'ask'`

- [ ] **Step 3: Add rank_candidates, _handle_abandoned, and ask to recommender/query_engine.py**

Add this import at the top of the file:
```python
from unittest.mock import patch  # remove this — it's in tests only
```

Add these functions to `recommender/query_engine.py`:
```python
from .enricher import enrich, enrich_batch
from .watch_index import WatchIndex


def rank_candidates(
    query: str,
    taste_profile: str,
    candidates: list[TmdbMetadata],
    enrichments: dict[str, str],
    client: anthropic.Anthropic,
    top_n: int = 1,
) -> list[Recommendation]:
    """Rank candidates against taste profile using Claude Sonnet."""
    meta_by_title = {c.title: c for c in candidates}

    cands_str = '\n'.join(
        f"{i+1}. {c.title} (rating: {c.vote_average:.1f}): {enrichments.get(c.title, ' '.join(c.genres))}"
        for i, c in enumerate(candidates)
    )

    message = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1000,
        messages=[{
            "role": "user",
            "content": (
                f'Given this taste profile and query, rank the candidates and explain why each fits.\n\n'
                f'TASTE PROFILE:\n{taste_profile}\n\n'
                f'QUERY: "{query}"\n\n'
                f'CANDIDATES:\n{cands_str}\n\n'
                f'Return ONLY valid JSON: a list of objects with fields:\n'
                f'- title: string (exact title from candidates)\n'
                f'- explanation: string (1-2 sentences why this fits this specific user)\n'
                f'- score: float 0-1\n\n'
                f'Return the top {top_n} ranked candidates.'
            ),
        }],
    )

    ranked = _parse_json_response(message.content[0].text)
    results = []
    for item in ranked:
        title = item['title']
        if title not in meta_by_title:
            continue
        meta = meta_by_title[title]
        results.append(Recommendation(
            title=title,
            content_type=meta.content_type,
            score=item['score'],
            vote_average=meta.vote_average,
            genres=meta.genres,
            explanation=item['explanation'],
        ))
    return results


def _handle_abandoned(query: str, intent: QueryIntent, ctx: RecommendContext) -> list[Recommendation]:
    """Handle 'I started X and stopped — worth finishing?' queries."""
    target = intent.similar_to[0] if intent.similar_to else query

    matching = [
        e for e in ctx.events
        if target.lower() in e.title.lower() or target.lower() in e.series_name.lower()
    ]
    if not matching:
        return []

    total_hours = sum(e.watched_duration.total_seconds() for e in matching) / 3600
    ct = matching[0].content_type
    lookup_title = matching[0].series_name if ct == 'tv' else matching[0].title
    meta = ctx.tmdb_client.get_metadata(lookup_title, ct)
    desc = enrich(meta, ctx.cache_dir, ctx.anthropic_client) if meta else target

    message = ctx.anthropic_client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=300,
        messages=[{
            "role": "user",
            "content": (
                f'A user has watched {total_hours:.1f} hours of "{target}".\n\n'
                f'About "{target}": {desc}\n\n'
                f'Their taste profile:\n{ctx.taste_profile}\n\n'
                'Should they continue watching? Give a direct yes/no with 1-2 sentences of reasoning.'
            ),
        }],
    )

    return [Recommendation(
        title=target,
        content_type=ct,
        score=1.0,
        vote_average=meta.vote_average if meta else 0.0,
        genres=meta.genres if meta else [],
        explanation=message.content[0].text.strip(),
    )]


def ask(query: str, ctx: RecommendContext) -> list[Recommendation]:
    """Answer a natural language recommendation query end-to-end."""
    intent = parse_intent(query, ctx.anthropic_client)

    if intent.special_intent == 'abandoned':
        return _handle_abandoned(query, intent, ctx)

    content_types = ['tv', 'movie'] if intent.content_type == 'both' else [intent.content_type]
    candidates: list[TmdbMetadata] = []
    for ct in content_types:
        candidates.extend(ctx.tmdb_client.search_by_filters(
            content_type=ct,
            genres=intent.genres,
            origin_countries=intent.origin_countries,
            languages=intent.languages,
            year_from=intent.year_from,
            year_to=intent.year_to,
            size=30,
        ))

    candidates = [c for c in candidates if not ctx.watch_index.is_watched(c)]

    if len(candidates) < 10:
        for title in _generate_suggestions(query, ctx.taste_profile, ctx.anthropic_client):
            ct = intent.content_type if intent.content_type != 'both' else 'movie'
            meta = ctx.tmdb_client.get_metadata(title, ct)
            if meta and not ctx.watch_index.is_watched(meta):
                candidates.append(meta)

    if not candidates:
        return []

    meta_dict = {c.title: c for c in candidates}
    enrichments = enrich_batch(meta_dict, ctx.cache_dir, ctx.anthropic_client)

    return rank_candidates(query, ctx.taste_profile, candidates, enrichments, ctx.anthropic_client, intent.top_n)


def _generate_suggestions(
    query: str,
    taste_profile: str,
    client: anthropic.Anthropic,
) -> list[str]:
    """Ask Claude to suggest specific titles when TMDB returns too few candidates."""
    message = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=300,
        messages=[{
            "role": "user",
            "content": (
                f'A user is looking for: "{query}"\n\n'
                f'Their taste profile:\n{taste_profile}\n\n'
                'Suggest 20 specific titles that fit the query and taste profile. '
                'Return ONLY a JSON array of title strings. Be precise with names.'
            ),
        }],
    )
    result = _parse_json_response(message.content[0].text)
    return result if isinstance(result, list) else []
```

- [ ] **Step 4: Add the missing import to tests/test_query_engine.py**

At the top of `tests/test_query_engine.py`, ensure these imports are present:
```python
import json
from unittest.mock import MagicMock, patch
from recommender.query_engine import QueryIntent, parse_intent, RecommendContext, ask, rank_candidates
```

- [ ] **Step 5: Run all query_engine tests**

```bash
python -m pytest tests/test_query_engine.py -v
```

Expected: all 15 tests PASS.

- [ ] **Step 6: Commit**

```bash
git add recommender/query_engine.py tests/test_query_engine.py
git commit -m "feat: ask() and rank_candidates() for end-to-end query handling"
```

---

## Task 9: setup.py — offline orchestration

**Files:**
- Create: `recommender/setup.py`

- [ ] **Step 1: Create recommender/setup.py**

```python
import argparse
from pathlib import Path

import anthropic

import config
from recommender.ingestion.netflix import parse as parse_netflix
from recommender.ingestion.prime import parse as parse_prime
from recommender.signals import compute_scores
from recommender.tmdb_client import TmdbClient
from recommender.enricher import enrich_batch
from recommender.taste_profile_builder import build as build_taste_profile
from recommender import watch_index as wi


def run_setup(refresh_profile: bool = False) -> None:
    print("Loading watch history...")
    events = []
    for platform, parser in [("netflix", parse_netflix), ("prime", parse_prime)]:
        path = config.PLATFORM_PATHS.get(platform)
        if path:
            platform_events = parser(path)
            events.extend(platform_events)
            print(f"  {platform}: {len(platform_events)} events")
    print(f"  Total: {len(events)} events")

    print("\nFetching TMDB metadata...")
    tmdb = TmdbClient(api_key=config.TMDB_API_KEY, cache_dir=config.CACHE_DIR)
    scores = compute_scores(events, {}, config.RECENCY_HALF_LIFE_DAYS)

    title_type: dict[str, str] = {}
    for e in events:
        key = e.series_name if e.content_type == 'tv' else e.title
        title_type[key] = e.content_type

    metadata = {}
    for i, (title, ct) in enumerate(title_type.items()):
        meta = tmdb.get_metadata(title, ct)
        if meta:
            metadata[title] = meta
        if (i + 1) % 50 == 0:
            print(f"  {i+1}/{len(title_type)} titles processed...")
    print(f"  {len(metadata)} titles with TMDB metadata")

    print("\nBuilding watch index...")
    index = wi.build(events, metadata)
    wi.save(index, config.WATCH_INDEX_PATH)
    print(f"  {len(index.entries)} unique titles indexed → {config.WATCH_INDEX_PATH}")

    print(f"\nEnriching {len(metadata)} titles with Claude Haiku...")
    claude = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
    enrichments = enrich_batch(metadata, config.ENRICHMENT_CACHE_DIR, claude)
    print(f"  {len(enrichments)} descriptions cached → {config.ENRICHMENT_CACHE_DIR}")

    profile_path = Path(config.TASTE_PROFILE_PATH)
    if refresh_profile or not profile_path.exists():
        print("\nBuilding taste profile with Claude Sonnet...")
        profile = build_taste_profile(events, scores, enrichments, claude)
        profile_path.parent.mkdir(parents=True, exist_ok=True)
        profile_path.write_text(profile)
        print(f"  Taste profile saved → {config.TASTE_PROFILE_PATH}")
    else:
        print(f"\nTaste profile exists, skipping (use --refresh-profile to rebuild).")

    print("\nSetup complete!")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run offline setup for the recommender")
    parser.add_argument("--refresh-profile", action="store_true",
                        help="Rebuild taste profile even if it exists")
    args = parser.parse_args()
    run_setup(refresh_profile=args.refresh_profile)
```

- [ ] **Step 2: Verify the module is importable**

```bash
python -c "from recommender.setup import run_setup; print('OK')"
```

Expected: prints `OK`

- [ ] **Step 3: Commit**

```bash
git add recommender/setup.py
git commit -m "feat: offline setup orchestration (enrich + taste profile + watch index)"
```

---

## Task 10: Rewrite main.py and update tests

**Files:**
- Rewrite: `recommender/main.py`
- Rewrite: `tests/test_main.py`

- [ ] **Step 1: Write the failing tests**

Replace the full contents of `tests/test_main.py`:
```python
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from recommender.models import Recommendation


def make_rec(title="Broadchurch", explanation="Great fit."):
    return Recommendation(
        title=title, content_type="tv", score=0.9,
        vote_average=8.4, genres=["Crime", "Drama"],
        explanation=explanation,
    )


def test_print_recommendations_shows_title(capsys):
    from recommender.main import print_recommendations
    results = [make_rec("Broadchurch", "Fits your British crime taste.")]
    print_recommendations(results, "British crime drama")
    out = capsys.readouterr().out
    assert "Broadchurch" in out
    assert "Fits your British crime taste" in out


def test_print_recommendations_empty(capsys):
    from recommender.main import print_recommendations
    print_recommendations([], "query")
    out = capsys.readouterr().out
    assert "No recommendations" in out


def test_main_exits_without_anthropic_key(monkeypatch):
    monkeypatch.setattr(sys, 'argv', ['recommender', 'test query'])
    import config
    original = config.ANTHROPIC_API_KEY
    config.ANTHROPIC_API_KEY = ""
    try:
        with pytest.raises(SystemExit) as exc_info:
            from recommender import main as main_module
            import importlib
            importlib.reload(main_module)
            main_module.main()
        assert exc_info.value.code == 1
    finally:
        config.ANTHROPIC_API_KEY = original


def test_load_context_exits_if_no_watch_index(tmp_path, monkeypatch):
    import config
    monkeypatch.setattr(config, 'WATCH_INDEX_PATH', str(tmp_path / 'missing.json'))
    monkeypatch.setattr(config, 'TASTE_PROFILE_PATH', str(tmp_path / 'profile.txt'))
    with pytest.raises(SystemExit):
        from recommender.main import load_context
        load_context()


def test_load_context_exits_if_no_taste_profile(tmp_path, monkeypatch):
    import config
    import json
    index_path = tmp_path / 'watch_index.json'
    index_path.write_text(json.dumps([{"tmdb_id": 0, "title": "downton abbey", "content_type": "tv"}]))
    monkeypatch.setattr(config, 'WATCH_INDEX_PATH', str(index_path))
    monkeypatch.setattr(config, 'TASTE_PROFILE_PATH', str(tmp_path / 'missing.txt'))
    with pytest.raises(SystemExit):
        from recommender.main import load_context
        load_context()
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
python -m pytest tests/test_main.py -v
```

Expected: some FAIL (print_recommendations doesn't exist in new main yet)

- [ ] **Step 3: Rewrite recommender/main.py**

```python
import sys
from pathlib import Path

import anthropic

import config
from recommender.ingestion.netflix import parse as parse_netflix
from recommender.ingestion.prime import parse as parse_prime
from recommender.models import Recommendation
from recommender.tmdb_client import TmdbClient
from recommender.query_engine import RecommendContext, ask
from recommender import watch_index as wi


def load_context() -> RecommendContext:
    events = []
    for platform, parser in [("netflix", parse_netflix), ("prime", parse_prime)]:
        path = config.PLATFORM_PATHS.get(platform)
        if path:
            events.extend(parser(path))

    index_path = Path(config.WATCH_INDEX_PATH)
    if not index_path.exists():
        print("Watch index not found. Run: python -m recommender.setup", file=sys.stderr)
        sys.exit(1)

    profile_path = Path(config.TASTE_PROFILE_PATH)
    if not profile_path.exists():
        print("Taste profile not found. Run: python -m recommender.setup", file=sys.stderr)
        sys.exit(1)

    return RecommendContext(
        taste_profile=profile_path.read_text(),
        watch_index=wi.load(config.WATCH_INDEX_PATH),
        events=events,
        tmdb_client=TmdbClient(api_key=config.TMDB_API_KEY, cache_dir=config.CACHE_DIR),
        anthropic_client=anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY),
        cache_dir=config.ENRICHMENT_CACHE_DIR,
    )


def print_recommendations(results: list[Recommendation], query: str) -> None:
    if not results:
        print("No recommendations found.")
        return
    print(f'\nResults for: "{query}"\n')
    for i, rec in enumerate(results, 1):
        print(f"{i}. {rec.title}  ★ {rec.vote_average:.1f}  [{', '.join(rec.genres[:3])}]")
        print(f"   {rec.explanation}")
        print()


def main() -> None:
    if not config.ANTHROPIC_API_KEY:
        print("Error: ANTHROPIC_API_KEY not set.", file=sys.stderr)
        sys.exit(1)
    if not config.TMDB_API_KEY:
        print("Error: TMDB_API_KEY not set.", file=sys.stderr)
        sys.exit(1)

    ctx = load_context()

    if len(sys.argv) > 1:
        query = " ".join(sys.argv[1:])
        results = ask(query, ctx)
        print_recommendations(results, query)
        return

    print("Streaming Recommender — ask me anything about what to watch.")
    print('Type "exit" to quit.\n')
    while True:
        try:
            query = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not query or query.lower() == "exit":
            break
        results = ask(query, ctx)
        print_recommendations(results, query)


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run all tests**

```bash
python -m pytest tests/ -v
```

Expected: all tests PASS (the 1 previously failing prime stub test is now gone).

- [ ] **Step 5: Commit**

```bash
git add recommender/main.py tests/test_main.py
git commit -m "feat: query-driven CLI — single-shot and interactive modes"
```

---

## Self-Review

**Spec coverage check:**

| Spec requirement | Task |
|-----------------|------|
| Delete engine.py, taste_profile.py | Task 1 |
| models.py with Recommendation.explanation | Task 2 |
| config.py ANTHROPIC_API_KEY + new paths | Task 2 |
| watch_index.py build/save/load/is_watched | Task 3 |
| tmdb_client search_by_filters with genre maps | Task 4 |
| enricher.py Claude Haiku + disk cache | Task 5 |
| taste_profile_builder.py Claude Sonnet | Task 6 |
| query_engine.py QueryIntent + parse_intent | Task 7 |
| query_engine.py ask() + rank_candidates() | Task 8 |
| Fallback when <10 candidates | Task 8 (_generate_suggestions) |
| Abandoned intent routing | Task 8 (_handle_abandoned) |
| setup.py offline orchestration | Task 9 |
| main.py single-shot + interactive | Task 10 |
| ANTHROPIC_API_KEY from env (already in ~/.bashrc) | Task 2 |
| Enrichment fallback on API error | Task 5 |
| Unknown title slug cache path | Task 5 |

All spec requirements covered. No placeholders found. Types consistent across tasks (`RecommendContext` defined in Task 7, used in Task 8 and 10; `enrich_batch` signature defined in Task 5, used in Task 8).
