"""Flask web UI for Streamline — taste profile dashboard, watch history, and recommendation search."""

import json
import logging
import os
import re
import sys
from pathlib import Path

import anthropic
from flask import Flask, jsonify, render_template, request
from markupsafe import Markup, escape

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

    return render_template(
        "index.html",
        taste_profile=ctx.taste_profile,
        clusters=clusters,
        total=len(entries),
        tv_count=tv_count,
        movie_count=movie_count,
        enrichment_count=len(enrichments),
        posters=posters,
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
        if meta:
            tmdb_type = "tv" if r.content_type == "tv" else "movie"
            tmdb_url = f"https://www.themoviedb.org/{tmdb_type}/{meta.tmdb_id}"
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


def run() -> None:
    logging.basicConfig(level=logging.INFO, format="%(name)s %(levelname)s: %(message)s")
    if not config.ANTHROPIC_API_KEY or not config.TMDB_API_KEY:
        print("Error: ANTHROPIC_API_KEY and TMDB_API_KEY must be set.", file=sys.stderr)
        sys.exit(1)
    host = os.environ.get("STREAMLINE_HOST", "127.0.0.1")
    port = int(os.environ.get("STREAMLINE_PORT", "5050"))
    print(f"Starting Streamline web UI at http://{host}:{port}")
    app.run(debug=False, host=host, port=port)


if __name__ == "__main__":
    run()
