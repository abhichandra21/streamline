import json
import logging
import re
import sys
from dataclasses import dataclass

import anthropic

from .enricher import enrich, enrich_batch
from .ingestion.base import WatchEvent
from .models import Recommendation
from .tmdb_client import TmdbClient, TmdbMetadata
from .watch_index import WatchIndex

log = logging.getLogger("recommender.query")


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
    top_n: int                  # 1 for single recommendation, >1 for "a few" queries


@dataclass
class RecommendContext:
    taste_profile: str
    watch_index: "WatchIndex"
    events: list[WatchEvent]
    tmdb_client: TmdbClient
    anthropic_client: anthropic.Anthropic
    cache_dir: str


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
        "content_type": "both", "top_n": 1,
    }
    for key, default in defaults.items():
        if key not in filtered:
            filtered[key] = default
    if isinstance(filtered.get("top_n"), str):
        filtered["top_n"] = int(filtered["top_n"])
    return QueryIntent(**filtered)


def parse_intent(query: str, client: anthropic.Anthropic) -> QueryIntent:
    """Parse a natural language query into structured intent using Claude Sonnet."""
    log.debug("Parsing intent for: %r", query)
    message = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=400,
        timeout=30.0,
        messages=[{
            "role": "user",
            "content": (
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
                '- top_n: integer — 1 for single recommendation (default), 3-5 if query implies '
                '"a few" or "some options" or "what should I watch"\n\n'
                f'Query: "{query}"'
            ),
        }],
    )
    log.debug("Raw intent response: %s", message.content[0].text[:500])
    try:
        data = _parse_json_response(message.content[0].text)
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
            content_type="both", top_n=1,
        )


def rank_candidates(
    query: str,
    taste_profile: str,
    candidates: list[TmdbMetadata],
    enrichments: dict[str, str],
    client: anthropic.Anthropic,
    top_n: int = 1,
) -> list[Recommendation]:
    """Rank candidates against taste profile using Claude Sonnet."""
    log.debug("Ranking %d candidates for query: %r (top_n=%d)", len(candidates), query, top_n)
    meta_by_title = {c.title: c for c in candidates}

    cands_str = '\n'.join(
        f"{i+1}. {c.title} (rating: {c.vote_average:.1f}): {enrichments.get(c.title, ' '.join(c.genres))}"
        for i, c in enumerate(candidates)
    )
    log.debug("Candidate list sent to ranker:\n%s", cands_str)

    message = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1000,
        timeout=30.0,
        messages=[{
            "role": "user",
            "content": (
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
                f'Return the top {top_n} ranked candidates.'
            ),
        }],
    )

    log.debug("Raw ranking response: %s", message.content[0].text[:500])
    try:
        ranked = _parse_json_response(message.content[0].text)
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
    desc = enrich(meta, ctx.cache_dir, ctx.anthropic_client) if meta else target

    message = ctx.anthropic_client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=300,
        timeout=30.0,
        messages=[{
            "role": "user",
            "content": (
                f'A user has watched {total_hours:.1f} hours of "{target}".\n\n'
                f'About "{target}": {desc}\n\n'
                f'Their taste profile:\n{ctx.taste_profile}\n\n'
                'Should they continue watching? Give a direct yes/no with 1-2 sentences of reasoning.'
            ),
        }],
    )

    return [Recommendation(
        title=target,
        content_type=ct,
        score=0.5,
        vote_average=meta.vote_average if meta else 0.0,
        genres=meta.genres if meta else [],
        explanation=message.content[0].text.strip(),
    )]


def ask(query: str, ctx: RecommendContext) -> list[Recommendation]:
    """Answer a natural language recommendation query end-to-end."""
    intent = parse_intent(query, ctx.anthropic_client)

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
        batch = ctx.tmdb_client.search_by_filters(
            content_type=ct,
            genres=intent.genres,
            origin_countries=intent.origin_countries,
            languages=intent.languages,
            year_from=intent.year_from,
            year_to=intent.year_to,
            size=30,
        )
        log.debug("TMDB returned %d candidates for %s", len(batch), ct)
        candidates.extend(batch)

    pre_filter = len(candidates)
    candidates = [c for c in candidates if not ctx.watch_index.is_watched(c)]
    log.debug("Watch filter: %d -> %d candidates (%d excluded)",
              pre_filter, len(candidates), pre_filter - len(candidates))
    for c in candidates:
        seen_ids.add(c.tmdb_id)

    # Source 2: Claude suggestions (semantic, taste-aware — always runs)
    log.debug("Fetching Claude suggestions for semantic coverage (similar_to=%s)", intent.similar_to)
    suggestions = _generate_suggestions(query, ctx.taste_profile, ctx.anthropic_client,
                                         similar_to=intent.similar_to)
    log.debug("Claude suggested %d titles: %s", len(suggestions), suggestions[:10])
    suggestion_count = 0
    for title in suggestions:
        for ct in content_types:
            meta = ctx.tmdb_client.get_metadata(title, ct)
            if meta and meta.tmdb_id not in seen_ids and not ctx.watch_index.is_watched(meta):
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
    enrichments = enrich_batch(meta_dict, ctx.cache_dir, ctx.anthropic_client)

    return rank_candidates(query, ctx.taste_profile, candidates, enrichments, ctx.anthropic_client, intent.top_n)


def _generate_suggestions(
    query: str,
    taste_profile: str,
    client: anthropic.Anthropic,
    similar_to: list[str] | None = None,
) -> list[str]:
    """Ask Claude to suggest specific titles based on query and taste profile."""
    similar_ctx = ""
    if similar_to:
        similar_ctx = f'\nThe user specifically wants something like: {", ".join(similar_to)}.\n'

    message = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=300,
        timeout=30.0,
        messages=[{
            "role": "user",
            "content": (
                f'A user is looking for: "{query}"\n'
                f'{similar_ctx}\n'
                f'Their taste profile:\n{taste_profile}\n\n'
                'Suggest 20 specific titles that fit the query. '
                'Prioritize query relevance over general taste match. '
                'Return ONLY a JSON array of title strings. Be precise with names.'
            ),
        }],
    )
    try:
        result = _parse_json_response(message.content[0].text)
        return [t for t in result if isinstance(t, str)] if isinstance(result, list) else []
    except (json.JSONDecodeError, TypeError, ValueError):
        return []
