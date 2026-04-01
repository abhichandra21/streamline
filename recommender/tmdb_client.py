import json
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path

import requests

TMDB_BASE = "https://api.themoviedb.org/3"

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


@dataclass
class TmdbMetadata:
    tmdb_id: int
    content_type: str       # "tv" or "movie"
    title: str
    genres: list[str] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)
    cast: list[str] = field(default_factory=list)
    creator_or_director: str | None = None
    original_language: str = ""
    vote_average: float = 0.0
    vote_count: int = 0
    runtime_minutes: int | None = None


class TmdbClient:
    def __init__(self, api_key: str, cache_dir: str = "recommender/cache/tmdb"):
        self.api_key = api_key
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def _get(self, endpoint: str, params: dict | None = None) -> dict:
        p = {"api_key": self.api_key}
        if params:
            p.update(params)
        resp = requests.get(f"{TMDB_BASE}/{endpoint}", params=p, timeout=10)
        resp.raise_for_status()
        return resp.json()

    def _cache_path(self, content_type: str, tmdb_id: int) -> Path:
        return self.cache_dir / content_type / f"{tmdb_id}.json"

    def _load_cache(self, content_type: str, tmdb_id: int) -> dict | None:
        path = self._cache_path(content_type, tmdb_id)
        if path.exists():
            return json.loads(path.read_text())
        return None

    def _save_cache(self, content_type: str, tmdb_id: int, data: dict) -> None:
        path = self._cache_path(content_type, tmdb_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data))

    def _search(self, title: str, content_type: str) -> int | None:
        endpoint = "search/tv" if content_type == "tv" else "search/movie"
        data = self._get(endpoint, {"query": title})
        results = data.get("results", [])
        return results[0]["id"] if results else None

    def _fetch_details(self, tmdb_id: int, content_type: str) -> dict:
        endpoint = f"tv/{tmdb_id}" if content_type == "tv" else f"movie/{tmdb_id}"
        return self._get(endpoint, {"append_to_response": "keywords,credits"})

    def _parse_metadata(self, data: dict, content_type: str) -> TmdbMetadata:
        title = data.get("name") or data.get("title", "")
        genres = [g["name"] for g in data.get("genres", [])]

        if content_type == "tv":
            kws = data.get("keywords", {}).get("results", [])
        else:
            kws = data.get("keywords", {}).get("keywords", [])
        keywords = [k["name"] for k in kws[:10]]

        credits = data.get("credits", {})
        cast = [c["name"] for c in credits.get("cast", [])[:5]]

        creator_or_director = None
        if content_type == "tv":
            created_by = data.get("created_by", [])
            if created_by:
                creator_or_director = created_by[0]["name"]
        else:
            crew = credits.get("crew", [])
            directors = [c["name"] for c in crew if c.get("job") == "Director"]
            if directors:
                creator_or_director = directors[0]

        if content_type == "tv":
            runtimes = data.get("episode_run_time", [])
            runtime_minutes = runtimes[0] if runtimes else None
        else:
            runtime_minutes = data.get("runtime")

        return TmdbMetadata(
            tmdb_id=data["id"],
            content_type=content_type,
            title=title,
            genres=genres,
            keywords=keywords,
            cast=cast,
            creator_or_director=creator_or_director,
            original_language=data.get("original_language", ""),
            vote_average=data.get("vote_average", 0.0),
            vote_count=data.get("vote_count", 0),
            runtime_minutes=runtime_minutes,
        )

    def get_metadata(self, title: str, content_type: str) -> TmdbMetadata | None:
        """Fetch and cache metadata for a single title. Returns None if not found."""
        tmdb_id = self._search(title, content_type)
        if tmdb_id is None:
            return None
        cached = self._load_cache(content_type, tmdb_id)
        if cached:
            return self._parse_metadata(cached, content_type)
        data = self._fetch_details(tmdb_id, content_type)
        self._save_cache(content_type, tmdb_id, data)
        return self._parse_metadata(data, content_type)

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

    def get_candidates(self, content_type: str, size: int = 500) -> list[TmdbMetadata]:
        """
        Fetch top-rated and popular titles as the recommendation candidate pool.
        Fetches full details for each, using cache where available.
        """
        prefix = "tv" if content_type == "tv" else "movie"
        candidates: dict[int, TmdbMetadata] = {}
        pages_per_list = max(1, size // 40)  # 20 results/page x 2 lists

        for list_type in ("top_rated", "popular"):
            for page in range(1, pages_per_list + 1):
                data = self._get(f"{prefix}/{list_type}", {"page": page})
                for item in data.get("results", []):
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
                            time.sleep(0.05)  # respect TMDB rate limits
                        except Exception:
                            continue
                    if len(candidates) >= size:
                        return list(candidates.values())

        return list(candidates.values())

    def clear_cache(self) -> None:
        """Delete all cached TMDB responses."""
        if self.cache_dir.exists():
            shutil.rmtree(self.cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
