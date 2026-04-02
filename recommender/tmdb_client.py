import json
import logging
import re
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path

import requests

log = logging.getLogger("recommender.tmdb")

MAX_DISCOVER_PAGES = 20

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
    'kids': 10762, 'mystery': 9648, 'sci-fi': 10765, 'thriller': 9648,
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

    def _clean_title_variants(self, title: str) -> list[str]:
        """Generate cleaned title variants for TMDB search fallback.

        Uses guessit for smart title extraction, plus custom patterns
        for streaming platform naming conventions.
        """
        from guessit import guessit as guess

        variants = [title]
        seen = {title}

        def _add(v: str) -> None:
            v = v.strip()
            if v and len(v) > 2 and v not in seen:
                variants.append(v)
                seen.add(v)

        # 1. guessit extraction — best for structured titles with episode/season markers
        try:
            info = guess(title)
            gi_title = info.get('title', '')
            if gi_title and gi_title != title:
                _add(gi_title)
        except Exception:
            pass

        # 2. Strip parenthetical suffixes (platform noise: UHD, Subtitled, etc.)
        stripped = re.sub(r'\s*\([^)]*(?:Subtitl|UHD|Hindi|English|Narrat|4K|Dubbed|Version|Edition|UNRATED|REPACK|Theatrical)[^)]*\)\s*$', '', title, flags=re.IGNORECASE).strip()
        _add(stripped)

        # 3. Strip non-parenthetical edition/version suffixes
        stripped_edition = re.sub(r'\s+(?:Theatrical|Directors?\s*Cut|Unrated|Repack|Extended|Special)\s*(?:Edition|Version)?\s*$', '', stripped, flags=re.IGNORECASE).strip()
        _add(stripped_edition)

        # 4. Strip year suffix like (2004)
        stripped_year = re.sub(r'\s*\(\d{4}\)\s*$', '', title).strip()
        _add(stripped_year)

        # 5. After last hyphen (Prime: "EpisodeName-SeriesName")
        if '-' in title:
            after_hyphen = title.rsplit('-', 1)[1].strip()
            after_hyphen = re.sub(r'\s+S\d+\s*$', '', after_hyphen).strip()
            _add(after_hyphen)

        # 6. Before first colon (Netflix: "Show: Season: Episode")
        if ':' in title:
            before_colon = title.split(':')[0].strip()
            if not re.match(r'^Episode\s+\d+$', before_colon, re.IGNORECASE):
                _add(before_colon)

        return variants

    def classify_title(self, title: str) -> tuple[str, str]:
        """Use guessit to detect if a title is a TV episode and extract the series name.

        Returns (content_type, clean_title). If guessit detects an episode,
        returns ('tv', series_name). Otherwise returns the original classification.
        """
        from guessit import guessit as guess

        try:
            info = guess(title)
            if info.get('type') == 'episode':
                gi_title = info.get('title', '')
                if gi_title and len(gi_title) > 2:
                    return 'tv', gi_title
        except Exception:
            pass
        return '', title

    def get_metadata(self, title: str, content_type: str) -> TmdbMetadata | None:
        """Fetch and cache metadata for a single title. Returns None if not found.

        Uses guessit to detect misclassified episodes and tries cleaned
        title variants if the original search fails.
        """
        # Let guessit override content_type if it detects an episode
        detected_type, detected_title = self.classify_title(title)
        if detected_type and detected_type != content_type:
            log.debug("guessit reclassified %r: %s -> %s (title: %r)",
                       title, content_type, detected_type, detected_title)
            content_type = detected_type

        alt_type = "tv" if content_type == "movie" else "movie"
        for variant in self._clean_title_variants(title):
            # Try the requested content type first
            tmdb_id = self._search(variant, content_type)
            if tmdb_id is not None:
                if variant != title:
                    log.debug("TMDB search hit via variant: %r -> %r -> ID %d", title, variant, tmdb_id)
                else:
                    log.debug("TMDB search hit: %r -> ID %d", title, tmdb_id)
                break
            # Try the alternate content type (misclassified episodes, etc.)
            tmdb_id = self._search(variant, alt_type)
            if tmdb_id is not None:
                content_type = alt_type
                log.debug("TMDB search hit via alt type: %r -> %r (%s) -> ID %d", title, variant, alt_type, tmdb_id)
                break
        else:
            log.debug("TMDB search miss (all variants): %r (%s)", title, content_type)
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
        log.debug("Discover: type=%s genres=%s countries=%s languages=%s years=%s-%s size=%d",
                   content_type, genres, origin_countries, languages, year_from, year_to, size)
        prefix = "tv" if content_type == "tv" else "movie"
        genre_map = TV_GENRE_IDS if content_type == "tv" else MOVIE_GENRE_IDS
        date_field = "first_air_date" if content_type == "tv" else "primary_release_date"

        params: dict = {"sort_by": "vote_average.desc", "vote_count.gte": 100}

        if genres:
            ids = [str(genre_map[g.lower()]) for g in genres if g.lower() in genre_map]
            unmapped = [g for g in genres if g.lower() not in genre_map]
            if unmapped:
                log.debug("Unmapped genres (not in TMDB genre map): %s", unmapped)
            if ids:
                params["with_genres"] = ",".join(ids)
                log.debug("Genre filter: %s -> TMDB IDs %s", genres, ids)

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
        while len(candidates) < size and page <= MAX_DISCOVER_PAGES:
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
                    except Exception as exc:
                        log.warning("TMDB fetch failed for ID %d: %s", tmdb_id, exc)
                        continue
                if len(candidates) >= size:
                    break
            page += 1

        log.debug("Discover returned %d candidates after %d pages", len(candidates), page - 1)
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
                        except Exception as exc:
                            log.warning("TMDB fetch failed for ID %d: %s", tmdb_id, exc)
                            continue
                    if len(candidates) >= size:
                        return list(candidates.values())

        return list(candidates.values())

    def get_watch_providers(
        self,
        tmdb_id: int,
        content_type: str,
        region: str,
        providers_cache_dir: str,
    ) -> list[str]:
        """Return flatrate (subscription) streaming provider names for a title in the given region.

        Results are cached in providers_cache_dir to avoid repeated API calls.
        Returns an empty list if no provider data is available.
        """
        cache_path = Path(providers_cache_dir) / content_type / region / f"{tmdb_id}.json"
        if cache_path.exists():
            cached = json.loads(cache_path.read_text())
            return cached.get("providers", [])

        endpoint = f"tv/{tmdb_id}/watch/providers" if content_type == "tv" else f"movie/{tmdb_id}/watch/providers"
        try:
            data = self._get(endpoint)
        except Exception as exc:
            log.debug("Watch providers fetch failed for %s/%d: %s", content_type, tmdb_id, exc)
            return []

        region_data = data.get("results", {}).get(region, {})
        flatrate = region_data.get("flatrate", [])
        providers = [p["provider_name"] for p in flatrate]

        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(json.dumps({"providers": providers}))
        return providers

    def clear_cache(self) -> None:
        """Delete all cached TMDB responses."""
        if self.cache_dir.exists():
            shutil.rmtree(self.cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
