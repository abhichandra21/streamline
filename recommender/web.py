"""Flask web UI for Streamline — taste profile dashboard, watch history, and recommendation search."""

import csv
import importlib
import io
import json
import logging
import os
import re
import secrets
import sys
import threading
from copy import copy, deepcopy
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

import yaml
from flask import Flask, Response, jsonify, redirect, render_template, request, session, url_for
from markupsafe import Markup, escape

import config
from recommender import history as query_history
from recommender import user_store
from recommender import watch_index as wi
from recommender import event_store
from recommender.event_store import load_events
from recommender.jobs import registry as job_registry
from recommender.llm import create_client
from recommender.log import setup_logging
from recommender.enricher import enrichment_key_from_parts
from recommender.query_engine import RecommendContext, ask, _safe_query_intent
from recommender import wizard
from recommender import wizard_flow
from recommender.structured_profile import load_structured_profile
from recommender.tmdb_client import TmdbClient

def _events_loader_fallback() -> list:
    events = load_events(config.EVENT_DB_PATH)
    if not events and not Path(config.EVENT_DB_PATH).exists():
        from recommender.setup import load_platform_events_from_exports
        return load_platform_events_from_exports(fail_on_error=False)
    return events

log = logging.getLogger("recommender.web")

app = Flask(__name__, template_folder="templates")
app.secret_key = os.environ.get("STREAMLINE_SECRET_KEY") or secrets.token_hex(32)
setup_logging()

# ── Shared context (read-only after build; rebuilt on config change) ──────────
_ctx: RecommendContext | None = None
_ctx_lock = threading.Lock()
_config_reload_pending = False

# ── User store initialization ─────────────────────────────────────────────────
# Run once per process. ensure_user_store does IF-NOT-EXISTS DDL + migration
# checks — cheap but not free. A flag prevents the overhead on every request.
_user_store_ready = False
_user_store_lock = threading.Lock()


def _ensure_user_store_once() -> None:
    global _user_store_ready
    if _user_store_ready:
        return
    with _user_store_lock:
        if _user_store_ready:
            return
        user_store.ensure_user_store(config.EVENT_DB_PATH, config.FEEDBACK_PATH)
        _user_store_ready = True


def _get_context() -> RecommendContext:
    global _ctx
    with _ctx_lock:
        if _ctx is None:
            _ctx = _build_context()
    return _ctx


def _build_context() -> RecommendContext:
    index_path = Path(config.WATCH_INDEX_PATH)
    profile_path = Path(config.TASTE_PROFILE_PATH)
    if not index_path.exists() or not profile_path.exists():
        raise RuntimeError("Run ./recommend setup before starting the web UI.")

    structured_profile = load_structured_profile(config.STRUCTURED_TASTE_PROFILE_PATH)
    return RecommendContext(
        taste_profile=profile_path.read_text(),
        watch_index=wi.load(config.WATCH_INDEX_PATH),
        tmdb_client=TmdbClient(api_key=config.TMDB_API_KEY, cache_dir=config.CACHE_DIR),
        llm=create_client(),
        cache_dir=config.ENRICHMENT_CACHE_DIR,
        _events_loader=_events_loader_fallback,
        providers_cache_dir=config.PROVIDERS_CACHE_DIR,
        watch_region=config.WATCH_REGION,
        streaming_platforms=list(config.STREAMING_PLATFORMS),
        structured_profile=structured_profile,
    )


def _get_job_context() -> RecommendContext:
    """Shallow-copy the shared context with a fresh LLM client per job.

    This gives each background job its own UsageStats without creating a new
    TMDB client or reloading the watch index.
    """
    ctx = copy(_get_context())
    ctx.llm = create_client()
    ctx.user_state = _load_user_state()
    return ctx


# ── Background job: recommendation query ─────────────────────────────────────

def _run_recommend_job(query: str) -> dict:
    ctx = _get_job_context()
    results = ask(query, ctx)
    items = _build_result_items(results, ctx)
    try:
        query_history.record(query, items, ctx.llm.provider, ctx.llm.usage.summary())
    except OSError as exc:
        log.warning("Failed to persist query history for %r: %s", query, exc)
    return {"items": items, "query": query}


def _run_wizard_recommend_job(intent_dict: dict, context_note: str, summary: str,
                              exclude: list[str] | None = None) -> dict:
    from recommender.query_engine import QueryIntent
    ctx = _get_job_context()
    intent = QueryIntent(**intent_dict)
    # Use the human-readable recap as the query so semantic suggestions and the
    # ranker stay tied to the wizard answers rather than an empty string.
    results = ask(summary, ctx, intent_override=intent, context_note=context_note,
                  exclude_titles=set(exclude) if exclude else None)
    items = _build_result_items(results, ctx)
    label = _wizard_label(summary)
    try:
        query_history.record(
            label, items, ctx.llm.provider, ctx.llm.usage.summary(),
            metadata={
                "source": "wizard",
                "label": label,
                "summary": summary,
                "intent_dict": intent_dict,
                "context_note": context_note,
            },
        )
    except OSError as exc:
        log.warning("Failed to persist wizard history: %s", exc)
    return {"items": items, "query": summary, "summary": summary,
            "intent_dict": intent_dict, "context_note": context_note}


def _wizard_label(summary: str, max_len: int = 60) -> str:
    """Build a compact ASCII history label from a wizard recap summary."""
    text = " ".join((summary or "").split()) or "guided pick"
    label = f"Mood Match - {text}"
    if len(label) > max_len:
        label = label[: max_len - 3].rstrip() + "..."
    return label


def _safe_intent_dict(raw) -> dict:
    """Parse posted intent into a normalized full intent dict.

    Accepts a JSON string or an already-parsed dict. Routes untrusted form data
    through ``_safe_query_intent`` so missing/extra fields are corrected and a
    full, valid intent dict is returned. Raises ``ValueError`` when the payload
    is absent or not a JSON object.
    """
    if isinstance(raw, dict):
        data = raw
    else:
        if not raw or not str(raw).strip():
            raise ValueError("missing intent payload")
        data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("intent payload must be an object")
    return asdict(_safe_query_intent(data))


def _build_result_items(results: list, ctx: RecommendContext) -> list[dict]:
    items = []
    us = _load_user_state()
    for r in results:
        meta = ctx.tmdb_client.get_metadata(r.title, r.content_type)
        poster = _get_poster_url(meta.tmdb_id, r.content_type) if meta else None
        tmdb_url = ""
        imdb_url = ""
        if meta:
            tmdb_type = "tv" if r.content_type == "tv" else "movie"
            tmdb_url = f"https://www.themoviedb.org/{tmdb_type}/{meta.tmdb_id}"
            cache_path = Path(config.CACHE_DIR) / r.content_type / f"{meta.tmdb_id}.json"
            if cache_path.exists():
                raw = json.loads(cache_path.read_text())
                imdb_id = raw.get("imdb_id")
                if imdb_id:
                    imdb_url = f"https://www.imdb.com/title/{imdb_id}/"
            if not imdb_url:
                from urllib.parse import quote
                imdb_url = f"https://www.imdb.com/find/?q={quote(r.title)}"
        tmdb_id_val = meta.tmdb_id if meta else None
        state = _title_state(r.title, r.content_type, us, tmdb_id_val)
        items.append({
            "title": r.title,
            "content_type": r.content_type,
            "score": r.score,
            "vote_average": r.vote_average,
            "genres": r.genres[:3],
            "explanation": r.explanation,
            "streaming_providers": _consolidate_providers(r.streaming_providers)[:4],
            "poster": poster,
            "tmdb_url": tmdb_url,
            "imdb_url": imdb_url,
            "tmdb_id": tmdb_id_val,
            "user_state": state,
        })
    return items


# ── Helpers ───────────────────────────────────────────────────────────────────

def _norm_title(title: str) -> str:
    """Normalize a title for dedup (same rule as user_store._normalize)."""
    title = title.lower()
    title = re.sub(r'\s*\([^)]*\)', '', title)
    return title.strip()


def _title_matches_legacy_entry(entry_title: str, meta_title: str) -> bool:
    """Return True when alternate-type metadata is title-compatible with an old index entry."""
    entry_norm = _norm_title(entry_title)
    meta_norm = _norm_title(meta_title)
    if not entry_norm or not meta_norm:
        return False
    if entry_norm == meta_norm:
        return True
    if len(meta_norm) < 4:
        return False
    return meta_norm in entry_norm


def _can_use_alternate_title_cache(watch_index, tmdb_id: int, alt_ct: str, alt_title: str) -> bool:
    """Allow alternate-type title details only for typed or title-compatible legacy entries."""
    if (alt_ct, tmdb_id) in getattr(watch_index, "tmdb_keys", set()):
        return True
    return any(
        e.get("tmdb_id") == tmdb_id and _title_matches_legacy_entry(e.get("title", ""), alt_title)
        for e in getattr(watch_index, "entries", [])
    )


def _load_enrichments() -> dict[str, str]:
    index_path = Path(config.ENRICHMENT_CACHE_DIR) / "index.json"
    if index_path.exists():
        return json.loads(index_path.read_text())
    return {}


_CLUSTER_TITLES_RE = re.compile(r'^\*\*\*(.+?)\*\*\*\s*$', re.MULTILINE)


def _split_cluster_titles(body: str) -> tuple[str, list[str]]:
    """Split a cluster's raw markdown body into (essay_text, title_list).

    The representative-title list is a single line wrapped in ``***...***``
    with each title individually italicized inside it, e.g.
    ``***Slow Horses*, *Luther*, *Broadchurch***``. Inline ``**bold**`` text
    elsewhere in the essay does not match because it isn't a whole-line
    ``***...***`` span.
    """
    match = _CLUSTER_TITLES_RE.search(body)
    if not match:
        return body.strip(), []
    titles = [t.strip() for t in re.findall(r'([^*,]+)(?:\*|$)', match.group(1)) if t.strip()]
    essay = body[:match.start()] + body[match.end():]
    return essay.strip(), titles


def _strip_markdown_emphasis(text: str) -> str:
    return re.sub(r'\*+', '', text)


def _first_sentence(text: str, max_len: int = 80) -> str:
    """Return the first sentence of text, truncated to max_len at a word boundary."""
    text = _strip_markdown_emphasis(text).strip()
    match = re.search(r'[.!?](?:\s|$)', text)
    sentence = text[:match.end()].strip() if match else text
    if len(sentence) <= max_len:
        return sentence
    truncated = sentence[:max_len].rsplit(' ', 1)[0].rstrip(' ,;:.')
    if not truncated:
        truncated = sentence[:max_len]
    return truncated + '…'


def _assign_cluster_tiers(clusters: list[dict], visible_count: int = 5) -> None:
    """Rank clusters by title_count (desc) and assign tier + fold state in place.

    Rank 1 -> 'big', ranks 2-4 -> 'medium', rank 5+ -> 'small'.
    Ranks beyond visible_count are marked folded=True (hidden behind "+more").
    """
    ranked = sorted(range(len(clusters)), key=lambda i: -clusters[i]["title_count"])
    for rank, idx in enumerate(ranked, start=1):
        c = clusters[idx]
        if rank == 1:
            c["tier"] = "big"
        elif rank <= 4:
            c["tier"] = "medium"
        else:
            c["tier"] = "small"
        c["folded"] = rank > visible_count


def _md_to_html(text: str) -> str:
    html = str(escape(text))
    html = re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', html)
    html = re.sub(r'\*(.+?)\*', r'<em>\1</em>', html)
    paragraphs = re.split(r'\n\s*\n', html)
    html = ''.join(f'<p>{p.strip()}</p>' for p in paragraphs if p.strip())
    return Markup(html)


def _get_tmdb_overview(tmdb_id: int, content_type: str) -> str | None:
    cache_path = Path(config.CACHE_DIR) / content_type / f"{tmdb_id}.json"
    if cache_path.exists():
        return json.loads(cache_path.read_text()).get("overview") or None
    return None


def _get_poster_url(tmdb_id: int, content_type: str, size: str = "w300") -> str | None:
    cache_path = Path(config.CACHE_DIR) / content_type / f"{tmdb_id}.json"
    if cache_path.exists():
        data = json.loads(cache_path.read_text())
        poster = data.get("poster_path")
        if poster:
            return f"https://image.tmdb.org/t/p/{size}{poster}"
    return None


def _get_recent_posters(entries: list[dict], limit: int = 24) -> list[dict]:
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


def _profile_built_at() -> str | None:
    path = Path(config.TASTE_PROFILE_PATH)
    if not path.exists():
        return None
    mtime = path.stat().st_mtime
    return datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat()


# ── CSRF ──────────────────────────────────────────────────────────────────────

def _get_csrf_token() -> str:
    if "csrf_token" not in session:
        session["csrf_token"] = secrets.token_hex(32)
    return session["csrf_token"]


def _csrf_valid() -> bool:
    expected = session.get("csrf_token")
    if not expected:
        return False
    submitted = (
        request.form.get("_csrf_token")
        or request.headers.get("X-CSRF-Token")
    )
    return submitted == expected


@app.context_processor
def _inject_csrf() -> dict:
    return {"csrf_token": _get_csrf_token()}


@app.context_processor
def _inject_footer() -> dict:
    provider = config.LLM_PROVIDER
    models = config.LLM_MODELS.get(provider, {})
    reason_model = models.get("reason", "")
    return {
        "footer_provider": provider,
        "footer_model": reason_model,
        "footer_profile_built_at": _profile_built_at(),
    }


@app.context_processor
def _inject_watchlist_count() -> dict:
    try:
        _ensure_user_store_once()
        items = user_store.list_saved_titles(config.EVENT_DB_PATH, status="watchlist")
        return {"watchlist_count": len(items)}
    except Exception:
        return {"watchlist_count": 0}


# ── Auth ──────────────────────────────────────────────────────────────────────

@app.before_request
def _check_auth() -> Response | None:
    password = os.environ.get("STREAMLINE_PASSWORD", "").strip()
    if not password:
        return None
    if request.path == "/healthz":
        return None
    auth = request.authorization
    if auth and auth.password == password:
        return None
    return Response(
        "Authentication required",
        401,
        {"WWW-Authenticate": 'Basic realm="Streamline"'},
    )


# ── CSRF guard ────────────────────────────────────────────────────────────────

@app.before_request
def _check_csrf() -> tuple | None:
    if request.method not in ("POST", "DELETE", "PUT", "PATCH"):
        return None
    if not _csrf_valid():
        log.warning("CSRF validation failed for %s %s", request.method, request.path)
        return "Invalid or missing CSRF token", 403
    return None


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/")
def dashboard() -> str:
    ctx = _get_context()
    enrichments = _load_enrichments()
    entries = ctx.watch_index.entries
    tv_count = sum(1 for e in entries if e.get("content_type") == "tv")
    movie_count = sum(1 for e in entries if e.get("content_type") == "movie")

    clusters = []
    current_cluster = None
    for line in ctx.taste_profile.split("\n"):
        if line.startswith("## "):
            if current_cluster:
                clusters.append(current_cluster)
            heading = line[3:].strip()
            heading = re.sub(r'^CLUSTER\s+[A-Z0-9]+:\s*', '', heading)
            current_cluster = {"heading": heading, "body": ""}
        elif current_cluster is not None:
            stripped = line.strip()
            if stripped == '---' or stripped.startswith('**Strength:'):
                continue
            current_cluster["body"] += line + "\n"
    if current_cluster:
        clusters.append(current_cluster)

    for c in clusters:
        essay, titles = _split_cluster_titles(c["body"].strip())
        c["titles"] = titles
        c["title_count"] = len(titles)
        c["tagline"] = _first_sentence(essay)
        c["body_html"] = _md_to_html(essay)
    _assign_cluster_tiers(clusters)

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
    sort = request.args.get("sort", "az")
    platform_filter = request.args.get("platform", "")
    rating_filter = request.args.get("rating", "")

    entries = list(ctx.watch_index.entries)

    # Union manual archive entries that aren't already in the watch index
    _ensure_user_store_once()
    manual = user_store.list_manual_archive(config.EVENT_DB_PATH)

    # Match a manual row to an existing entry by typed TMDB identity when it has
    # one (the schema's unique key), so same-title remakes/editions with distinct
    # ids stay distinct; fall back to (title, type) only for rows without an id.
    by_tmdb: dict[tuple[str, int], int] = {}
    by_title: dict[tuple[str, str], int] = {}
    for i, e in enumerate(entries):
        ct = e.get("content_type", "tv")
        tid = e.get("tmdb_id")
        if tid:
            by_tmdb.setdefault((ct, tid), i)
        by_title.setdefault((_norm_title(e.get("title", "")), ct), i)

    for m in manual:
        ct = m["content_type"]
        tid = m.get("tmdb_id")
        title_key = (_norm_title(m["title"]), ct)
        watched_at = m.get("watched_at", "")

        if tid and (ct, tid) in by_tmdb:
            target = by_tmdb[(ct, tid)]
        elif tid:
            target = None              # distinct id not present yet → new row
        elif title_key in by_title:
            target = by_title[title_key]
        else:
            target = None

        if target is None:
            entries.append({
                "title": m["title"],
                "content_type": ct,
                "tmdb_id": tid,
                "source": m["source"],
                # Manual additions are their own provider category (issue #37).
                "platforms": ["manual"],
                "last_watched": watched_at,
            })
            new_i = len(entries) - 1
            if tid:
                by_tmdb.setdefault((ct, tid), new_i)
            by_title.setdefault(title_key, new_i)
        else:
            # Fold the manual watch into the existing row rather than dropping it,
            # so the 'manual' source filter and recency sort both see it. Copy
            # first — the entry dict is shared with the cached watch index and
            # must not be mutated in place.
            merged = dict(entries[target])
            merged["platforms"] = sorted(set(merged.get("platforms") or []) | {"manual"})
            if watched_at > (merged.get("last_watched") or ""):
                merged["last_watched"] = watched_at
            entries[target] = merged

    # Source-provider options for the filter dropdown, computed over the full
    # set before filtering so every provider stays selectable.
    providers = sorted({p for e in entries for p in (e.get("platforms") or [])})

    if q:
        entries = [e for e in entries if q in e["title"].lower()]
    if ct_filter in ("tv", "movie"):
        entries = [e for e in entries if e.get("content_type") == ct_filter]
    if platform_filter:
        entries = [e for e in entries if platform_filter in (e.get("platforms") or [])]

    if sort == "recent":
        # Most recently watched first; titles with no timestamp sort last.
        entries.sort(key=lambda e: e.get("last_watched") or "", reverse=True)
    elif sort == "za":
        entries.sort(key=lambda e: e["title"].lower(), reverse=True)
    else:
        entries.sort(key=lambda e: e["title"].lower())
    us = _load_user_state()
    items = []
    for e in entries:
        class _M:
            pass
        m = _M()
        m.title = e["title"]
        m.content_type = e.get("content_type", "tv")
        m.tmdb_id = e.get("tmdb_id")
        items.append({
            "title": e["title"],
            "content_type": e.get("content_type", ""),
            "tmdb_id": e.get("tmdb_id"),
            "description": (
                enrichments.get(
                    enrichment_key_from_parts(e.get("content_type", "movie"), e.get("tmdb_id"), e["title"]),
                )
                or enrichments.get(e["title"], "")
                or (
                    _get_tmdb_overview(e["tmdb_id"], e.get("content_type", "movie"))
                    if e.get("tmdb_id") else None
                )
                or ""
            ),
            "poster": _get_poster_url(e.get("tmdb_id", 0), e.get("content_type", "movie"), "w185")
                      if e.get("tmdb_id") else None,
            "rating": us.get_rating(m),
            "platforms": e.get("platforms") or [],
            "last_watched": e.get("last_watched") or "",
        })

    if rating_filter == "liked":
        items = [it for it in items if it["rating"] == "liked"]
    elif rating_filter == "disliked":
        items = [it for it in items if it["rating"] == "disliked"]
    elif rating_filter == "unrated":
        items = [it for it in items if not it["rating"]]

    total = len(items)
    ALLOWED_PAGE_SIZES = (30, 60, 120)
    try:
        per_page = int(request.args.get("per_page", "60"))
    except (ValueError, TypeError):
        per_page = 60
    if per_page not in ALLOWED_PAGE_SIZES:
        per_page = 60
    try:
        page = max(1, int(request.args.get("page", "1")))
    except (ValueError, TypeError):
        page = 1
    total_pages = max(1, (total + per_page - 1) // per_page)
    page = min(page, total_pages)
    start = (page - 1) * per_page
    paged_items = items[start : start + per_page]
    return render_template(
        "history.html",
        items=paged_items,
        q=q,
        ct_filter=ct_filter,
        sort=sort,
        platform_filter=platform_filter,
        rating_filter=rating_filter,
        providers=providers,
        platform_labels=PLATFORM_LABELS,
        total=total,
        page=page,
        total_pages=total_pages,
        per_page=per_page,
    )


@app.route("/searches")
def searches() -> str:
    entries = query_history.load()
    us = _load_user_state()
    for e in entries:
        ts = e.get("timestamp", "")
        try:
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
        for r in e.get("results", []):
            r["user_state"] = _title_state(r["title"], r["content_type"], us, r.get("tmdb_id"))
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


@app.route("/recommend", methods=["GET"])
def recommend_page() -> str:
    return render_template("recommend.html", query="")


@app.route("/recommend", methods=["POST"])
def recommend_post() -> str:
    query = (request.form.get("query") or "").strip()
    is_htmx = request.headers.get("HX-Request") == "true"

    if not query:
        if is_htmx:
            return render_template("_results.html", results=None, query="", error=None)
        return render_template("recommend.html", query="")

    if is_htmx:
        job_id = job_registry.submit(_run_recommend_job, query, label="recommend")
        return render_template("_polling.html", job_id=job_id)

    # Non-HTMX: run synchronously and return full page
    try:
        ctx = _get_job_context()
        results = ask(query, ctx)
        items = _build_result_items(results, ctx)
        try:
            query_history.record(query, items, ctx.llm.provider, ctx.llm.usage.summary())
        except OSError as exc:
            log.warning("Failed to persist query history for %r: %s", query, exc)
        return render_template("recommend.html", query=query, results=items)
    except Exception as exc:
        log.exception("Error during recommendation")
        return render_template("recommend.html", query=query, results=None, error=str(exc))


@app.route("/jobs/<job_id>/poll")
def poll_job(job_id: str) -> str:
    """HTMX polling endpoint for recommendation jobs."""
    job = job_registry.get(job_id)
    if job is None:
        return render_template("_results.html", results=None, query="", error="Job not found.")

    if job.status in ("pending", "running"):
        return render_template("_polling.html", job_id=job_id, elapsed=job.elapsed_seconds)

    if job.status == "error":
        return render_template("_results.html", results=None, query="", error=job.error)

    result = job.result
    return render_template("_results.html", results=result["items"], query=result["query"], error=None)


@app.route("/wizard")
def wizard_page() -> str:
    return render_template("wizard.html", max_questions=config.WIZARD_MAX_QUESTIONS)


def _wizard_question_number(state: wizard.WizardState) -> int:
    """Position used for the progress display.

    Content type is question 1; each adaptive question counts up from there.
    """
    if state.step == "content_type":
        return 1
    # Content type already counted in turn_count, so the next adaptive question
    # is turn_count + 1 (e.g. after content type, turn_count=1 → question 2).
    if state.step == "adaptive":
        return state.turn_count + 1
    return state.turn_count + 1


def _render_wizard_turn(turn: dict, state: wizard.WizardState) -> str:
    """Render a deterministic or adaptive ask turn."""
    return render_template(
        "_wizard_step.html", turn=turn, state_json=state.to_json(),
        question_number=_wizard_question_number(state),
        max_questions=config.WIZARD_MAX_QUESTIONS)


def _render_wizard_state(state: wizard.WizardState) -> str:
    """Render whatever the current deterministic step / review surface is."""
    turn = wizard_flow.current_turn(state)
    if turn["action"] == "review":
        state.review_seen = True
        return render_template("_wizard_review.html", review=turn,
                               state_json=state.to_json())
    return _render_wizard_turn(turn, state)


def _wizard_polling(job_id: str) -> str:
    return render_template("_polling.html", job_id=job_id,
                           poll_url=f"/wizard/jobs/{job_id}/poll",
                           poll_target="#wizard-stage")


def _start_wizard_seed_job(state: wizard.WizardState) -> str:
    """Build a recommendation from structured answers alone — no LLM turn."""
    intent_dict, context_note, summary = wizard_flow.build_recommendation_seed(state)
    job_id = job_registry.submit(
        _run_wizard_recommend_job, intent_dict, context_note, summary, label="wizard")
    return _wizard_polling(job_id)


def _combine_notes(*notes: str) -> str:
    return "\n".join(n.strip() for n in notes if n and n.strip()).strip()


def _finish_with_adaptive(state: wizard.WizardState, intent, context_note: str,
                          summary: str) -> str:
    """Merge an adaptive LLM turn over the deterministic seed and submit."""
    seed, seed_note, seed_summary = wizard_flow.build_recommendation_seed(state)
    merged = wizard_flow.merge_intent_with_seed(intent, seed)
    note = _combine_notes(seed_note, context_note)
    job_id = job_registry.submit(
        _run_wizard_recommend_job, asdict(merged), note,
        summary or seed_summary, label="wizard")
    return _wizard_polling(job_id)


def _wizard_adaptive_turn(state: wizard.WizardState, *, answering: bool) -> str:
    """Drive one turn of the LLM-led adaptive loop.

    ``answering=True`` forces the model to finalize and submits; otherwise it
    asks the next profile-grounded question (or recommends if it has enough
    signal, or the question cap was hit).
    """
    state.step = "adaptive"
    ctx = _get_job_context()
    turn = wizard.next_turn(state, ctx, force_finish=answering)
    if turn["action"] == "recommend":
        return _finish_with_adaptive(state, turn["intent"], turn["context_note"],
                                     turn["summary"])
    turn["step"] = "adaptive"
    turn["can_go_back"] = False
    return _render_wizard_turn(turn, state)


@app.route("/wizard/next", methods=["POST"])
def wizard_next() -> str:
    form = request.form
    state = wizard.WizardState.from_json(form.get("state", ""))
    finish = form.get("finish") == "1"
    posted_step = (form.get("step") or "").strip()

    try:
        # 1. Instant deterministic first step: content type (never an LLM call).
        #    It counts as question 1 toward the cap.
        if posted_step == "content_type":
            wizard_flow.apply_answer(state, "content_type", form.getlist("selected"), "")
            state.step = "adaptive"
            state.turn_count += 1
            if finish:
                return _start_wizard_seed_job(state)
            return _wizard_adaptive_turn(state, answering=False)

        # 2. Start, or no content type yet → render the content-type tap, no LLM.
        if not state.answers.get("content_type"):
            state.step = "content_type"
            return _render_wizard_state(state)

        # 3. Record the current adaptive answer FIRST, so finishing never drops it.
        prompt = (form.get("prompt") or "").strip()
        if prompt:
            state.turns.append({
                "prompt": prompt,
                "selected": form.getlist("selected"),
                "free_text": (form.get("free_text") or "").strip(),
            })
            state.turn_count += 1

        # 4. Early exit → finalize from the conversation, or from content type alone.
        if finish:
            if state.turns:
                return _wizard_adaptive_turn(state, answering=True)
            return _start_wizard_seed_job(state)

        # 5. Continue the LLM-led loop.
        return _wizard_adaptive_turn(state, answering=False)
    except Exception as exc:   # network / provider failure
        log.exception("Wizard turn failed")
        return render_template("_wizard_step.html", error=str(exc),
                               state_json=state.to_json())


@app.route("/wizard/jobs/<job_id>/poll")
def wizard_poll(job_id: str) -> str:
    job = job_registry.get(job_id)
    if job is None:
        return render_template("_wizard_results.html", results=None,
                               error="Session expired. Start again.", query="")
    if job.status in ("pending", "running"):
        return render_template("_polling.html", job_id=job_id,
                               poll_url=f"/wizard/jobs/{job_id}/poll",
                               poll_target="#wizard-stage",
                               elapsed=job.elapsed_seconds)
    if job.status == "error":
        return render_template("_wizard_results.html", results=None, error=job.error, query="")
    result = job.result
    shown = [
        {"title": i["title"], "content_type": i["content_type"], "tmdb_id": i.get("tmdb_id")}
        for i in result["items"]
    ]
    return render_template("_wizard_results.html", results=result["items"],
                           summary=result["summary"], error=None,
                           query=result["summary"],
                           intent_json=json.dumps(result["intent_dict"]),
                           context_note=result["context_note"],
                           shown=json.dumps(shown))


# Default runtime ceiling a "shorter" refine applies when none is set yet,
# and the step it shaves off an existing ceiling (with a floor).
_SHORTER_DEFAULT_RUNTIME = {"movie": 100, "tv": 45}
_SHORTER_STEP_MINUTES = 30
_SHORTER_FLOOR_MINUTES = 30


def _apply_refinement(intent_dict: dict, context_note: str, directive: str) -> tuple[dict, str]:
    """Translate a refine directive into structured intent + context changes.

    Constraints (runtime) are updated on the intent where a clean mapping exists;
    softer directives append a ranker hint to the context note. Deterministic so
    refinement behaves like a command rather than a vague prose nudge.
    """
    intent = dict(intent_dict)
    note = context_note or ""
    d = (directive or "").strip().lower()

    if d == "shorter":
        current = intent.get("max_runtime_minutes")
        if current:
            intent["max_runtime_minutes"] = max(_SHORTER_FLOOR_MINUTES,
                                                 current - _SHORTER_STEP_MINUTES)
        else:
            ct = intent.get("content_type", "both")
            intent["max_runtime_minutes"] = _SHORTER_DEFAULT_RUNTIME.get(ct, 90)
        note = f"{note}\nRefinement: keep it shorter than {intent['max_runtime_minutes']} minutes.".strip()
    elif d == "lighter":
        note = f"{note}\nRefinement: prefer lighter, easier-going picks.".strip()
    elif d == "more obscure":
        note = f"{note}\nRefinement: prefer more obscure, lesser-known picks over obvious ones.".strip()
    elif d == "surprise me":
        note = f"{note}\nRefinement: surprise me — keep the same constraints but add variety and novelty.".strip()
    elif d:
        note = f"{note}\nRefinement: {directive}.".strip()

    return intent, note


def _parse_shown(raw: str) -> list[str]:
    """Extract shown titles to hard-exclude. Accepts a JSON list of items
    (``{"title", "content_type", "tmdb_id"}``) or a legacy comma-joined string."""
    raw = (raw or "").strip()
    if not raw:
        return []
    try:
        data = json.loads(raw)
        if isinstance(data, list):
            titles = []
            for item in data:
                if isinstance(item, dict) and item.get("title"):
                    titles.append(str(item["title"]).strip())
                elif isinstance(item, str) and item.strip():
                    titles.append(item.strip())
            return titles
    except (json.JSONDecodeError, ValueError):
        pass
    return [t.strip() for t in raw.split(",") if t.strip()]


@app.route("/wizard/refine", methods=["POST"])
def wizard_refine():
    try:
        intent_dict = _safe_intent_dict(request.form.get("intent", ""))
    except (ValueError, json.JSONDecodeError):
        return "Invalid or missing refine payload.", 400
    base_note = request.form.get("context_note", "")
    directive = request.form.get("directive", "")     # e.g. "lighter"
    intent_dict, note = _apply_refinement(intent_dict, base_note, directive)
    # Hard-exclude the titles already shown so a refine never repeats them.
    exclude = _parse_shown(request.form.get("shown", ""))
    summary = request.form.get("summary", "your picks")
    job_id = job_registry.submit(
        _run_wizard_recommend_job, intent_dict, note, summary, exclude, label="wizard")
    return render_template("_polling.html", job_id=job_id,
                           poll_url=f"/wizard/jobs/{job_id}/poll",
                           poll_target="#wizard-stage")


@app.route("/wizard/replay", methods=["POST"])
def wizard_replay():
    """Replay a stored wizard run from its structured intent, not its recap text."""
    try:
        intent_dict = _safe_intent_dict(request.form.get("intent", ""))
    except (ValueError, json.JSONDecodeError):
        return "Invalid or missing replay payload.", 400
    context_note = request.form.get("context_note", "")
    summary = request.form.get("summary", "your picks")
    job_id = job_registry.submit(
        _run_wizard_recommend_job, intent_dict, context_note, summary, label="wizard")
    # Full-page navigation from the Searches page: render the wizard shell with
    # the polling stage already active so results land in #wizard-stage.
    return render_template("wizard.html", max_questions=config.WIZARD_MAX_QUESTIONS,
                           replay_job_id=job_id)


@app.route("/title/<int:tmdb_id>")
def title_detail(tmdb_id: int) -> str:
    ct = request.args.get("type", "tv")
    ctx = _get_context()
    enrichments = _load_enrichments()
    meta = None
    try:
        meta = ctx.tmdb_client.get_cached_by_id(tmdb_id, ct)
    except Exception:
        pass

    # Fallback: if the requested type has no cache, check if the watch index
    # has this tmdb_id under the alternate type (content-type mismatch fix).
    if meta is None:
        alt_ct = "movie" if ct == "tv" else "tv"
        try:
            alt_meta = ctx.tmdb_client.get_cached_by_id(tmdb_id, alt_ct)
        except Exception:
            alt_meta = None
        if alt_meta:
            if _can_use_alternate_title_cache(ctx.watch_index, tmdb_id, alt_ct, alt_meta.title):
                meta = alt_meta
                ct = alt_ct

    description = ""
    enrichment_path = Path(config.ENRICHMENT_CACHE_DIR) / ct / f"{tmdb_id}.txt"
    if enrichment_path.exists():
        description = enrichment_path.read_text()
    elif meta:
        key = enrichment_key_from_parts(ct, tmdb_id, meta.title)
        description = enrichments.get(key) or enrichments.get(meta.title, "")
    poster = _get_poster_url(tmdb_id, ct, "w500") if meta else None
    overview = ""
    if meta:
        cache_path = Path(config.CACHE_DIR) / ct / f"{tmdb_id}.json"
        if cache_path.exists():
            raw = json.loads(cache_path.read_text())
            overview = raw.get("overview", "")
    us = _load_user_state()
    state = _title_state(meta.title if meta else "", ct, us, tmdb_id if meta else None)
    # Also count watch-index entries (imported from Netflix/Prime/etc.) as archived
    if not state["in_archive"] and meta:
        if (ct, tmdb_id) in ctx.watch_index.tmdb_keys:
            state["in_archive"] = True
    return render_template(
        "title.html", meta=meta, description=description, overview=overview,
        tmdb_id=tmdb_id, ct=ct, poster=poster, user_state=state,
    )


def _load_user_state():
    """Load fresh user state snapshot for the current request."""
    _ensure_user_store_once()
    from recommender.user_state import UserStateIndex
    return UserStateIndex.load(config.EVENT_DB_PATH)


def _title_state(title: str, content_type: str, us: "UserStateIndex",
                 tmdb_id: int | None = None) -> dict:
    """Return user state for a title, used across templates."""
    class _Meta:
        pass
    meta = _Meta()
    meta.title = title
    meta.content_type = content_type
    meta.tmdb_id = tmdb_id
    return {
        "in_archive": us.is_manually_watched(meta),
        "in_watchlist": us.is_in_watchlist(meta),
        "is_dismissed": us.is_dismissed(meta),
        "rating": us.get_rating(meta),
    }


def _watchlist_saved_fragment(title: str, ct: str, tmdb_id: int | None, target_id: str) -> str:
    """HTML fragment: 'Saved ×' toggle, used when saving from searches history."""
    csrf = _get_csrf_token()
    t = escape(title)
    tid = escape(target_id)
    tmdb_val = tmdb_id if tmdb_id is not None else ""
    return Markup(
        f'<span id="{tid}" style="display:inline-flex; align-items:center; gap:0.3rem;">'
        f'<span class="mono" style="font-size:0.58rem; color:var(--teal);">Saved</span>'
        f'<form hx-post="/watchlist/unsave" hx-target="#{tid}" hx-swap="outerHTML" style="margin:0; display:inline;">'
        f'<input type="hidden" name="_csrf_token" value="{csrf}">'
        f'<input type="hidden" name="title" value="{t}">'
        f'<input type="hidden" name="content_type" value="{escape(ct)}">'
        f'<input type="hidden" name="tmdb_id" value="{tmdb_val}">'
        f'<input type="hidden" name="target_id" value="{tid}">'
        f'<button type="submit" class="mono" style="font-size:0.55rem; background:none; border:none; cursor:pointer; color:var(--muted); padding:0 2px;" title="Remove from watchlist">&times;</button>'
        f'</form></span>'
    )


def _watchlist_save_fragment(title: str, ct: str, tmdb_id: int | None, target_id: str) -> str:
    """HTML fragment: '+ Save' toggle, returned after unsaving from searches history."""
    csrf = _get_csrf_token()
    t = escape(title)
    tid = escape(target_id)
    tmdb_val = tmdb_id if tmdb_id is not None else ""
    return Markup(
        f'<span id="{tid}" style="display:inline-flex; align-items:center;">'
        f'<form hx-post="/watchlist/save" hx-target="#{tid}" hx-swap="outerHTML" style="margin:0; display:inline;">'
        f'<input type="hidden" name="_csrf_token" value="{csrf}">'
        f'<input type="hidden" name="title" value="{t}">'
        f'<input type="hidden" name="content_type" value="{escape(ct)}">'
        f'<input type="hidden" name="tmdb_id" value="{tmdb_val}">'
        f'<input type="hidden" name="mode" value="toggle">'
        f'<input type="hidden" name="target_id" value="{tid}">'
        f'<button type="submit" class="mono result-link" style="background:none; border:none; cursor:pointer; padding:0;">+ Save</button>'
        f'</form></span>'
    )


# ── Watchlist routes ──────────────────────────────────────────────────────────

# Display names for internal ingestion source codes (archive "source" filter).
# Plain title-casing mangles these (Hbo, Apple Tv), so map them explicitly.
PLATFORM_LABELS = {
    "netflix": "Netflix",
    "prime": "Prime Video",
    "apple_tv": "Apple TV",
    "disney": "Disney+",
    "hbo": "HBO Max",
    "manual": "Manual",
}


# Brand grouping for granular TMDB streaming-provider names (consolidates
# ad-supported tiers, channel resells, and aliases). Mirrors the client-side
# canonicalProvider() on the Searches page.
_PROVIDER_SUFFIXES = (" with ads", " amazon channel", " apple tv channel",
                      " roku premium channel")
_PROVIDER_BRANDS = (
    ("netflix", "Netflix"),
    ("amazon prime", "Amazon Prime Video"),
    ("prime video", "Amazon Prime Video"),
    ("disney", "Disney+"),
    ("paramount", "Paramount+"),
    ("britbox", "BritBox"),
    ("acorn", "Acorn TV"),
    ("apple tv", "Apple TV+"),
    ("hulu", "Hulu"),
    ("peacock", "Peacock"),
)


def _canonical_provider(raw: str) -> str:
    low = (raw or "").strip().lower()
    for suffix in _PROVIDER_SUFFIXES:
        if low.endswith(suffix):
            low = low[: -len(suffix)].strip()
    for keyword, label in _PROVIDER_BRANDS:
        if keyword in low:
            return label
    return low.title()


def _consolidate_providers(names: list[str]) -> list[str]:
    """Collapse TMDB provider variants to one entry per brand, preserving order."""
    out: list[str] = []
    seen: set[str] = set()
    for name in names or []:
        label = _canonical_provider(name)
        if label and label not in seen:
            seen.add(label)
            out.append(label)
    return out


def _decorate_saved_item(item: dict, ctx) -> None:
    """Attach cached poster, rating, genres, providers, and links to a saved title.

    Cache-first so the page stays instant. A title saved on another machine may
    have no cached metadata yet; when it has a tmdb_id we fetch and cache its
    detail JSON once so the card fills in. All network work is best-effort and
    never breaks the page render.
    """
    item.setdefault("vote_average", 0.0)
    item.setdefault("genres", [])
    item.setdefault("streaming_providers", [])
    item.setdefault("poster", None)
    item.setdefault("tmdb_url", "")
    item.setdefault("imdb_url", "")
    tmdb_id = item.get("tmdb_id")
    ct = item.get("content_type")
    if not (ctx and tmdb_id and ct):
        return
    tmdb = ctx.tmdb_client
    meta = tmdb.get_cached_by_id(tmdb_id, ct)
    if meta is None and config.TMDB_API_KEY:
        # One-time backfill for titles saved elsewhere with no cached metadata.
        try:
            data = tmdb._fetch_details(tmdb_id, ct)
            tmdb._save_cache(ct, tmdb_id, data)
            meta = tmdb.get_cached_by_id(tmdb_id, ct)
        except Exception:
            meta = None
    if meta is None:
        return
    item["vote_average"] = meta.vote_average or 0.0
    item["genres"] = meta.genres[:3]
    item["poster"] = _get_poster_url(tmdb_id, ct)
    tmdb_type = "tv" if ct == "tv" else "movie"
    item["tmdb_url"] = f"https://www.themoviedb.org/{tmdb_type}/{tmdb_id}"
    raw = tmdb._load_cache(ct, tmdb_id) or {}
    imdb_id = raw.get("imdb_id")
    if imdb_id:
        item["imdb_url"] = f"https://www.imdb.com/title/{imdb_id}/"
    else:
        from urllib.parse import quote
        item["imdb_url"] = f"https://www.imdb.com/find/?q={quote(item['title'])}"
    try:
        item["streaming_providers"] = _consolidate_providers(
            tmdb.get_watch_providers(
                tmdb_id, ct, config.WATCH_REGION, config.PROVIDERS_CACHE_DIR))[:4]
    except Exception:
        item["streaming_providers"] = []


@app.route("/watchlist")
def watchlist_page() -> str:
    _ensure_user_store_once()
    items = user_store.list_saved_titles(config.EVENT_DB_PATH, status="watchlist")
    try:
        ctx = _get_context()
    except Exception:
        ctx = None
    for item in items:
        _decorate_saved_item(item, ctx)
    return render_template("watchlist.html", items=items)


@app.route("/watchlist/export")
def watchlist_export():
    _ensure_user_store_once()
    items = user_store.list_saved_titles(config.EVENT_DB_PATH, status="watchlist")
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["title", "content_type", "tmdb_id", "saved_at"])
    for item in items:
        writer.writerow([item["title"], item["content_type"],
                         item.get("tmdb_id") or "", item.get("saved_at") or ""])
    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=watchlist.csv"},
    )


@app.route("/watchlist/save", methods=["POST"])
def watchlist_save() -> str:
    title = (request.form.get("title") or "").strip()
    ct = request.form.get("content_type", "tv")
    tmdb_id = request.form.get("tmdb_id", type=int)
    mode = request.form.get("mode", "")
    target_id = (request.form.get("target_id") or "").strip()
    if not title:
        return "Missing title", 400
    _ensure_user_store_once()
    user_store.save_title(config.EVENT_DB_PATH, title, ct, tmdb_id=tmdb_id)
    if mode == "toggle" and target_id:
        return _watchlist_saved_fragment(title, ct, tmdb_id, target_id)
    return '<span class="mono" style="font-size:0.58rem; color:var(--teal);">Saved</span>'


@app.route("/watchlist/unsave", methods=["POST"])
def watchlist_unsave() -> str:
    title = (request.form.get("title") or "").strip()
    ct = request.form.get("content_type", "tv")
    tmdb_id = request.form.get("tmdb_id", type=int)
    target_id = (request.form.get("target_id") or "").strip()
    if not title:
        return "Missing title", 400
    _ensure_user_store_once()
    user_store.remove_saved_title(config.EVENT_DB_PATH, title, ct, tmdb_id=tmdb_id)
    return _watchlist_save_fragment(title, ct, tmdb_id, target_id)


@app.route("/watchlist/dismiss", methods=["POST"])
def watchlist_dismiss() -> str:
    title = (request.form.get("title") or "").strip()
    ct = request.form.get("content_type", "tv")
    tmdb_id = request.form.get("tmdb_id", type=int)
    if not title:
        return "Missing title", 400
    _ensure_user_store_once()
    user_store.dismiss_title(config.EVENT_DB_PATH, title, ct, tmdb_id=tmdb_id)
    return ""


@app.route("/watchlist/remove", methods=["POST"])
def watchlist_remove() -> str:
    title = (request.form.get("title") or "").strip()
    ct = request.form.get("content_type", "tv")
    tmdb_id = request.form.get("tmdb_id", type=int)
    if not title:
        return "Missing title", 400
    _ensure_user_store_once()
    user_store.remove_saved_title(config.EVENT_DB_PATH, title, ct, tmdb_id=tmdb_id)
    return ""


@app.route("/watchlist/watched", methods=["POST"])
def watchlist_watched() -> str:
    title = (request.form.get("title") or "").strip()
    ct = request.form.get("content_type", "tv")
    tmdb_id = request.form.get("tmdb_id", type=int)
    if not title:
        return "Missing title", 400
    _ensure_user_store_once()
    user_store.mark_watched_from_watchlist(config.EVENT_DB_PATH, title, ct, tmdb_id=tmdb_id)
    uid = f"wl-{hash(title) & 0xFFFFFF:06x}"
    return render_template("_rating_prompt.html", title=title, content_type=ct,
                           tmdb_id=tmdb_id, uid=uid, context="watchlist")


# ── Archive routes ────────────────────────────────────────────────────────────

@app.route("/archive/add", methods=["POST"])
def archive_add() -> str:
    title = (request.form.get("title") or "").strip()
    ct = request.form.get("content_type", "tv")
    tmdb_id = request.form.get("tmdb_id", type=int)
    if not title:
        return "Missing title", 400
    _ensure_user_store_once()
    if not tmdb_id and config.TMDB_API_KEY:
        try:
            from recommender.tmdb_client import TmdbClient
            tmdb = TmdbClient(api_key=config.TMDB_API_KEY, cache_dir=config.CACHE_DIR)
            resolved_id, resolved_ct = tmdb.resolve_title_confident(title, ct)
            if resolved_id:
                tmdb_id = resolved_id
                ct = resolved_ct
                # Fetch and cache detail JSON so overview/poster are available immediately.
                if not tmdb._load_cache(ct, resolved_id):
                    try:
                        data = tmdb._fetch_details(resolved_id, ct)
                        tmdb._save_cache(ct, resolved_id, data)
                    except Exception:
                        pass
        except Exception:
            pass
    user_store.add_to_archive(config.EVENT_DB_PATH, title, ct, tmdb_id=tmdb_id, source="web")
    Path(config.PROFILE_STALE_FLAG).touch()
    uid = f"aa-{hash(title) & 0xFFFFFF:06x}"
    return render_template("_rating_prompt.html", title=title, content_type=ct,
                           tmdb_id=tmdb_id, uid=uid)


@app.route("/archive/rate", methods=["POST"])
def archive_rate() -> str:
    title = (request.form.get("title") or "").strip()
    ct = request.form.get("content_type", "tv")
    rating = request.form.get("rating", "")
    tmdb_id = request.form.get("tmdb_id", type=int)
    context = request.form.get("context", "")
    if not title:
        return "Missing title", 400
    if rating == "skip":
        return "" if context == "watchlist" else '<span class="mono" style="font-size:0.58rem; color:var(--teal);">added</span>'
    _ensure_user_store_once()
    user_store.rate_title(config.EVENT_DB_PATH, title, ct, rating, tmdb_id=tmdb_id)
    if context == "watchlist":
        return ""
    current = None if rating == "clear" else rating
    uid = f"rt-{hash(title) & 0xFFFFFF:06x}"
    return render_template("_rating_state.html", title=title, content_type=ct,
                           tmdb_id=tmdb_id, current_rating=current, uid=uid)


@app.route("/help")
def help_page() -> str:
    return render_template("help.html")


@app.route("/healthz")
def healthz():
    index_path = Path(config.WATCH_INDEX_PATH)
    profile_path = Path(config.TASTE_PROFILE_PATH)
    if not index_path.exists() or not profile_path.exists():
        return jsonify({"status": "not ready", "reason": "setup not run"}), 503
    running = job_registry.running_jobs()
    if running:
        return jsonify({"status": "busy", "running_jobs": len(running)}), 200
    return jsonify({"status": "ok"}), 200


def _resolve_log_path(log_name: str | None) -> tuple[str, Path]:
    requested_name = (log_name or "app").strip().lower()
    if requested_name == "web":
        return "web", Path(config.APP_LOG_PATH).with_name("web.log")
    return "app", Path(config.APP_LOG_PATH)


def _read_log_lines(log_name: str, n: int) -> list[str]:
    _, log_path = _resolve_log_path(log_name)
    if not log_path.exists():
        return []
    try:
        raw = log_path.read_text(encoding="utf-8", errors="replace")
        return raw.splitlines()[-n:]
    except OSError:
        return []


def _parse_n_value(raw_value: str | None) -> int:
    try:
        return min(int(raw_value or 200), 1000)
    except (ValueError, TypeError):
        return 200


def _parse_n() -> int:
    return _parse_n_value(request.args.get("n", 200))


@app.route("/logs")
def logs_page() -> str:
    n = _parse_n()
    log_name, log_path = _resolve_log_path(request.args.get("file"))
    lines = _read_log_lines(log_name, n)
    return render_template(
        "logs.html",
        lines=lines,
        log_name=log_name,
        log_path=str(log_path),
        n=n,
    )


@app.route("/logs/lines")
def logs_lines() -> str:
    """Partial used by HTMX refresh — returns only the log line fragment."""
    n = _parse_n()
    log_name, _ = _resolve_log_path(request.args.get("file"))
    lines = _read_log_lines(log_name, n)
    return render_template("_log_lines.html", lines=lines)


@app.route("/logs/clear", methods=["POST"])
def logs_clear() -> Response:
    log_name, log_path = _resolve_log_path(request.form.get("file") or request.args.get("file"))
    n = _parse_n_value(request.form.get("n") or request.args.get("n"))
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text("", encoding="utf-8")
    except OSError as exc:
        return Response(f"Failed to clear {log_name} log: {exc}", status=500)
    return redirect(url_for("logs_page", file=log_name, n=n))


@app.route("/status")
def status():
    index_entries = 0
    try:
        ctx = _get_context()
        index_entries = len(ctx.watch_index.entries)
    except Exception:
        pass

    enrichment_count = 0
    enrichment_index = Path(config.ENRICHMENT_CACHE_DIR) / "index.json"
    if enrichment_index.exists():
        try:
            enrichment_count = len(json.loads(enrichment_index.read_text()))
        except Exception:
            pass

    # Event store info
    try:
        import_info = event_store.get_import_info(config.EVENT_DB_PATH)
    except Exception:
        import_info = {}

    running = job_registry.running_jobs()
    return jsonify({
        "status": "ok",
        "provider": config.LLM_PROVIDER,
        "models": config.LLM_MODELS.get(config.LLM_PROVIDER, {}),
        "watch_index_entries": index_entries,
        "enrichment_count": enrichment_count,
        "taste_profile_built_at": _profile_built_at(),
        "event_store_ready": len(import_info) > 0,
        "event_store_import_count": len(import_info),
        "event_store_path": config.EVENT_DB_PATH,
        "running_jobs": len(running),
        "jobs": [
            {
                "id": j.id,
                "label": j.label,
                "status": j.status,
                "elapsed_seconds": round(j.elapsed_seconds, 1),
                "error": j.error,
            }
            for j in job_registry.recent_jobs(5)
        ],
    })


# ── Settings ──────────────────────────────────────────────────────────────────

_CONFIG_PATH = Path(__file__).parent.parent / "config.yaml"
_DEFAULT_LLM_API_KEY_ENVS = dict(config.LLM_DEFAULT_API_KEY_ENVS)
_SETTINGS_DEFAULTS = {
    "provider": "anthropic",
    "models": {
        "anthropic": {"fast": "claude-haiku-4-5-20251001", "reason": "claude-sonnet-4-6"},
        "gemini": {"fast": "gemini-2.5-flash", "reason": "gemini-2.5-flash"},
        "openai": {"fast": "gpt-4.1-mini", "reason": "gpt-4.1", "base_url": None},
    },
    "llm": {
        "timeout_fast": 30, "timeout_reason": 60,
        "timeout_profile_batch": 60, "timeout_profile_merge": 300,
        "tokens_fast": 200, "tokens_intent": 400, "tokens_ranking": 1000,
        "tokens_suggestions": 300, "tokens_profile_batch": 8000,
        "tokens_profile_merge": 16000, "tokens_abandoned": 300,
        "profile_batch_size": 200, "rate_limit_wait": 65,
    },
    "scoring": {
        "weight_completion": 0.5, "weight_rewatch": 0.3, "weight_recency": 0.2,
        "default_tv_runtime": 45, "default_movie_runtime": 90, "rewatch_saturation": 5,
    },
    "default_top_n": 3, "min_vote_count": 20, "min_rating": 0, "min_year": 0,
    "recency_half_life_days": 90, "watch_region": "US", "streaming_platforms": [],
    "manual": {"timestamp": "now", "tv_duration_minutes": 45, "movie_duration_minutes": 120},
    "log_level": "WARNING",
}

_PROFILE_REBUILD_LLM_KEYS = (
    "timeout_profile_batch", "timeout_profile_merge",
    "tokens_profile_batch", "tokens_profile_merge",
    "profile_batch_size", "rate_limit_wait",
)
_PROFILE_REBUILD_SCORING_KEYS = (
    "weight_completion", "weight_rewatch", "weight_recency",
    "default_tv_runtime", "default_movie_runtime", "rewatch_saturation",
)


def _load_config_yaml() -> dict:
    with open(_CONFIG_PATH) as f:
        return yaml.safe_load(f) or {}


def _save_config_yaml(cfg: dict) -> None:
    with open(_CONFIG_PATH, "w") as f:
        f.write("# Streamline configuration\n")
        f.write("# Set API keys in the environment. .env is optional local convenience.\n\n")
        f.write("# Put machine-specific watch-history paths in config.local.yaml.\n\n")
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
    rebuild_command: str | None = None,
    reload_deferred: bool = False,
) -> str:
    resolved_cfg = _resolve_settings_config(cfg if cfg is not None else _load_config_yaml())
    return render_template(
        "settings.html", cfg=resolved_cfg, saved=saved, error=error,
        rebuild_command=rebuild_command,
        reload_deferred=reload_deferred,
    )


def _apply_runtime_config_reload_locked() -> None:
    global _ctx, _config_reload_pending
    importlib.reload(config)
    _ctx = None  # next request will rebuild context
    _config_reload_pending = False


def _reload_app_config() -> bool:
    global _config_reload_pending
    with _ctx_lock:
        running = job_registry.running_jobs()
        if running:
            _config_reload_pending = True
            log.warning(
                "Deferring config reload — %d job(s) still running: %s",
                len(running),
                ", ".join(j.label for j in running),
            )
            return False
        _apply_runtime_config_reload_locked()
    log.info("Config reloaded from config.yaml with config.local.yaml overrides if present")
    return True


def _maybe_apply_deferred_reload(_job=None) -> None:
    with _ctx_lock:
        if not _config_reload_pending:
            return
        if job_registry.running_jobs():
            return
        _apply_runtime_config_reload_locked()
    log.info("Applied deferred config reload after background jobs finished")


job_registry.add_completion_callback(_maybe_apply_deferred_reload)


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
    refresh_profile = refresh_data or any(
        previous_cfg["scoring"][key] != updated_cfg["scoring"][key]
        for key in _PROFILE_REBUILD_SCORING_KEYS
    )
    refresh_profile = refresh_profile or (
        previous_cfg["recency_half_life_days"] != updated_cfg["recency_half_life_days"]
    )
    refresh_profile = refresh_profile or any(
        previous_cfg["llm"][key] != updated_cfg["llm"][key]
        for key in _PROFILE_REBUILD_LLM_KEYS
    )
    return refresh_profile, refresh_data


@app.route("/settings", methods=["GET"])
def settings_page() -> str:
    saved = request.args.get("saved")
    rebuild_command = request.args.get("rebuild_command")
    reload_deferred = request.args.get("reload_deferred") == "1"
    return _render_settings_page(
        saved=saved, rebuild_command=rebuild_command,
        reload_deferred=reload_deferred,
    )


@app.route("/settings", methods=["POST"])
def settings_save() -> str:
    cfg = _load_config_yaml()
    current_cfg = _resolve_settings_config(cfg)
    form = request.form

    try:
        llm_values = {
            key: _parse_int_field(form, f"llm_{key}", current_cfg["llm"][key])
            for key in (
                "timeout_fast", "timeout_reason", "timeout_profile_batch", "timeout_profile_merge",
                "tokens_fast", "tokens_intent", "tokens_ranking", "tokens_suggestions",
                "tokens_profile_batch", "tokens_profile_merge", "tokens_abandoned",
                "profile_batch_size", "rate_limit_wait",
            )
        }
        scoring_values = {
            "weight_completion": _parse_float_field(form, "weight_completion", current_cfg["scoring"]["weight_completion"]),
            "weight_rewatch": _parse_float_field(form, "weight_rewatch", current_cfg["scoring"]["weight_rewatch"]),
            "weight_recency": _parse_float_field(form, "weight_recency", current_cfg["scoring"]["weight_recency"]),
            "default_tv_runtime": _parse_int_field(form, "default_tv_runtime", current_cfg["scoring"]["default_tv_runtime"]),
            "default_movie_runtime": _parse_int_field(form, "default_movie_runtime", current_cfg["scoring"]["default_movie_runtime"]),
            "rewatch_saturation": _parse_int_field(form, "rewatch_saturation", current_cfg["scoring"]["rewatch_saturation"]),
        }
        recommendation_values = {
            "default_top_n": _parse_int_field(form, "default_top_n", current_cfg["default_top_n"]),
            "min_vote_count": _parse_int_field(form, "min_vote_count", current_cfg["min_vote_count"]),
            "min_rating": _parse_float_field(form, "min_rating", current_cfg["min_rating"]),
            "min_year": _parse_int_field(form, "min_year", current_cfg["min_year"]),
            "recency_half_life_days": _parse_int_field(form, "recency_half_life_days", current_cfg["recency_half_life_days"]),
        }
        manual_values = {
            "timestamp": _parse_manual_timestamp(form, current_cfg),
            "tv_duration_minutes": _parse_int_field(form, "manual_tv_duration", current_cfg["manual"]["tv_duration_minutes"]),
            "movie_duration_minutes": _parse_int_field(form, "manual_movie_duration", current_cfg["manual"]["movie_duration_minutes"]),
        }
    except ValueError as exc:
        return _render_settings_page(cfg=current_cfg, saved=False, error=str(exc))

    w_comp = scoring_values["weight_completion"]
    w_rew = scoring_values["weight_rewatch"]
    w_rec = scoring_values["weight_recency"]
    if abs((w_comp + w_rew + w_rec) - 1.0) > 0.01:
        return _render_settings_page(
            cfg=current_cfg, saved=False,
            error=f"Scoring weights must sum to 1.0 (got {w_comp + w_rew + w_rec:.2f})",
        )

    cfg["provider"] = form.get("provider", "anthropic")
    cfg.setdefault("models", {})
    for p in ["anthropic", "gemini", "openai"]:
        existing = cfg["models"].get(p, {})
        provider_cfg = {
            "fast": (form.get(f"{p}_fast") or existing.get("fast", "")).strip(),
            "reason": (form.get(f"{p}_reason") or existing.get("reason", "")).strip(),
        }
        submitted_api_key_env = form.get(f"{p}_api_key_env")
        api_key_env = (
            str(existing.get("api_key_env") or "").strip()
            if submitted_api_key_env is None
            else submitted_api_key_env.strip()
        )
        if api_key_env and api_key_env != _DEFAULT_LLM_API_KEY_ENVS.get(p, ""):
            provider_cfg["api_key_env"] = api_key_env
        cfg["models"][p] = provider_cfg
    base_url = (form.get("openai_base_url") or "").strip()
    cfg["models"]["openai"]["base_url"] = base_url if base_url else None

    llm = cfg.setdefault("llm", {})
    llm.update(llm_values)
    scoring = cfg.setdefault("scoring", {})
    scoring.update(scoring_values)
    cfg.update(recommendation_values)
    cfg["watch_region"] = form.get("watch_region", "US").strip().upper()
    platforms_str = form.get("streaming_platforms", "").strip()
    cfg["streaming_platforms"] = [p.strip() for p in platforms_str.split(",") if p.strip()] if platforms_str else []
    manual = cfg.setdefault("manual", {})
    manual.update(manual_values)
    cfg["log_level"] = form.get("log_level", "WARNING")

    updated_cfg = _resolve_settings_config(cfg)
    refresh_profile, refresh_data = _settings_refresh_flags(current_cfg, updated_cfg)

    _save_config_yaml(cfg)
    reload_applied = _reload_app_config()

    rebuild_command = None
    if refresh_data:
        rebuild_command = "./recommend setup --refresh-data"
    elif refresh_profile:
        rebuild_command = "./recommend setup --refresh-profile"

    return redirect(url_for(
        "settings_page",
        saved="1",
        rebuild_command=rebuild_command or "",
        reload_deferred="1" if not reload_applied else "0",
    ))


# ── Startup validation ─────────────────────────────────────────────────────────

def validate_config() -> None:
    """Check required config. Called by run() and the systemd preflight check."""
    if not config.TMDB_API_KEY:
        raise RuntimeError("TMDB_API_KEY must be set.")
    try:
        create_client()
    except (RuntimeError, ValueError) as exc:
        raise RuntimeError(str(exc)) from exc


# ── Application entry point ───────────────────────────────────────────────────

def run() -> None:
    from recommender.log import setup_logging
    setup_logging()
    try:
        validate_config()
    except RuntimeError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)
    host = os.environ.get("STREAMLINE_HOST", "127.0.0.1")
    port = int(os.environ.get("STREAMLINE_PORT", "5051"))
    print(f"Starting Streamline web UI at http://{host}:{port}")
    app.run(debug=False, host=host, port=port)


if __name__ == "__main__":
    run()
