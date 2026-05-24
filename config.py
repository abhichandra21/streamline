"""Configuration loader.

Reads non-secret settings from config.yaml plus optional local overrides from
config.local.yaml. Secrets continue to come from the environment. All
module-level attributes are available as before for backward compat.
"""

import os
from pathlib import Path

import yaml

_ROOT = Path(__file__).parent

_CONFIG_PATH = _ROOT / "config.yaml"
_LOCAL_CONFIG_PATH = _ROOT / "config.local.yaml"


def _load_yaml(path: Path) -> dict:
    if not path.exists():
        return {}
    with open(path) as f:
        return yaml.safe_load(f) or {}


def _merge_dicts(base: dict, override: dict) -> dict:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(merged.get(key), dict) and isinstance(value, dict):
            merged[key] = _merge_dicts(merged[key], value)
        else:
            merged[key] = value
    return merged


_cfg = _merge_dicts(_load_yaml(_CONFIG_PATH), _load_yaml(_LOCAL_CONFIG_PATH))

# ── Logging ──
LOG_LEVEL = os.environ.get("LOG_LEVEL", _cfg.get("log_level", "WARNING")).upper()

# ── Secrets (from environment only; .env is an optional launcher convenience) ──
TMDB_API_KEY = os.environ.get("TMDB_API_KEY", "")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")

LLM_DEFAULT_API_KEY_ENVS: dict[str, str] = {
    "anthropic": "ANTHROPIC_API_KEY",
    "gemini": "GEMINI_API_KEY",
    "openai": "OPENAI_API_KEY",
    "local": "OPENAI_API_KEY",  # local endpoints don't require a real key
}

# ── LLM settings ──
LLM_PROVIDER = os.environ.get("LLM_PROVIDER", _cfg.get("provider", "anthropic"))
LLM_MODELS: dict[str, dict[str, object]] = _cfg.get("models", {
    "anthropic": {"fast": "claude-haiku-4-5-20251001", "reason": "claude-sonnet-4-6"},
    "gemini": {"fast": "gemini-2.5-flash", "reason": "gemini-2.5-flash"},
    "openai": {"fast": "gpt-4.1-mini", "reason": "gpt-4.1", "base_url": None},
    "local": {"fast": "llama3.2", "reason": "llama3.2", "base_url": "http://localhost:11434/v1"},
})


def get_llm_api_key_env(provider: str) -> str:
    """Resolve the environment variable name for a provider API key.

    The common case uses provider-specific defaults. config.yaml only needs an
    api_key_env entry when the deployment uses a non-standard variable name.
    """
    configured_models = LLM_MODELS.get(provider, {})
    configured_env = str(configured_models.get("api_key_env") or "").strip()
    if configured_env:
        return configured_env

    return LLM_DEFAULT_API_KEY_ENVS.get(provider, f"{provider.upper()}_API_KEY")

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


def _resolve_platform_paths(platform: str, default: str | None) -> list[str]:
    """Normalize a platform_paths entry to a list of absolute path strings.

    Input shapes accepted (from config.yaml or config.local.yaml):
      - string        -> one path (wrapped in list)
      - list[str]     -> multiple paths
      - null / ""     -> disabled (empty list)
      - empty list    -> disabled (empty list)
      - missing key   -> repo default path (wrapped in list), or empty list if no default

    Returns an empty list to signal "disabled".
    """
    if platform not in _paths:
        # Missing key: keep repo default if one exists.
        if default is None:
            return []
        return [str(_ROOT / default)]

    raw = _paths[platform]

    # Explicit null or empty string disables the source.
    if raw in (None, ""):
        return []

    # Normalize to list.
    if isinstance(raw, str):
        paths = [raw]
    elif isinstance(raw, list):
        paths = raw
    else:
        raise TypeError(f"platform_paths.{platform} must be a string, list, or null, got {type(raw).__name__}")

    # Filter out any empty strings, then resolve against _ROOT.
    return [str(_ROOT / p) for p in paths if p]


PLATFORM_PATHS: dict[str, list[str]] = {
    "netflix": _resolve_platform_paths("netflix", "data/netflix/export.zip"),
    "prime": _resolve_platform_paths("prime", "data/prime_video/Prime Video.zip"),
    "apple_tv": _resolve_platform_paths("apple_tv", "data/AppleTV/Apple Media Services Information Part 1 of 2.zip"),
    "disney": _resolve_platform_paths("disney", None),
    "hbo": _resolve_platform_paths("hbo", None),
}
DISNEY_PROFILES: list[str] = list(_cfg.get("disney_profiles", []) or [])
MANUAL_TV_PATH = str(_ROOT / _cfg.get("manual_tv_path", "data/manual/tv.csv"))
MANUAL_MOVIES_PATH = str(_ROOT / _cfg.get("manual_movies_path", "data/manual/movies.csv"))
OVERRIDES_PATH = str(_ROOT / _cfg.get("overrides_path", "data/overrides.json"))
EVENT_DB_PATH = str(_ROOT / _cfg.get("event_db_path", "data/streamline.db"))

# ── Cache paths (fixed, not user-configurable) ──
CACHE_DIR = str(_ROOT / "recommender/cache/tmdb")
ENRICHMENT_CACHE_DIR = str(_ROOT / "recommender/cache/enrichments")
PROVIDERS_CACHE_DIR = str(_ROOT / "recommender/cache/providers")
TASTE_PROFILE_PATH = str(_ROOT / "recommender/cache/taste_profile.txt")
WATCH_INDEX_PATH = str(_ROOT / "recommender/cache/watch_index.json")
FEEDBACK_PATH = str(_ROOT / "recommender/cache/feedback.json")
PROFILE_STALE_FLAG = str(_ROOT / "recommender/cache/.profile_stale")
APP_LOG_PATH = str(_ROOT / "logs/app.log")
