"""Flask web UI for Streamline — taste profile dashboard, watch history, and recommendation search."""

import json
import logging
import os
import re
import sys
from copy import deepcopy
from datetime import datetime
from pathlib import Path

import yaml
from recommender.llm import create_client
from flask import Flask, jsonify, redirect, render_template, request, url_for
from markupsafe import Markup, escape

import config
from recommender.ingestion.netflix import parse as parse_netflix
from recommender.ingestion.prime import parse as parse_prime
from recommender.ingestion.manual import parse as parse_manual
from recommender.tmdb_client import TmdbClient
from recommender.query_engine import RecommendContext, ask
from recommender import watch_index as wi
from recommender import history as query_history

log = logging.getLogger("recommender.web")

app = Flask(__name__, template_folder="templates")

_ctx: RecommendContext | None = None


def _get_context() -> RecommendContext:
    global _ctx
    if _ctx is None:
        _ctx = _build_context()
    return _ctx


def _build_context() -> RecommendContext:
    events = []
    for platform, parser in [("netflix", parse_netflix), ("prime", parse_prime)]:
        path = config.PLATFORM_PATHS.get(platform)
        if path:
            try:
                events.extend(parser(path))
            except Exception:
                pass
    try:
        events.extend(parse_manual(config.MANUAL_TV_PATH, config.MANUAL_MOVIES_PATH))
    except Exception:
        pass

    index_path = Path(config.WATCH_INDEX_PATH)
    profile_path = Path(config.TASTE_PROFILE_PATH)

    if not index_path.exists() or not profile_path.exists():
        raise RuntimeError("Run python -m recommender.setup before starting the web UI.")

    return RecommendContext(
        taste_profile=profile_path.read_text(),
        watch_index=wi.load(config.WATCH_INDEX_PATH),
        events=events,
        tmdb_client=TmdbClient(api_key=config.TMDB_API_KEY, cache_dir=config.CACHE_DIR),
        llm=create_client(),
        cache_dir=config.ENRICHMENT_CACHE_DIR,
        providers_cache_dir=config.PROVIDERS_CACHE_DIR,
        watch_region=config.WATCH_REGION,
        streaming_platforms=list(config.STREAMING_PLATFORMS),
    )


def _load_enrichments() -> dict[str, str]:
    index_path = Path(config.ENRICHMENT_CACHE_DIR) / "index.json"
    if index_path.exists():
        return json.loads(index_path.read_text())
    return {}


def _md_to_html(text: str) -> str:
    """Convert basic markdown (bold, italic, em-dash) to HTML. No external deps."""
    html = str(escape(text))
    # Bold: **text**
    html = re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', html)
    # Italic: *text*
    html = re.sub(r'\*(.+?)\*', r'<em>\1</em>', html)
    # Paragraphs: double newline
    paragraphs = re.split(r'\n\s*\n', html)
    html = ''.join(f'<p>{p.strip()}</p>' for p in paragraphs if p.strip())
    return Markup(html)


def _get_poster_url(tmdb_id: int, content_type: str, size: str = "w300") -> str | None:
    """Get poster URL from cached TMDB data."""
    cache_path = Path(config.CACHE_DIR) / content_type / f"{tmdb_id}.json"
    if cache_path.exists():
        data = json.loads(cache_path.read_text())
        poster = data.get("poster_path")
        if poster:
            return f"https://image.tmdb.org/t/p/{size}{poster}"
    return None


def _get_recent_posters(entries: list[dict], limit: int = 24) -> list[dict]:
    """Get poster URLs for a selection of watched titles."""
    posters = []
    for e in entries:
        tmdb_id = e.get("tmdb_id")
        ct = e.get("content_type", "movie")
        if not tmdb_id:
            continue
        url = _get_poster_url(tmdb_id, ct, "w185")
        if url:
            posters.append({"title": e["title"], "poster": url, "tmdb_id": tmdb_id, "content_type": ct})
        if len(posters) >= limit:
            break
    return posters


@app.route("/")
def dashboard() -> str:
    ctx = _get_context()
    enrichments = _load_enrichments()
    entries = ctx.watch_index.entries
    tv_count = sum(1 for e in entries if e.get("content_type") == "tv")
    movie_count = sum(1 for e in entries if e.get("content_type") == "movie")

    # Parse profile into clusters
    clusters = []
    current_cluster = None
    for line in ctx.taste_profile.split("\n"):
        if line.startswith("## "):
            if current_cluster:
                clusters.append(current_cluster)
            heading = line[3:].strip()
            # Strip "CLUSTER A: " or "CLUSTER B: " prefix
            heading = re.sub(r'^CLUSTER\s+[A-Z0-9]+:\s*', '', heading)
            current_cluster = {"heading": heading, "body": ""}
        elif current_cluster is not None:
            # Skip standalone --- separators and **Strength:** lines
            stripped = line.strip()
            if stripped == '---' or stripped.startswith('**Strength:'):
                continue
            current_cluster["body"] += line + "\n"
    if current_cluster:
        clusters.append(current_cluster)

    # Convert markdown in cluster bodies to HTML
    for c in clusters:
        c["body_html"] = _md_to_html(c["body"].strip())

    posters = _get_recent_posters(entries, limit=30)
    recent_queries = query_history.load(limit=5)

    return render_template(
        "index.html",
        taste_profile=ctx.taste_profile,
        clusters=clusters,
        total=len(entries),
        tv_count=tv_count,
        movie_count=movie_count,
        enrichment_count=len(enrichments),
        posters=posters,
        recent_queries=recent_queries,
    )


@app.route("/history")
def history() -> str:
    ctx = _get_context()
    enrichments = _load_enrichments()
    q = request.args.get("q", "").lower()
    ct_filter = request.args.get("type", "")

    entries = list(ctx.watch_index.entries)  # list of dicts

    if q:
        entries = [e for e in entries if q in e["title"].lower()]
    if ct_filter in ("tv", "movie"):
        entries = [e for e in entries if e.get("content_type") == ct_filter]

    entries.sort(key=lambda e: e["title"].lower())
    items = [
        {
            "title": e["title"],
            "content_type": e.get("content_type", ""),
            "tmdb_id": e.get("tmdb_id"),
            "description": enrichments.get(e["title"], ""),
            "poster": _get_poster_url(e.get("tmdb_id", 0), e.get("content_type", "movie"), "w185") if e.get("tmdb_id") else None,
        }
        for e in entries
    ]
    return render_template("history.html", items=items, q=q, ct_filter=ct_filter, total=len(entries))


@app.route("/searches")
def searches() -> str:
    entries = query_history.load()
    # Format timestamps for display
    for e in entries:
        ts = e.get("timestamp", "")
        try:
            from datetime import datetime, timezone
            dt = datetime.fromisoformat(ts)
            now = datetime.now(timezone.utc)
            delta = now - dt
            if delta.days == 0:
                hours = delta.seconds // 3600
                if hours == 0:
                    minutes = delta.seconds // 60
                    e["timestamp_display"] = f"{minutes}m ago" if minutes > 0 else "just now"
                else:
                    e["timestamp_display"] = f"{hours}h ago"
            elif delta.days == 1:
                e["timestamp_display"] = "yesterday"
            elif delta.days < 7:
                e["timestamp_display"] = f"{delta.days}d ago"
            else:
                e["timestamp_display"] = dt.strftime("%b %d, %Y")
        except (ValueError, TypeError):
            e["timestamp_display"] = ts[:19]
    return render_template("searches.html", entries=entries)


@app.route("/searches/delete", methods=["DELETE"])
def delete_search():
    ts = request.args.get("ts", "")
    if not ts:
        return "", 400
    deleted = query_history.delete(ts)
    if not deleted:
        return "", 404
    return "", 200, {"HX-Redirect": "/searches"}


@app.route("/recommend", methods=["GET", "POST"])
def recommend() -> str:
    if request.method == "GET":
        return render_template("recommend.html", results=None, query="")

    query = (request.form.get("query") or "").strip()
    if not query:
        return render_template("recommend.html", results=None, query="")

    ctx = _get_context()
    ctx.llm.usage.reset()
    try:
        results = ask(query, ctx)
    except Exception as exc:
        log.exception("Error during recommendation")
        return render_template("recommend.html", results=None, query=query, error=str(exc))

    try:
        query_history.record(query, results, ctx.llm.provider, ctx.llm.usage.summary())
    except OSError as exc:
        log.warning("Failed to persist query history for %r: %s", query, exc)

    items = []
    for r in results:
        # Look up poster from TMDB cache
        poster = None
        cached = ctx.tmdb_client._load_cache(r.content_type, 0)  # won't work, need tmdb_id
        # Search the candidates for tmdb_id
        meta = ctx.tmdb_client.get_metadata(r.title, r.content_type)
        if meta:
            poster = _get_poster_url(meta.tmdb_id, r.content_type)
        tmdb_url = ""
        imdb_url = ""
        if meta:
            tmdb_type = "tv" if r.content_type == "tv" else "movie"
            tmdb_url = f"https://www.themoviedb.org/{tmdb_type}/{meta.tmdb_id}"
            # Try to get direct IMDB link from cached TMDB detail
            cache_path = Path(config.CACHE_DIR) / r.content_type / f"{meta.tmdb_id}.json"
            if cache_path.exists():
                raw = json.loads(cache_path.read_text())
                imdb_id = raw.get("imdb_id")
                if imdb_id:
                    imdb_url = f"https://www.imdb.com/title/{imdb_id}/"
            # Fallback: IMDB search by title
            if not imdb_url:
                from urllib.parse import quote
                imdb_url = f"https://www.imdb.com/find/?q={quote(r.title)}"
        items.append({
            "title": r.title,
            "content_type": r.content_type,
            "score": r.score,
            "vote_average": r.vote_average,
            "genres": r.genres[:3],
            "explanation": r.explanation,
            "streaming_providers": r.streaming_providers[:4],
            "poster": poster,
            "tmdb_url": tmdb_url,
            "imdb_url": imdb_url,
        })

    # HTMX partial — return just the results fragment.
    if request.headers.get("HX-Request"):
        return render_template("_results.html", results=items, query=query)

    return render_template("recommend.html", results=items, query=query)


@app.route("/title/<int:tmdb_id>")
def title_detail(tmdb_id: int) -> str:
    ct = request.args.get("type", "tv")
    ctx = _get_context()
    enrichments = _load_enrichments()
    meta = None
    try:
        from recommender.tmdb_client import TmdbMetadata
        cached = ctx.tmdb_client._load_cache(ct, tmdb_id)
        if cached:
            meta = ctx.tmdb_client._parse_metadata(cached, ct)
    except Exception:
        pass

    description = enrichments.get(meta.title, "") if meta else ""
    poster = _get_poster_url(tmdb_id, ct, "w500") if meta else None
    # Get overview from cached TMDB data
    overview = ""
    if meta:
        cache_path = Path(config.CACHE_DIR) / ct / f"{tmdb_id}.json"
        if cache_path.exists():
            raw = json.loads(cache_path.read_text())
            overview = raw.get("overview", "")
    return render_template("title.html", meta=meta, description=description, overview=overview,
                           tmdb_id=tmdb_id, ct=ct, poster=poster)


@app.route("/help")
def help_page() -> str:
    return render_template("help.html")


_CONFIG_PATH = Path(__file__).parent.parent / "config.yaml"
_DEFAULT_LLM_API_KEY_ENVS = dict(config.LLM_DEFAULT_API_KEY_ENVS)
_SETTINGS_DEFAULTS = {
    "provider": "anthropic",
    "models": {
        "anthropic": {
            "fast": "claude-haiku-4-5-20251001",
            "reason": "claude-sonnet-4-6",
        },
        "gemini": {
            "fast": "gemini-2.5-flash",
            "reason": "gemini-2.5-flash",
        },
        "openai": {
            "fast": "gpt-4.1-mini",
            "reason": "gpt-4.1",
            "base_url": None,
        },
    },
    "llm": {
        "timeout_fast": 30,
        "timeout_reason": 60,
        "timeout_profile_batch": 60,
        "timeout_profile_merge": 300,
        "tokens_fast": 200,
        "tokens_intent": 400,
        "tokens_ranking": 1000,
        "tokens_suggestions": 300,
        "tokens_profile_batch": 800,
        "tokens_profile_merge": 4000,
        "tokens_abandoned": 300,
        "profile_batch_size": 200,
        "rate_limit_wait": 65,
    },
    "scoring": {
        "weight_completion": 0.5,
        "weight_rewatch": 0.3,
        "weight_recency": 0.2,
        "default_tv_runtime": 45,
        "default_movie_runtime": 90,
        "rewatch_saturation": 5,
    },
    "default_top_n": 3,
    "min_vote_count": 20,
    "min_rating": 0,
    "min_year": 0,
    "recency_half_life_days": 90,
    "watch_region": "US",
    "streaming_platforms": [],
    "manual": {
        "timestamp": "now",
        "tv_duration_minutes": 45,
        "movie_duration_minutes": 120,
    },
    "log_level": "WARNING",
}

_PROFILE_REBUILD_LLM_KEYS = (
    "timeout_profile_batch",
    "timeout_profile_merge",
    "tokens_profile_batch",
    "tokens_profile_merge",
    "profile_batch_size",
    "rate_limit_wait",
)
_PROFILE_REBUILD_SCORING_KEYS = (
    "weight_completion",
    "weight_rewatch",
    "weight_recency",
    "default_tv_runtime",
    "default_movie_runtime",
    "rewatch_saturation",
)


def _load_config_yaml() -> dict:
    """Load raw config.yaml as a dict for the settings form."""
    with open(_CONFIG_PATH) as f:
        return yaml.safe_load(f) or {}


def _save_config_yaml(cfg: dict) -> None:
    """Write config dict back to config.yaml, preserving comments where possible."""
    with open(_CONFIG_PATH, "w") as f:
        f.write("# Streamline configuration\n")
        f.write("# Set API keys in the environment. .env is optional local convenience.\n\n")
        yaml.dump(cfg, f, default_flow_style=False, sort_keys=False, allow_unicode=True)


def _merge_settings_defaults(target: dict, source: dict) -> dict:
    for key, value in source.items():
        if isinstance(value, dict):
            existing = target.get(key)
            if not isinstance(existing, dict):
                existing = {}
            target[key] = _merge_settings_defaults(existing, value)
            continue
        target[key] = value
    return target


def _resolve_settings_config(raw_cfg: dict | None = None) -> dict:
    cfg = deepcopy(_SETTINGS_DEFAULTS)
    return _merge_settings_defaults(cfg, raw_cfg or {})


def _render_settings_page(
    cfg: dict | None = None,
    *,
    saved: str | bool | None = None,
    error: str | None = None,
) -> str:
    resolved_cfg = _resolve_settings_config(cfg if cfg is not None else _load_config_yaml())
    return render_template("settings.html", cfg=resolved_cfg, saved=saved, error=error)


def _reload_app_config() -> None:
    """Reload config module and rebuild app context."""
    global _ctx
    import importlib
    importlib.reload(config)
    _ctx = None  # next request will rebuild context
    log.info("Config reloaded from config.yaml")


def _parse_int_field(form, key: str, default: int) -> int:
    value = (form.get(key) or "").strip()
    if not value:
        return default
    try:
        return int(value)
    except ValueError as exc:
        raise ValueError(f"{key} must be an integer.") from exc


def _parse_float_field(form, key: str, default: float) -> float:
    value = (form.get(key) or "").strip()
    if not value:
        return default
    try:
        return float(value)
    except ValueError as exc:
        raise ValueError(f"{key} must be a number.") from exc


def _parse_manual_timestamp(form, current_cfg: dict) -> str:
    mode = (form.get("manual_timestamp_mode") or "").strip() or (
        "now" if current_cfg["manual"]["timestamp"] == "now" else "fixed"
    )
    if mode == "now":
        return "now"

    date_value = (form.get("manual_timestamp_date") or "").strip()
    if not date_value:
        fallback = current_cfg["manual"]["timestamp"]
        date_value = fallback if fallback != "now" else "2022-01-01"
    try:
        datetime.strptime(date_value, "%Y-%m-%d")
    except ValueError as exc:
        raise ValueError("manual_timestamp_date must use YYYY-MM-DD.") from exc
    return date_value


def _settings_refresh_flags(previous_cfg: dict, updated_cfg: dict) -> tuple[bool, bool]:
    refresh_data = any(
        previous_cfg["manual"][key] != updated_cfg["manual"][key]
        for key in ("timestamp", "tv_duration_minutes", "movie_duration_minutes")
    )
    refresh_profile = refresh_data
    refresh_profile = refresh_profile or any(
        previous_cfg["scoring"][key] != updated_cfg["scoring"][key]
        for key in _PROFILE_REBUILD_SCORING_KEYS
    )
    refresh_profile = refresh_profile or previous_cfg["recency_half_life_days"] != updated_cfg["recency_half_life_days"]
    refresh_profile = refresh_profile or any(
        previous_cfg["llm"][key] != updated_cfg["llm"][key]
        for key in _PROFILE_REBUILD_LLM_KEYS
    )
    return refresh_profile, refresh_data


def _refresh_derived_data(refresh_profile: bool, refresh_data: bool) -> None:
    global _ctx
    if not refresh_profile and not refresh_data:
        return

    from recommender.setup import run_setup

    run_setup(refresh_profile=refresh_profile, refresh_data=refresh_data)
    _ctx = None
    log.info(
        "Rebuilt derived data after settings save (refresh_profile=%s, refresh_data=%s)",
        refresh_profile,
        refresh_data,
    )


@app.route("/settings", methods=["GET"])
def settings_page() -> str:
    return _render_settings_page(saved=request.args.get("saved"))


@app.route("/settings", methods=["POST"])
def settings_save() -> str:
    cfg = _load_config_yaml()
    current_cfg = _resolve_settings_config(cfg)
    form = request.form

    try:
        llm_values = {
            key: _parse_int_field(form, f"llm_{key}", current_cfg["llm"][key])
            for key in (
                "timeout_fast",
                "timeout_reason",
                "timeout_profile_batch",
                "timeout_profile_merge",
                "tokens_fast",
                "tokens_intent",
                "tokens_ranking",
                "tokens_suggestions",
                "tokens_profile_batch",
                "tokens_profile_merge",
                "tokens_abandoned",
                "profile_batch_size",
                "rate_limit_wait",
            )
        }
        scoring_values = {
            "weight_completion": _parse_float_field(
                form, "weight_completion", current_cfg["scoring"]["weight_completion"]
            ),
            "weight_rewatch": _parse_float_field(
                form, "weight_rewatch", current_cfg["scoring"]["weight_rewatch"]
            ),
            "weight_recency": _parse_float_field(
                form, "weight_recency", current_cfg["scoring"]["weight_recency"]
            ),
            "default_tv_runtime": _parse_int_field(
                form, "default_tv_runtime", current_cfg["scoring"]["default_tv_runtime"]
            ),
            "default_movie_runtime": _parse_int_field(
                form, "default_movie_runtime", current_cfg["scoring"]["default_movie_runtime"]
            ),
            "rewatch_saturation": _parse_int_field(
                form, "rewatch_saturation", current_cfg["scoring"]["rewatch_saturation"]
            ),
        }
        recommendation_values = {
            "default_top_n": _parse_int_field(form, "default_top_n", current_cfg["default_top_n"]),
            "min_vote_count": _parse_int_field(form, "min_vote_count", current_cfg["min_vote_count"]),
            "min_rating": _parse_float_field(form, "min_rating", current_cfg["min_rating"]),
            "min_year": _parse_int_field(form, "min_year", current_cfg["min_year"]),
            "recency_half_life_days": _parse_int_field(
                form, "recency_half_life_days", current_cfg["recency_half_life_days"]
            ),
        }
        manual_values = {
            "timestamp": _parse_manual_timestamp(form, current_cfg),
            "tv_duration_minutes": _parse_int_field(
                form, "manual_tv_duration", current_cfg["manual"]["tv_duration_minutes"]
            ),
            "movie_duration_minutes": _parse_int_field(
                form, "manual_movie_duration", current_cfg["manual"]["movie_duration_minutes"]
            ),
        }
    except ValueError as exc:
        return _render_settings_page(cfg=current_cfg, saved=False, error=str(exc))

    # Validate scoring weights sum to 1.0
    w_comp = scoring_values["weight_completion"]
    w_rew = scoring_values["weight_rewatch"]
    w_rec = scoring_values["weight_recency"]
    try:
        if abs((w_comp + w_rew + w_rec) - 1.0) > 0.01:
            return _render_settings_page(
                cfg=current_cfg,
                saved=False,
                error=f"Scoring weights must sum to 1.0 (got {w_comp + w_rew + w_rec:.2f})",
            )
    except ValueError:
        return _render_settings_page(cfg=current_cfg, saved=False, error="Invalid scoring weights.")

    # Provider & models — all providers treated the same
    cfg["provider"] = form.get("provider", "anthropic")
    cfg.setdefault("models", {})
    for p in ["anthropic", "gemini", "openai"]:
        existing = cfg["models"].get(p, {})
        provider_cfg = {
            "fast": (form.get(f"{p}_fast") or existing.get("fast", "")).strip(),
            "reason": (form.get(f"{p}_reason") or existing.get("reason", "")).strip(),
        }
        submitted_api_key_env = form.get(f"{p}_api_key_env")
        if submitted_api_key_env is None:
            api_key_env = str(existing.get("api_key_env") or "").strip()
        else:
            api_key_env = submitted_api_key_env.strip()
        if api_key_env and api_key_env != _DEFAULT_LLM_API_KEY_ENVS.get(p, ""):
            provider_cfg["api_key_env"] = api_key_env
        cfg["models"][p] = provider_cfg
    # base_url only relevant for openai provider
    base_url = (form.get("openai_base_url") or "").strip()
    cfg["models"]["openai"]["base_url"] = base_url if base_url else None

    # LLM limits
    llm = cfg.setdefault("llm", {})
    llm.update(llm_values)

    # Scoring
    scoring = cfg.setdefault("scoring", {})
    scoring.update(scoring_values)

    # Recommendations
    cfg.update(recommendation_values)

    # Streaming
    cfg["watch_region"] = form.get("watch_region", "US").strip().upper()
    platforms_str = form.get("streaming_platforms", "").strip()
    cfg["streaming_platforms"] = [p.strip() for p in platforms_str.split(",") if p.strip()] if platforms_str else []

    # Manual
    manual = cfg.setdefault("manual", {})
    manual.update(manual_values)

    # Logging
    cfg["log_level"] = form.get("log_level", "WARNING")

    updated_cfg = _resolve_settings_config(cfg)
    refresh_profile, refresh_data = _settings_refresh_flags(current_cfg, updated_cfg)

    _save_config_yaml(cfg)
    _reload_app_config()
    try:
        if refresh_profile or refresh_data:
            _refresh_derived_data(refresh_profile=refresh_profile, refresh_data=refresh_data)
    except SystemExit as exc:
        log.error("Derived data rebuild exited during settings save with status %s", exc.code)
        return _render_settings_page(
            cfg=updated_cfg,
            saved=False,
            error="Settings were saved, but derived data rebuild failed. Check the server log.",
        )
    except Exception as exc:
        log.exception("Failed to rebuild derived data after settings save")
        return _render_settings_page(
            cfg=updated_cfg,
            saved=False,
            error=f"Settings were saved, but derived data rebuild failed: {exc}",
        )

    return redirect(url_for("settings_page", saved="1"))


def run() -> None:
    from recommender.log import setup_logging
    setup_logging()
    if not config.TMDB_API_KEY:
        print("Error: TMDB_API_KEY must be set.", file=sys.stderr)
        sys.exit(1)
    try:
        create_client()
    except (RuntimeError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)
    host = os.environ.get("STREAMLINE_HOST", "127.0.0.1")
    port = int(os.environ.get("STREAMLINE_PORT", "5050"))
    print(f"Starting Streamline web UI at http://{host}:{port}")
    app.run(debug=False, host=host, port=port)


if __name__ == "__main__":
    run()
