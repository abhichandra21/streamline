import argparse
import sys
from pathlib import Path

import anthropic

import config
from recommender.ingestion.netflix import parse as parse_netflix
from recommender.ingestion.prime import parse as parse_prime
from recommender.signals import compute_scores
from recommender.tmdb_client import TmdbClient
from recommender.enricher import enrich_batch
from recommender.taste_profile_builder import build as build_taste_profile
from recommender import watch_index as wi


def run_setup(refresh_profile: bool = False) -> None:
    if not config.ANTHROPIC_API_KEY:
        print("Error: ANTHROPIC_API_KEY not set. Export it and re-run.", file=sys.stderr)
        sys.exit(1)

    print("Loading watch history...")
    events = []
    for platform, parser in [("netflix", parse_netflix), ("prime", parse_prime)]:
        path = config.PLATFORM_PATHS.get(platform)
        if path:
            platform_events = parser(path)
            events.extend(platform_events)
            print(f"  {platform}: {len(platform_events)} events")
    print(f"  Total: {len(events)} events")

    print("\nFetching TMDB metadata...")
    tmdb = TmdbClient(api_key=config.TMDB_API_KEY, cache_dir=config.CACHE_DIR)
    scores = compute_scores(events, {}, config.RECENCY_HALF_LIFE_DAYS)

    title_type: dict[str, str] = {}
    for e in events:
        key = e.series_name if e.content_type == 'tv' else e.title
        title_type[key] = e.content_type

    metadata = {}
    for i, (title, ct) in enumerate(title_type.items()):
        meta = tmdb.get_metadata(title, ct)
        if meta:
            metadata[title] = meta
        if (i + 1) % 50 == 0:
            print(f"  {i+1}/{len(title_type)} titles processed...")
    print(f"  {len(metadata)} titles with TMDB metadata")

    print("\nBuilding watch index...")
    index = wi.build(events, metadata)
    wi.save(index, config.WATCH_INDEX_PATH)
    print(f"  {len(index.entries)} unique titles indexed → {config.WATCH_INDEX_PATH}")

    print(f"\nEnriching {len(metadata)} titles with Claude Haiku...")
    claude = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
    enrichments = enrich_batch(metadata, config.ENRICHMENT_CACHE_DIR, claude)
    print(f"  {len(enrichments)} descriptions cached → {config.ENRICHMENT_CACHE_DIR}")

    profile_path = Path(config.TASTE_PROFILE_PATH)
    if refresh_profile or not profile_path.exists():
        print("\nBuilding taste profile with Claude Sonnet...")
        profile = build_taste_profile(events, scores, enrichments, claude)
        profile_path.parent.mkdir(parents=True, exist_ok=True)
        profile_path.write_text(profile)
        print(f"  Taste profile saved → {config.TASTE_PROFILE_PATH}")
    else:
        print(f"\nTaste profile exists, skipping (use --refresh-profile to rebuild).")

    print("\nSetup complete!")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run offline setup for the recommender")
    parser.add_argument("--refresh-profile", action="store_true",
                        help="Rebuild taste profile even if it exists")
    args = parser.parse_args()
    run_setup(refresh_profile=args.refresh_profile)
