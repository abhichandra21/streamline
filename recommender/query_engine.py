import json
import logging
import re
import sys
from dataclasses import dataclass, field
from typing import Callable

import config

from .enricher import enrich, enrich_batch
from .ingestion.base import WatchEvent
from .llm import LLMClient
from .models import Recommendation
from .tmdb_client import TmdbClient, TmdbMetadata
from .watch_index import WatchIndex

log = logging.getLogger("recommender.query")


# Maps casual platform names users say to TMDB provider names.
PLATFORM_ALIASES: dict[str, str] = {
    "prime": "Amazon Prime Video",
    "amazon": "Amazon Prime Video",
    "amazon prime": "Amazon Prime Video",
    "netflix": "Netflix",
    "apple tv": "Apple TV Plus",
    "apple tv+": "Apple TV Plus",
    "apple": "Apple TV Plus",
    "hbo": "Max",
    "hbo max": "Max",
    "max": "Max",
    "disney": "Disney Plus",
    "disney+": "Disney Plus",
    "disney plus": "Disney Plus",
    "paramount": "Paramount Plus",
    "paramount+": "Paramount Plus",
    "peacock": "Peacock",
    "hulu": "Hulu",
    # UK broadcast networks -> their streaming counterparts
    "bbc": "BBC iPlayer",
    "bbc one": "BBC iPlayer",
    "bbc two": "BBC iPlayer",
    "bbc iplayer": "BBC iPlayer",
    "iplayer": "BBC iPlayer",
    "itv": "ITVX",
    "itv1": "ITVX",
    "itvx": "ITVX",
    "channel 4": "Channel 4",
    "channel 5": "My5",
    "all 4": "Channel 4",
    # UK streaming
    "britbox": "BritBox",
    "now tv": "Now TV",
    "now": "Now TV",
}


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
    top_n: int                  # default 3; 1 for "the single best", higher for "many options"
    platforms: list[str]        # e.g. ["Netflix"] from "spy thriller on Netflix"


@dataclass(init=False)
class RecommendContext:
    taste_profile: str
    watch_index: "WatchIndex"
    tmdb_client: TmdbClient
    llm: LLMClient
    cache_dir: str
    providers_cache_dir: str
    watch_region: str
    streaming_platforms: list[str]
    _events_loader: Callable[[], list[WatchEvent]] | None
    _events_resolved: list[WatchEvent] | None

    def __init__(self, taste_profile, watch_index, tmdb_client, llm, cache_dir,
                 events: list[WatchEvent] | None = None,
                 _events_loader: Callable[[], list[WatchEvent]] | None = None,
                 providers_cache_dir="", watch_region="US",
                 streaming_platforms: list[str] | None = None):
        self.taste_profile = taste_profile
        self.watch_index = watch_index
        self.tmdb_client = tmdb_client
        self.llm = llm
        self.cache_dir = cache_dir
        self.providers_cache_dir = providers_cache_dir
        self.watch_region = watch_region
        self.streaming_platforms = streaming_platforms or []
        self._events_loader = _events_loader
        self._events_resolved = events  # None = lazy, list = eager

    @property
    def events(self) -> list[WatchEvent]:
        if self._events_resolved is None:
            self._events_resolved = self._events_loader() if self._events_loader else []
        return self._events_resolved


@dataclass
class ConversationContext:
    """Tracks state across interactive REPL turns for refinement queries."""
    last_query: str
    last_intent: "QueryIntent"
    last_results: list[Recommendation]
    excluded_titles: set[str] = field(default_factory=set)  # grows across conversation


def _parse_json_response(text: str) -> dict | list:
    text = text.strip()
    if text.startswith('```'):
        text = re.sub(r'^```(?:json)?\n?', '', text)
        text = re.sub(r'\n?```$', '', text)
    return json.loads(text.strip())


def _safe_query_intent(data: dict) -> QueryIntent:
    """Construct QueryIntent with validation and defaults for missing/extra fields."""
    known_fields = {f.name for f in QueryIntent.__dataclass_fields__.values()}
    filtered = {k: v for k, v in data.items() if k in known_fields}
    defaults = {
        "genres": [], "origin_countries": [], "languages": [],
        "mood_descriptors": [], "similar_to": [],
        "max_runtime_minutes": None, "year_from": None, "year_to": None,
        "unwatched_only": True, "special_intent": None,
        "content_type": "both", "top_n": config.DEFAULT_TOP_N,
        "platforms": [],
    }
    for key, default in defaults.items():
        if key not in filtered:
            filtered[key] = default
    if isinstance(filtered.get("top_n"), str):
        filtered["top_n"] = int(filtered["top_n"])
    return QueryIntent(**filtered)


def parse_intent(
    query: str,
    client: LLMClient,
    conv_ctx: "ConversationContext | None" = None,
) -> QueryIntent:
    """Parse a natural language query into structured intent."""
    log.debug("Parsing intent for: %r", query)

    context_section = ""
    if conv_ctx:
        prev_titles = ", ".join(f'"{r.title}"' for r in conv_ctx.last_results[:3])
        context_section = (
            f'\nPrevious query: "{conv_ctx.last_query}"\n'
            f'Previous results: {prev_titles}\n'
            'The new query may be a refinement (e.g. "but something British", '
            '"more like #2", "something lighter"). Interpret it relative to the previous query.\n'
        )

    prompt = (
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
        f'- top_n: integer — default is {config.DEFAULT_TOP_N}; use 1 only if user asks for '
        '"the single best" or "one recommendation"; use 5-10 for "a lot" or "many options"\n'
        '- platforms: list of streaming platform names the user wants e.g. ["Netflix"] '
        'from "on Netflix" or "available on Prime" — use exact names like '
        '"Netflix", "Amazon Prime Video", "Apple TV Plus", "Max", "Disney Plus", '
        '"Paramount Plus", "Peacock", "Hulu"; empty list if no platform specified\n'
        f'{context_section}\n'
        f'Query: "{query}"'
    )
    response_text = client.generate(prompt, role="reason", max_tokens=config.TOKENS_INTENT,
                                     timeout=config.TIMEOUT_REASON)
    log.debug("Raw intent response: %s", response_text[:500])
    try:
        data = _parse_json_response(response_text)
        intent = _safe_query_intent(data)
        log.debug("Parsed intent: genres=%s countries=%s languages=%s content_type=%s "
                   "mood=%s similar_to=%s special=%s top_n=%d",
                   intent.genres, intent.origin_countries, intent.languages,
                   intent.content_type, intent.mood_descriptors, intent.similar_to,
                   intent.special_intent, intent.top_n)
        return intent
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        log.warning("Failed to parse intent: %s", exc)
        return QueryIntent(
            genres=[], origin_countries=[], languages=[],
            mood_descriptors=[], similar_to=[],
            max_runtime_minutes=None, year_from=None, year_to=None,
            unwatched_only=True, special_intent=None,
            content_type="both", top_n=config.DEFAULT_TOP_N,
            platforms=[],
        )


def rank_candidates(
    query: str,
    taste_profile: str,
    candidates: list[TmdbMetadata],
    enrichments: dict[str, str],
    client: LLMClient,
    top_n: int = 1,
) -> list[Recommendation]:
    """Rank candidates against taste profile."""
    log.debug("Ranking %d candidates for query: %r (top_n=%d)", len(candidates), query, top_n)
    meta_by_title = {c.title: c for c in candidates}

    cands_str = '\n'.join(
        f"{i+1}. {c.title} (rating: {c.vote_average:.1f}): {enrichments.get(c.title, ' '.join(c.genres))}"
        for i, c in enumerate(candidates)
    )
    log.debug("Candidate list sent to ranker:\n%s", cands_str)

    prompt = (
        f'Rank these candidates for a user. IMPORTANT: the query is the primary filter — '
        f'a candidate must match what the user asked for. The taste profile is secondary, '
        f'used to break ties between candidates that all fit the query.\n\n'
        f'QUERY: "{query}"\n\n'
        f'TASTE PROFILE:\n{taste_profile}\n\n'
        f'CANDIDATES:\n{cands_str}\n\n'
        f'Return ONLY valid JSON: a list of objects with fields:\n'
        f'- title: string (exact title from candidates)\n'
        f'- explanation: string (1-2 sentences why this fits the query and this user)\n'
        f'- score: float 0-1 (how well it matches the QUERY, boosted slightly by taste fit)\n\n'
        f'Return EXACTLY the top {top_n} ranked candidates, no more.'
    )
    # Scale output tokens based on result count
    rank_tokens = max(config.TOKENS_RANKING, top_n * 200 + 200)
    response_text = client.generate(prompt, role="reason", max_tokens=rank_tokens,
                                     timeout=config.TIMEOUT_REASON)

    log.debug("Raw ranking response: %s", response_text[:500])
    try:
        ranked = _parse_json_response(response_text)
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        log.warning("Failed to parse ranking response: %s", exc)
        return []
    results = []
    for item in ranked:
        title = item.get('title', '')
        if title not in meta_by_title:
            log.debug("Ranked title not in candidates, skipping: %r", title)
            continue
        meta = meta_by_title[title]
        results.append(Recommendation(
            title=title,
            content_type=meta.content_type,
            score=float(item.get('score', 0)),
            vote_average=meta.vote_average,
            genres=meta.genres,
            explanation=item.get('explanation', ''),
        ))
    log.debug("Ranking returned %d results", len(results))
    return results[:top_n]


def _handle_abandoned(query: str, intent: QueryIntent, ctx: RecommendContext) -> list[Recommendation]:
    """Handle 'I started X and stopped — worth finishing?' queries."""
    target = intent.similar_to[0] if intent.similar_to else query
    log.debug("Handling abandoned query for target: %r", target)

    matching = [
        e for e in ctx.events
        if (target.lower() in e.title.lower() or target.lower() in e.series_name.lower())
        and (intent.content_type == 'both' or e.content_type == intent.content_type)
    ]
    if not matching:
        log.debug("No matching events found for abandoned target")
        return []

    total_hours = sum(e.watched_duration.total_seconds() for e in matching) / 3600
    ct = matching[0].content_type
    lookup_title = matching[0].series_name if ct == 'tv' else matching[0].title
    meta = ctx.tmdb_client.get_metadata(lookup_title, ct)
    desc = enrich(meta, ctx.cache_dir, ctx.llm) if meta else target

    prompt = (
        f'A user has watched {total_hours:.1f} hours of "{target}".\n\n'
        f'About "{target}": {desc}\n\n'
        f'Their taste profile:\n{ctx.taste_profile}\n\n'
        'Should they continue watching? Give a direct yes/no with 1-2 sentences of reasoning.'
    )
    response_text = ctx.llm.generate(prompt, role="reason", max_tokens=config.TOKENS_ABANDONED,
                                      timeout=config.TIMEOUT_REASON)

    return [Recommendation(
        title=target,
        content_type=ct,
        score=0.5,
        vote_average=meta.vote_average if meta else 0.0,
        genres=meta.genres if meta else [],
        explanation=response_text.strip(),
    )]


def _extract_why_not_title(query: str) -> str | None:
    """Extract title from 'why not X?' / 'why didn't you recommend X?' queries."""
    q = query.strip().rstrip("?").lower()
    for prefix in ("why not ", "why didn't you recommend ", "why wasn't ", "why isn't "):
        if q.startswith(prefix):
            return query.strip().rstrip("?")[len(prefix):].strip()
    return None


def _handle_why_not(title: str, ctx: RecommendContext) -> list[Recommendation]:
    """Trace a title through the pipeline and explain why it wasn't recommended."""
    lines: list[str] = [f'Tracing "{title}" through the pipeline:\n']

    # Step 1: TMDB lookup
    meta_tv = ctx.tmdb_client.get_metadata(title, "tv")
    meta_movie = ctx.tmdb_client.get_metadata(title, "movie")
    meta = meta_tv or meta_movie

    if not meta:
        lines.append("  TMDB: [bold red]Not found[/bold red] — title not in TMDB database.")
        lines.append("  → Try searching with the exact title, or it may be too obscure for TMDB.")
        return [Recommendation(
            title=title, content_type="unknown", score=0.0, vote_average=0.0, genres=[],
            explanation="\n".join(lines),
        )]

    ct_label = f"{meta.content_type.upper()} — ID {meta.tmdb_id}"
    genres_str = ", ".join(meta.genres) or "none"
    lines.append(f"  TMDB: [green]Found[/green] — {ct_label}, genres: [{genres_str}], ★ {meta.vote_average:.1f}")

    # Step 2: Watch index
    if ctx.watch_index.is_watched(meta):
        lines.append("  Watch index: [yellow]WATCHED[/yellow] — already in your watch history.")
        lines.append("  → Excluded because you've already watched it.")
        return [Recommendation(
            title=meta.title, content_type=meta.content_type,
            score=0.0, vote_average=meta.vote_average, genres=meta.genres,
            explanation="\n".join(lines),
        )]
    lines.append("  Watch index: [green]Not watched[/green] — not in watch history.")

    # Step 3: Popularity / vote threshold
    if meta.vote_count < config.MIN_VOTE_COUNT:
        lines.append(
            f"  Popularity: [yellow]Below threshold[/yellow] — "
            f"only {meta.vote_count} votes (minimum {config.MIN_VOTE_COUNT})."
        )
        lines.append("  → Filtered out as too obscure. Lower MIN_VOTE_COUNT in config.py to include it.")
        return [Recommendation(
            title=meta.title, content_type=meta.content_type,
            score=0.0, vote_average=meta.vote_average, genres=meta.genres,
            explanation="\n".join(lines),
        )]
    lines.append(f"  Popularity: [green]OK[/green] — {meta.vote_count} votes.")

    # Step 3b: Rating filter
    if config.MIN_RATING > 0 and meta.vote_average < config.MIN_RATING:
        lines.append(
            f"  Rating: [yellow]Below minimum[/yellow] — "
            f"★ {meta.vote_average:.1f} (minimum {config.MIN_RATING})."
        )
        lines.append("  → Filtered by min_rating setting. Lower it in Settings to include this title.")
        return [Recommendation(
            title=meta.title, content_type=meta.content_type,
            score=0.0, vote_average=meta.vote_average, genres=meta.genres,
            explanation="\n".join(lines),
        )]
    if config.MIN_RATING > 0:
        lines.append(f"  Rating: [green]OK[/green] — ★ {meta.vote_average:.1f} (minimum {config.MIN_RATING}).")

    # Step 3c: Year filter
    if config.MIN_YEAR > 0 and meta.release_year and meta.release_year < config.MIN_YEAR:
        lines.append(
            f"  Year: [yellow]Too old[/yellow] — "
            f"released {meta.release_year} (minimum {config.MIN_YEAR})."
        )
        lines.append("  → Filtered by min_year setting. Lower it in Settings to include this title.")
        return [Recommendation(
            title=meta.title, content_type=meta.content_type,
            score=0.0, vote_average=meta.vote_average, genres=meta.genres,
            explanation="\n".join(lines),
        )]
    if config.MIN_YEAR > 0 and meta.release_year:
        lines.append(f"  Year: [green]OK[/green] — released {meta.release_year} (minimum {config.MIN_YEAR}).")

    # Step 4: Candidate pool (we can't replay the last query, so explain what gets it included)
    lines.append(
        "  Candidate pool: Title would need to appear via TMDB Discover filters "
        "(matching genres/country/language) OR be suggested by Claude's semantic search."
    )
    lines.append(
        "  → If it didn't appear, try a broader query or mention it by name "
        "(e.g. \"something like <Title>\")."
    )

    return [Recommendation(
        title=meta.title, content_type=meta.content_type,
        score=0.0, vote_average=meta.vote_average, genres=meta.genres,
        explanation="\n".join(lines),
    )]


_WHAT_ELSE_PATTERNS = re.compile(
    r'^(what else|more|show more|anything else|other options?)\??$', re.IGNORECASE
)
_MORE_LIKE_N = re.compile(r'^more like #?(\d+)', re.IGNORECASE)


def ask(
    query: str,
    ctx: RecommendContext,
    top_n_override: int | None = None,
    conv_ctx: "ConversationContext | None" = None,
) -> list[Recommendation]:
    """Answer a natural language recommendation query end-to-end."""
    why_not_title = _extract_why_not_title(query)
    if why_not_title:
        return _handle_why_not(why_not_title, ctx)

    # Conversational refinement: "what else?" → re-run last query, exclude seen titles.
    if conv_ctx and _WHAT_ELSE_PATTERNS.match(query.strip()):
        log.debug("'What else?' detected — re-running last intent with exclusions")
        intent = conv_ctx.last_intent
        intent.top_n = top_n_override or intent.top_n
        extra_excludes = conv_ctx.excluded_titles
    else:
        extra_excludes: set[str] = set()
        # Conversational refinement: "more like #N" — extract title from last results.
        if conv_ctx:
            m = _MORE_LIKE_N.match(query.strip())
            if m:
                idx = int(m.group(1)) - 1
                if 0 <= idx < len(conv_ctx.last_results):
                    picked = conv_ctx.last_results[idx].title
                    log.debug("'More like #%d' → using %r as similar_to", idx + 1, picked)
                    query = f"more like {picked}"

        intent = parse_intent(query, ctx.llm, conv_ctx=conv_ctx)
        if top_n_override is not None:
            intent.top_n = top_n_override

    if intent.special_intent == 'abandoned':
        return _handle_abandoned(query, intent, ctx)

    content_types = ['tv', 'movie'] if intent.content_type == 'both' else [intent.content_type]
    seen_ids: set[int] = set()

    # Source 1: TMDB Discover (structured metadata filter)
    candidates: list[TmdbMetadata] = []
    for ct in content_types:
        log.debug("TMDB discover: type=%s genres=%s countries=%s languages=%s years=%s-%s",
                   ct, intent.genres, intent.origin_countries, intent.languages,
                   intent.year_from, intent.year_to)
        # Use the more restrictive of intent year_from and config MIN_YEAR
        effective_year_from = intent.year_from
        if config.MIN_YEAR > 0:
            effective_year_from = max(config.MIN_YEAR, intent.year_from or 0) or config.MIN_YEAR
        batch = ctx.tmdb_client.search_by_filters(
            content_type=ct,
            genres=intent.genres,
            origin_countries=intent.origin_countries,
            languages=intent.languages,
            year_from=effective_year_from,
            year_to=intent.year_to,
            size=30,
        )
        log.debug("TMDB returned %d candidates for %s", len(batch), ct)
        candidates.extend(batch)

    pre_filter = len(candidates)
    candidates = [
        c for c in candidates
        if not ctx.watch_index.is_watched(c) and c.title not in extra_excludes
    ]
    log.debug("Watch filter: %d -> %d candidates (%d excluded)",
              pre_filter, len(candidates), pre_filter - len(candidates))

    # Apply min rating filter (min_year is already pushed into TMDB discover)
    pre_quality = len(candidates)
    if config.MIN_RATING > 0:
        candidates = [c for c in candidates if c.vote_average >= config.MIN_RATING]
    if len(candidates) < pre_quality:
        log.debug("Rating filter: %d -> %d candidates (min_rating=%.1f)",
                  pre_quality, len(candidates), config.MIN_RATING)

    for c in candidates:
        seen_ids.add(c.tmdb_id)

    # Source 2: Claude suggestions (semantic, taste-aware — always runs)
    log.debug("Fetching Claude suggestions for semantic coverage (similar_to=%s)", intent.similar_to)
    suggestions = _generate_suggestions(query, ctx.taste_profile, ctx.llm,
                                         similar_to=intent.similar_to)
    log.debug("Claude suggested %d titles: %s", len(suggestions), suggestions[:10])
    suggestion_count = 0
    for title in suggestions:
        for ct in content_types:
            meta = ctx.tmdb_client.get_metadata(title, ct)
            if meta and meta.tmdb_id not in seen_ids and not ctx.watch_index.is_watched(meta) and meta.title not in extra_excludes:
                if config.MIN_RATING > 0 and meta.vote_average < config.MIN_RATING:
                    continue
                if config.MIN_YEAR > 0 and meta.release_year and meta.release_year < config.MIN_YEAR:
                    continue
                candidates.append(meta)
                seen_ids.add(meta.tmdb_id)
                suggestion_count += 1
    log.debug("Claude suggestions added %d new candidates", suggestion_count)

    if log.isEnabledFor(logging.DEBUG) and candidates:
        log.debug("Final candidate pool (%d): %s",
                   len(candidates),
                   [f"{c.title} ({c.content_type}, ★{c.vote_average:.1f})" for c in candidates[:20]])

    if not candidates:
        log.debug("No candidates after all sources, returning empty")
        return []

    log.debug("Enriching %d candidates", len(candidates))
    meta_dict = {c.title: c for c in candidates}
    enrichments = enrich_batch(meta_dict, ctx.cache_dir, ctx.llm)

    # Annotate results with streaming provider data (and optionally filter by platform).
    if ctx.providers_cache_dir:
        meta_by_title = {c.title: c for c in candidates}
        requested_platforms = [
            PLATFORM_ALIASES.get(p.lower(), p) for p in (intent.platforms or [])
        ] or [PLATFORM_ALIASES.get(p.lower(), p) for p in config.STREAMING_PLATFORMS]

        # Rank with a larger pool when platform filtering is active, so we have
        # enough candidates after discarding titles not on the requested service.
        rank_size = max(intent.top_n * 3, 15) if requested_platforms else intent.top_n
        results = rank_candidates(query, ctx.taste_profile, candidates, enrichments, ctx.llm, rank_size)

        annotated = []
        unfiltered = []
        for rec in results:
            meta = meta_by_title.get(rec.title)
            if meta:
                providers = ctx.tmdb_client.get_watch_providers(
                    meta.tmdb_id, meta.content_type,
                    ctx.watch_region, ctx.providers_cache_dir,
                )
                rec.streaming_providers = providers
            unfiltered.append(rec)
            if requested_platforms:
                if not any(p in rec.streaming_providers for p in requested_platforms):
                    log.debug("Filtering out %r — not on requested platforms %s", rec.title, requested_platforms)
                    continue
            annotated.append(rec)
            if len(annotated) == intent.top_n:
                break

        # If platform filter removed everything, fall back to unfiltered results
        if not annotated and unfiltered and requested_platforms:
            log.debug("Platform filter removed all results — returning unfiltered top %d", intent.top_n)
            annotated = unfiltered[:intent.top_n]

        if conv_ctx is not None:
            conv_ctx.last_intent = intent
        return annotated

    results = rank_candidates(query, ctx.taste_profile, candidates, enrichments, ctx.llm, intent.top_n)
    if conv_ctx is not None:
        conv_ctx.last_intent = intent
    return results


def _generate_suggestions(
    query: str,
    taste_profile: str,
    client: LLMClient,
    similar_to: list[str] | None = None,
) -> list[str]:
    """Ask LLM to suggest specific titles based on query and taste profile."""
    similar_ctx = ""
    if similar_to:
        similar_ctx = f'\nThe user specifically wants something like: {", ".join(similar_to)}.\n'

    prompt = (
        f'A user is looking for: "{query}"\n'
        f'{similar_ctx}\n'
        f'Their taste profile:\n{taste_profile}\n\n'
        'Suggest 20 specific titles that fit the query. '
        'Prioritize query relevance over general taste match. '
        'Return ONLY a JSON array of title strings. Be precise with names.'
    )
    response_text = client.generate(prompt, role="reason", max_tokens=config.TOKENS_SUGGESTIONS,
                                     timeout=config.TIMEOUT_REASON)
    try:
        result = _parse_json_response(response_text)
        return [t for t in result if isinstance(t, str)] if isinstance(result, list) else []
    except (json.JSONDecodeError, TypeError, ValueError):
        return []
