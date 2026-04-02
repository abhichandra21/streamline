"""Configuration loader.

Reads secrets from environment (.env) and settings from config.yaml.
All module-level attributes are available as before for backward compat.
"""

import os
from pathlib import Path

import yaml

_ROOT = Path(__file__).parent

# Load config.yaml
_CONFIG_PATH = _ROOT / "config.yaml"
if _CONFIG_PATH.exists():
    with open(_CONFIG_PATH) as f:
        _cfg = yaml.safe_load(f) or {}
else:
    _cfg = {}

# ── Secrets (from .env / environment only) ──
TMDB_API_KEY = os.environ.get("TMDB_API_KEY", "")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")

# ── LLM settings ──
LLM_PROVIDER = os.environ.get("LLM_PROVIDER", _cfg.get("provider", "anthropic"))
LLM_MODELS: dict[str, dict[str, str]] = _cfg.get("models", {
    "anthropic": {"fast": "claude-haiku-4-5-20251001", "reason": "claude-sonnet-4-6"},
    "gemini": {"fast": "gemini-2.5-flash", "reason": "gemini-2.5-flash"},
})

# ── Recommendation settings ──
DEFAULT_TOP_N = _cfg.get("default_top_n", 3)
CANDIDATE_POOL_SIZE = _cfg.get("candidate_pool_size", 500)
MIN_VOTE_COUNT = _cfg.get("min_vote_count", 20)
RECENCY_HALF_LIFE_DAYS = _cfg.get("recency_half_life_days", 90)

# ── Streaming availability ──
WATCH_REGION = _cfg.get("watch_region", "US")
STREAMING_PLATFORMS: list[str] = _cfg.get("streaming_platforms", [])

# ── Data paths ──
_paths = _cfg.get("platform_paths", {})
PLATFORM_PATHS = {
    "netflix": str(_ROOT / _paths.get("netflix", "data/netflix/ViewingActivity.csv")),
    "prime": str(_ROOT / _paths.get("prime", "data/prime_video/Viewing History.csv")),
    "disney": None,
    "hbo": None,
}
MANUAL_TV_PATH = str(_ROOT / _cfg.get("manual_tv_path", "data/manual/tv.csv"))
MANUAL_MOVIES_PATH = str(_ROOT / _cfg.get("manual_movies_path", "data/manual/movies.csv"))
OVERRIDES_PATH = str(_ROOT / _cfg.get("overrides_path", "data/overrides.json"))

# ── Cache paths (fixed, not user-configurable) ──
CACHE_DIR = str(_ROOT / "recommender/cache/tmdb")
ENRICHMENT_CACHE_DIR = str(_ROOT / "recommender/cache/enrichments")
PROVIDERS_CACHE_DIR = str(_ROOT / "recommender/cache/providers")
TASTE_PROFILE_PATH = str(_ROOT / "recommender/cache/taste_profile.txt")
WATCH_INDEX_PATH = str(_ROOT / "recommender/cache/watch_index.json")
FEEDBACK_PATH = str(_ROOT / "recommender/cache/feedback.json")
