import logging
import sys
from pathlib import Path

import anthropic

import config
from recommender.ingestion.netflix import parse as parse_netflix
from recommender.ingestion.prime import parse as parse_prime
from recommender.ingestion.manual import parse as parse_manual
from recommender.models import Recommendation
from recommender.tmdb_client import TmdbClient
from recommender.query_engine import RecommendContext, ask
from recommender import watch_index as wi

log = logging.getLogger("recommender")


def load_context() -> RecommendContext:
    events = []
    for platform, parser in [("netflix", parse_netflix), ("prime", parse_prime)]:
        path = config.PLATFORM_PATHS.get(platform)
        if path:
            events.extend(parser(path))
    if config.MANUAL_TV_PATH and config.MANUAL_MOVIES_PATH:
        events.extend(parse_manual(config.MANUAL_TV_PATH, config.MANUAL_MOVIES_PATH))

    index_path = Path(config.WATCH_INDEX_PATH)
    if not index_path.exists():
        print("Watch index not found. Run: python -m recommender.setup", file=sys.stderr)
        sys.exit(1)

    profile_path = Path(config.TASTE_PROFILE_PATH)
    if not profile_path.exists():
        print("Taste profile not found. Run: python -m recommender.setup", file=sys.stderr)
        sys.exit(1)

    return RecommendContext(
        taste_profile=profile_path.read_text(),
        watch_index=wi.load(config.WATCH_INDEX_PATH),
        events=events,
        tmdb_client=TmdbClient(api_key=config.TMDB_API_KEY, cache_dir=config.CACHE_DIR),
        anthropic_client=anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY),
        cache_dir=config.ENRICHMENT_CACHE_DIR,
    )


def print_recommendations(results: list[Recommendation], query: str) -> None:
    if not results:
        print("No recommendations found.")
        return
    print(f'\nResults for: "{query}"\n')
    for i, rec in enumerate(results, 1):
        print(f"{i}. {rec.title}  ★ {rec.vote_average:.1f}  [{', '.join(rec.genres[:3])}]")
        print(f"   {rec.explanation}")
        print()


def main() -> None:
    import argparse
    parser = argparse.ArgumentParser(description="Streaming Recommender")
    parser.add_argument("query", nargs="*", help="Recommendation query")
    parser.add_argument("--debug", action="store_true", help="Enable debug logging")
    parser.add_argument("-n", type=int, default=None, help="Number of results (overrides default)")
    args = parser.parse_args()

    level = logging.DEBUG if args.debug else logging.WARNING
    logging.basicConfig(
        level=logging.WARNING,
        format="%(name)s %(levelname)s: %(message)s",
        stream=sys.stderr,
    )
    logging.getLogger("recommender").setLevel(level)

    if not config.ANTHROPIC_API_KEY:
        print("Error: ANTHROPIC_API_KEY not set.", file=sys.stderr)
        sys.exit(1)
    if not config.TMDB_API_KEY:
        print("Error: TMDB_API_KEY not set.", file=sys.stderr)
        sys.exit(1)

    ctx = load_context()
    log.debug("Context loaded: %d events, %d watched titles",
              len(ctx.events), len(ctx.watch_index.entries))

    if args.query:
        query = " ".join(args.query)
        results = ask(query, ctx, top_n_override=args.n)
        print_recommendations(results, query)
        return

    print("Streaming Recommender — ask me anything about what to watch.")
    print('Type "exit" to quit.\n')
    while True:
        try:
            query = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not query or query.lower() == "exit":
            break
        results = ask(query, ctx, top_n_override=args.n)
        print_recommendations(results, query)


if __name__ == "__main__":
    main()
