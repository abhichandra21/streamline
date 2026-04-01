import argparse
import json
import logging
import sys
from pathlib import Path

import anthropic
from rich.console import Console

log = logging.getLogger("recommender.setup")

import config
from recommender.ingestion.netflix import parse as parse_netflix
from recommender.ingestion.prime import parse as parse_prime
from recommender.ingestion.manual import parse as parse_manual
from recommender.signals import compute_scores
from recommender.tmdb_client import TmdbClient
from recommender.enricher import enrich_batch
from recommender.taste_profile_builder import build as build_taste_profile
from recommender import watch_index as wi

console = Console(stderr=True)


def run_setup(refresh_profile: bool = False, refresh_data: bool = False) -> None:
    if not config.ANTHROPIC_API_KEY:
        console.print("[red]Error: ANTHROPIC_API_KEY not set. Export it and re-run.[/red]")
        sys.exit(1)
    if not config.TMDB_API_KEY:
        console.print("[red]Error: TMDB_API_KEY not set. Export it and re-run.[/red]")
        sys.exit(1)

    console.print("Loading watch history...")
    events = []
    for platform, parser in [("netflix", parse_netflix), ("prime", parse_prime)]:
        path = config.PLATFORM_PATHS.get(platform)
        if path:
            platform_events = parser(path)
            events.extend(platform_events)
            console.print(f"  {platform}: {len(platform_events)} events")
    if config.MANUAL_TV_PATH and config.MANUAL_MOVIES_PATH:
        manual_events = parse_manual(config.MANUAL_TV_PATH, config.MANUAL_MOVIES_PATH)
        events.extend(manual_events)
        console.print(f"  manual: {len(manual_events)} events")
    console.print(f"  Total: {len(events)} events")

    claude = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)

    enrichments_index_path = Path(config.ENRICHMENT_CACHE_DIR) / "index.json"
    metadata: dict = {}

    index_path = Path(config.WATCH_INDEX_PATH)
    if not refresh_data and index_path.exists():
        console.print("\nWatch index exists, skipping data fetch (use --refresh-data to rebuild).")
        enrichments = json.loads(enrichments_index_path.read_text()) if enrichments_index_path.exists() else {}
    else:
        console.print("\nFetching TMDB metadata...")
        tmdb = TmdbClient(api_key=config.TMDB_API_KEY, cache_dir=config.CACHE_DIR)

        title_type: dict[str, str] = {}
        for e in events:
            key = e.series_name if e.content_type == 'tv' else e.title
            title_type[key] = e.content_type

        metadata = {}
        with console.status("[bold magenta]Fetching TMDB metadata...[/bold magenta]", spinner="dots"):
            for i, (title, ct) in enumerate(title_type.items()):
                meta = tmdb.get_metadata(title, ct)
                if meta:
                    metadata[title] = meta
                if (i + 1) % 50 == 0:
                    console.print(f"  {i+1}/{len(title_type)} titles processed...")
        console.print(f"  {len(metadata)} titles with TMDB metadata")

        console.print("\nBuilding watch index...")
        index = wi.build(events, metadata)
        wi.save(index, config.WATCH_INDEX_PATH)
        console.print(f"  {len(index.entries)} unique titles indexed → {config.WATCH_INDEX_PATH}")

        with console.status(f"[bold magenta]Enriching {len(metadata)} titles with Claude Haiku...[/bold magenta]", spinner="dots"):
            enrichments = enrich_batch(metadata, config.ENRICHMENT_CACHE_DIR, claude)
        enrichments_index_path.write_text(json.dumps(enrichments))
        console.print(f"  {len(enrichments)} descriptions cached → {config.ENRICHMENT_CACHE_DIR}")

    profile_path = Path(config.TASTE_PROFILE_PATH)
    if refresh_profile or not profile_path.exists():
        with console.status("[bold magenta]Building taste profile with Claude Sonnet...[/bold magenta]", spinner="dots"):
            scores = compute_scores(events, metadata, config.RECENCY_HALF_LIFE_DAYS)
            profile = build_taste_profile(events, scores, enrichments, claude)
        profile_path.parent.mkdir(parents=True, exist_ok=True)
        profile_path.write_text(profile)
        console.print(f"  Taste profile saved → {config.TASTE_PROFILE_PATH}")
    else:
        console.print("\nTaste profile exists, skipping (use --refresh-profile to rebuild).")

    console.print("\n[green]Setup complete![/green]")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run offline setup for the recommender")
    parser.add_argument("--refresh-profile", action="store_true",
                        help="Rebuild taste profile even if it exists")
    parser.add_argument("--refresh-data", action="store_true",
                        help="Re-fetch TMDB metadata, watch index, and enrichments")
    parser.add_argument("--debug", action="store_true",
                        help="Enable debug logging")
    args = parser.parse_args()
    level = logging.DEBUG if args.debug else logging.WARNING
    logging.basicConfig(
        level=level,
        format="%(name)s %(levelname)s: %(message)s",
        stream=sys.stderr,
    )
    run_setup(refresh_profile=args.refresh_profile, refresh_data=args.refresh_data)
