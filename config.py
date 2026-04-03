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

# ── Logging ──
LOG_LEVEL = os.environ.get("LOG_LEVEL", _cfg.get("log_level", "WARNING")).upper()

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

# ── LLM call settings ──
_llm_cfg = _cfg.get("llm", {})

# Timeouts (seconds)
TIMEOUT_FAST = _llm_cfg.get("timeout_fast", 30)
TIMEOUT_REASON = _llm_cfg.get("timeout_reason", 60)
TIMEOUT_PROFILE_BATCH = _llm_cfg.get("timeout_profile_batch", 60)
TIMEOUT_PROFILE_MERGE = _llm_cfg.get("timeout_profile_merge", 300)

# Max output tokens
TOKENS_FAST = _llm_cfg.get("tokens_fast", 200)
TOKENS_INTENT = _llm_cfg.get("tokens_intent", 400)
TOKENS_RANKING = _llm_cfg.get("tokens_ranking", 1000)
TOKENS_SUGGESTIONS = _llm_cfg.get("tokens_suggestions", 300)
TOKENS_PROFILE_BATCH = _llm_cfg.get("tokens_profile_batch", 800)
TOKENS_PROFILE_MERGE = _llm_cfg.get("tokens_profile_merge", 4000)
TOKENS_ABANDONED = _llm_cfg.get("tokens_abandoned", 300)

# Taste profile
PROFILE_BATCH_SIZE = _llm_cfg.get("profile_batch_size", 200)
RATE_LIMIT_WAIT = _llm_cfg.get("rate_limit_wait", 65)

# ── Scoring weights ──
_scoring = _cfg.get("scoring", {})
WEIGHT_COMPLETION = _scoring.get("weight_completion", 0.5)
WEIGHT_REWATCH = _scoring.get("weight_rewatch", 0.3)
WEIGHT_RECENCY = _scoring.get("weight_recency", 0.2)
DEFAULT_TV_RUNTIME = _scoring.get("default_tv_runtime", 45)
DEFAULT_MOVIE_RUNTIME = _scoring.get("default_movie_runtime", 90)
REWATCH_SATURATION = _scoring.get("rewatch_saturation", 5)

# ── Manual title settings ──
_manual = _cfg.get("manual", {})
MANUAL_TIMESTAMP = _manual.get("timestamp", "now")  # "now" or "YYYY-MM-DD"
MANUAL_TV_DURATION_MINUTES = _manual.get("tv_duration_minutes", 45)
MANUAL_MOVIE_DURATION_MINUTES = _manual.get("movie_duration_minutes", 120)

# ── Recommendation settings ──
DEFAULT_TOP_N = _cfg.get("default_top_n", 3)
CANDIDATE_POOL_SIZE = _cfg.get("candidate_pool_size", 500)
MIN_VOTE_COUNT = _cfg.get("min_vote_count", 20)
MIN_RATING = float(_cfg.get("min_rating", 0))
MIN_YEAR = int(_cfg.get("min_year", 0))
RECENCY_HALF_LIFE_DAYS = _cfg.get("recency_half_life_days", 90)

# ── Streaming availability ──
WATCH_REGION = _cfg.get("watch_region", "US")
STREAMING_PLATFORMS: list[str] = _cfg.get("streaming_platforms", [])

# ── Data paths ──
_paths = _cfg.get("platform_paths", {})


def _resolve_platform_path(platform: str, default: str | None) -> str | None:
    # Missing keys should keep repo defaults; explicit null/empty disables the source.
    if platform not in _paths:
        return str(_ROOT / default) if default is not None else None

    configured_path = _paths[platform]
    if configured_path in (None, ""):
        return None

    return str(_ROOT / configured_path)


PLATFORM_PATHS = {
    "netflix": _resolve_platform_path("netflix", "data/netflix/ViewingActivity.csv"),
    "prime": _resolve_platform_path("prime", "data/prime_video/Viewing History.csv"),
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
