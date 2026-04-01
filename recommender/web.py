"""Flask web UI for Streamline — taste profile dashboard, watch history, and recommendation search."""

import json
import logging
import sys
from pathlib import Path

import anthropic
from flask import Flask, jsonify, render_template, request

import config
from recommender.ingestion.netflix import parse as parse_netflix
from recommender.ingestion.prime import parse as parse_prime
from recommender.ingestion.manual import parse as parse_manual
from recommender.tmdb_client import TmdbClient
from recommender.query_engine import RecommendContext, ask
from recommender import watch_index as wi

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
        anthropic_client=anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY),
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


@app.route("/")
def dashboard() -> str:
    ctx = _get_context()
    enrichments = _load_enrichments()
    entries = ctx.watch_index.entries  # list of dicts
    tv_count = sum(1 for e in entries if e.get("content_type") == "tv")
    movie_count = sum(1 for e in entries if e.get("content_type") == "movie")
    return render_template(
        "index.html",
        taste_profile=ctx.taste_profile,
        total=len(entries),
        tv_count=tv_count,
        movie_count=movie_count,
        enrichment_count=len(enrichments),
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
            "poster": None,
        }
        for e in entries
    ]
    return render_template("history.html", items=items, q=q, ct_filter=ct_filter, total=len(entries))


@app.route("/recommend", methods=["GET", "POST"])
def recommend() -> str:
    if request.method == "GET":
        return render_template("recommend.html", results=None, query="")

    query = (request.form.get("query") or "").strip()
    if not query:
        return render_template("recommend.html", results=None, query="")

    ctx = _get_context()
    try:
        results = ask(query, ctx)
    except Exception as exc:
        log.exception("Error during recommendation")
        return render_template("recommend.html", results=None, query=query, error=str(exc))

    items = [
        {
            "title": r.title,
            "content_type": r.content_type,
            "score": r.score,
            "vote_average": r.vote_average,
            "genres": r.genres[:3],
            "explanation": r.explanation,
            "streaming_providers": r.streaming_providers[:4],
        }
        for r in results
    ]

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
    return render_template("title.html", meta=meta, description=description, tmdb_id=tmdb_id, ct=ct)


def run() -> None:
    logging.basicConfig(level=logging.INFO, format="%(name)s %(levelname)s: %(message)s")
    if not config.ANTHROPIC_API_KEY or not config.TMDB_API_KEY:
        print("Error: ANTHROPIC_API_KEY and TMDB_API_KEY must be set.", file=sys.stderr)
        sys.exit(1)
    print("Starting Streamline web UI at http://localhost:5000")
    app.run(debug=False, host="127.0.0.1", port=5000)


if __name__ == "__main__":
    run()
