#!/usr/bin/env python3
"""Compare recommendation output between two LLM providers side-by-side.

Usage:
    .venv/bin/python tools/compare_providers.py

Runs a fixed set of queries against both 'anthropic' and 'local' providers,
then writes a human-readable side-by-side comparison to comparison_results.txt.
"""

import os
import sys
import time
import traceback
from pathlib import Path

# Resolve project root (one level up from tools/)
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# Load .env before importing any project code
_env_path = PROJECT_ROOT / ".env"
if _env_path.exists():
    with open(_env_path) as _f:
        for _line in _f:
            _line = _line.strip()
            if _line and not _line.startswith("#") and "=" in _line:
                _k, _, _v = _line.partition("=")
                os.environ.setdefault(_k.strip(), _v.strip())

import config
from recommender.llm import create_client
from recommender.tmdb_client import TmdbClient
from recommender.query_engine import RecommendContext, ask
from recommender.user_state import UserStateIndex
from recommender import watch_index as wi
from recommender import user_store
from recommender.event_store import load_events


QUERIES = [
    "British crime drama",
    "Korean thriller",
    "feel-good comedy",
    "sci-fi mystery",
    "documentary about nature",
]

PROVIDERS = ["anthropic", "local"]

OUTPUT_PATH = PROJECT_ROOT / "comparison_results.txt"


def _events_loader() -> list:
    events = load_events(config.EVENT_DB_PATH)
    if not events and not Path(config.EVENT_DB_PATH).exists():
        from recommender.setup import load_platform_events_from_exports
        return load_platform_events_from_exports(fail_on_error=False)
    return events


def _load_user_state():
    if Path(config.EVENT_DB_PATH).exists():
        user_store.ensure_user_store(config.EVENT_DB_PATH, config.FEEDBACK_PATH)
    return UserStateIndex.load(config.EVENT_DB_PATH)


def load_shared_resources():
    """Load resources shared across both providers (watch index, taste profile, tmdb)."""
    index_path = Path(config.WATCH_INDEX_PATH)
    profile_path = Path(config.TASTE_PROFILE_PATH)

    if not index_path.exists():
        print("ERROR: Watch index not found. Run: ./recommend setup", file=sys.stderr)
        sys.exit(1)
    if not profile_path.exists():
        print("ERROR: Taste profile not found. Run: ./recommend setup", file=sys.stderr)
        sys.exit(1)

    taste_profile = profile_path.read_text()
    watch_index = wi.load(config.WATCH_INDEX_PATH)
    tmdb_client = TmdbClient(api_key=config.TMDB_API_KEY, cache_dir=config.CACHE_DIR)
    user_state = _load_user_state()
    return taste_profile, watch_index, tmdb_client, user_state


def build_context(provider: str, taste_profile, watch_index, tmdb_client, user_state) -> RecommendContext:
    llm = create_client(provider)
    return RecommendContext(
        taste_profile=taste_profile,
        watch_index=watch_index,
        tmdb_client=tmdb_client,
        llm=llm,
        cache_dir=config.ENRICHMENT_CACHE_DIR,
        _events_loader=_events_loader,
        providers_cache_dir=config.PROVIDERS_CACHE_DIR,
        watch_region=config.WATCH_REGION,
        streaming_platforms=list(config.STREAMING_PLATFORMS),
        user_state=user_state,
    )


def run_query_for_provider(ctx: RecommendContext, query: str, provider: str) -> dict:
    """Run a single query and return a result dict."""
    ctx.llm.usage.reset()
    start = time.monotonic()
    try:
        results = ask(query, ctx)
        elapsed_ms = (time.monotonic() - start) * 1000
        return {
            "provider": provider,
            "query": query,
            "results": results,
            "total_latency_ms": elapsed_ms,
            "llm_total_latency_ms": ctx.llm.usage.total_latency_ms,
            "llm_avg_latency_ms": ctx.llm.usage.avg_latency_ms(),
            "llm_calls": ctx.llm.usage.calls,
            "error": None,
        }
    except Exception as exc:
        elapsed_ms = (time.monotonic() - start) * 1000
        return {
            "provider": provider,
            "query": query,
            "results": [],
            "total_latency_ms": elapsed_ms,
            "llm_total_latency_ms": 0.0,
            "llm_avg_latency_ms": 0.0,
            "llm_calls": 0,
            "error": f"{type(exc).__name__}: {exc}",
        }


def format_results_block(result: dict) -> list[str]:
    """Format a single provider result as lines."""
    lines = []
    if result["error"]:
        lines.append(f"  FAILED: {result['error']}")
    elif not result["results"]:
        lines.append("  (no recommendations returned)")
    else:
        for i, rec in enumerate(result["results"], 1):
            genres = ", ".join(rec.genres[:3]) if rec.genres else "unknown genre"
            lines.append(f"  {i}. {rec.title}  [score: {rec.score:.2f}]  [{genres}]")
            if rec.explanation:
                # Wrap explanation at ~80 chars, indent
                explanation = rec.explanation.replace("\n", " ").strip()
                if len(explanation) > 200:
                    explanation = explanation[:197] + "..."
                lines.append(f"     {explanation}")
    lines.append(f"  Latency: {result['total_latency_ms']:.0f}ms total | "
                 f"{result['llm_total_latency_ms']:.0f}ms LLM | "
                 f"{result['llm_avg_latency_ms']:.0f}ms avg/call | "
                 f"{result['llm_calls']} LLM calls")
    return lines


def write_report(all_results: list[dict]) -> None:
    """Write comparison_results.txt with side-by-side layout per query."""
    lines = []
    lines.append("=" * 80)
    lines.append("PROVIDER COMPARISON REPORT")
    lines.append(f"Queries tested: {len(QUERIES)}")
    lines.append(f"Providers: {', '.join(PROVIDERS)}")
    lines.append("=" * 80)
    lines.append("")

    # Group by query
    by_query: dict[str, dict[str, dict]] = {}
    for r in all_results:
        by_query.setdefault(r["query"], {})[r["provider"]] = r

    for query in QUERIES:
        lines.append(f"QUERY: \"{query}\"")
        lines.append("-" * 80)
        provider_results = by_query.get(query, {})
        for provider in PROVIDERS:
            lines.append(f"  [{provider.upper()}]")
            result = provider_results.get(provider)
            if result is None:
                lines.append("  (not run)")
            else:
                lines.extend(format_results_block(result))
            lines.append("")
        lines.append("=" * 80)
        lines.append("")

    OUTPUT_PATH.write_text("\n".join(lines))


def main():
    if not config.TMDB_API_KEY:
        print("ERROR: TMDB_API_KEY not set.", file=sys.stderr)
        sys.exit(1)

    print("Loading shared resources (watch index, taste profile, TMDB)...", file=sys.stderr)
    taste_profile, watch_index, tmdb_client, user_state = load_shared_resources()
    print(f"Watch index: {len(watch_index.entries)} entries", file=sys.stderr)

    # Build contexts, catching per-provider init failures
    contexts: dict[str, RecommendContext | None] = {}
    init_errors: dict[str, str] = {}
    for provider in PROVIDERS:
        print(f"Initializing {provider} provider...", file=sys.stderr)
        try:
            ctx = build_context(provider, taste_profile, watch_index, tmdb_client, user_state)
            contexts[provider] = ctx
            print(f"  {provider}: OK", file=sys.stderr)
        except Exception as exc:
            contexts[provider] = None
            init_errors[provider] = f"{type(exc).__name__}: {exc}"
            print(f"  {provider}: FAILED — {exc}", file=sys.stderr)

    all_results = []

    # Pre-populate error results for failed inits
    for provider, error in init_errors.items():
        for query in QUERIES:
            all_results.append({
                "provider": provider,
                "query": query,
                "results": [],
                "total_latency_ms": 0.0,
                "llm_total_latency_ms": 0.0,
                "llm_avg_latency_ms": 0.0,
                "llm_calls": 0,
                "error": error,
            })

    total = len(QUERIES) * sum(1 for p in PROVIDERS if contexts.get(p) is not None)
    done = 0

    for query in QUERIES:
        for provider in PROVIDERS:
            ctx = contexts.get(provider)
            if ctx is None:
                continue  # already recorded init error above
            done += 1
            print(f"[{done}/{total}] {provider}: \"{query}\"...", file=sys.stderr)
            result = run_query_for_provider(ctx, query, provider)
            all_results.append(result)
            if result["error"]:
                print(f"  FAILED: {result['error']}", file=sys.stderr)
            else:
                titles = [r.title for r in result["results"]]
                print(f"  OK: {titles} ({result['total_latency_ms']:.0f}ms)", file=sys.stderr)

    write_report(all_results)
    print(f"\nResults written to: {OUTPUT_PATH}", file=sys.stderr)


if __name__ == "__main__":
    main()
