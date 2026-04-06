import argparse
import json
import logging
import sys
from pathlib import Path

from rich.console import Console

log = logging.getLogger("recommender.setup")

import config
from recommender.ingestion.netflix import parse as parse_netflix
from recommender.ingestion.prime import parse as parse_prime
from recommender.ingestion.apple_tv import parse as parse_apple_tv
from recommender.ingestion.manual import parse as parse_manual
from recommender.signals import compute_scores
from recommender.tmdb_client import TmdbClient
from recommender.enricher import enrich_batch
from recommender.taste_profile_builder import build as build_taste_profile
from recommender.llm import create_client
from recommender import watch_index as wi
from recommender import feedback as fb
from recommender import overrides as ov

console = Console(stderr=True)


_PLATFORM_PARSERS = [
    ("netflix", parse_netflix),
    ("prime", parse_prime),
    ("apple_tv", parse_apple_tv),
]


def _load_all_events(strict: bool = False) -> tuple[list, bool]:
    """Load watch events from all configured platforms.

    Args:
        strict: If True, any configured provider that fails validation causes
                overall failure. Empty-but-valid exports remain acceptable.

    Returns:
        (events, ok) — ok is False if strict=True and any provider failed.
    """
    events = []
    ok = True

    for platform, parser in _PLATFORM_PARSERS:
        path = config.PLATFORM_PATHS.get(platform)
        if not path:
            if strict:
                console.print(f"  {platform}: [dim]disabled[/dim]")
            continue
        try:
            platform_events = parser(path)
        except (FileNotFoundError, ValueError) as exc:
            console.print(f"  {platform}: [red]FAIL[/red] {exc}")
            ok = False
            continue
        if not platform_events:
            console.print(f"  {platform}: [green]ok[/green] 0 events (no qualifying watch activity)")
            continue
        dates = [e.timestamp for e in platform_events]
        console.print(f"  {platform}: [green]ok[/green] {len(platform_events)} events "
                      f"({min(dates):%Y-%m-%d} to {max(dates):%Y-%m-%d})")
        events.extend(platform_events)

    # Manual ingestion (not subject to zip-only contract)
    if config.MANUAL_TV_PATH and config.MANUAL_MOVIES_PATH:
        try:
            manual_events = parse_manual(config.MANUAL_TV_PATH, config.MANUAL_MOVIES_PATH)
            events.extend(manual_events)
            console.print(f"  manual: [green]ok[/green] {len(manual_events)} events")
        except FileNotFoundError:
            console.print("  manual: [yellow]skipped[/yellow] (files not found)")

    return events, ok


def run_ingest_only() -> None:
    """Strict preflight validation of configured provider zips."""
    from collections import defaultdict

    console.print("Validating watch history sources...")
    events, ok = _load_all_events(strict=True)

    configured = sum(1 for p in _PLATFORM_PARSERS if config.PLATFORM_PATHS.get(p[0]))
    if configured == 0:
        console.print("\n[yellow]No providers configured. Set platform_paths in config.yaml.[/yellow]")
        sys.exit(1)

    console.print(f"\n  Total: {len(events)} events")

    if events:
        by_platform = defaultdict(list)
        for e in events:
            by_platform[e.platform].append(e)
        for platform, pevents in sorted(by_platform.items()):
            tv_titles = {e.series_name for e in pevents if e.content_type == "tv"}
            movie_titles = {e.series_name for e in pevents if e.content_type == "movie"}
            console.print(f"  {platform}: {len(tv_titles)} TV shows, {len(movie_titles)} movies")

    if not ok:
        console.print("\n[red]Validation failed.[/red]")
        sys.exit(1)

    console.print("\n[green]All configured providers validated.[/green]")


def run_setup(refresh_profile: bool = False, refresh_data: bool = False, provider: str | None = None) -> None:
    if not config.TMDB_API_KEY:
        console.print("[red]Error: TMDB_API_KEY not set. Export it and re-run.[/red]")
        sys.exit(1)
    try:
        llm = create_client(provider)
    except RuntimeError as exc:
        console.print(f"[red]Error: {exc}[/red]")
        sys.exit(1)
    console.print(f"  LLM provider: {llm.provider}")

    console.print("Loading watch history...")
    events, ok = _load_all_events(strict=True)
    if not ok:
        console.print("[red]Aborting setup: one or more configured providers failed.[/red]")
        sys.exit(1)
    if not events:
        console.print("[red]No watch events found. Add manual titles or provider exports with qualifying watch activity.[/red]")
        sys.exit(1)
    console.print(f"  Total: {len(events)} events")


    enrichments_index_path = Path(config.ENRICHMENT_CACHE_DIR) / "index.json"
    metadata: dict = {}

    # Auto-detect if overrides file has changed since last index build
    index_path = Path(config.WATCH_INDEX_PATH)
    overrides_path = Path(config.OVERRIDES_PATH)
    overrides_newer = (
        overrides_path.exists()
        and index_path.exists()
        and overrides_path.stat().st_mtime > index_path.stat().st_mtime
    )
    if overrides_newer and not refresh_data:
        console.print("\n[yellow]Overrides file changed since last build — triggering data + profile refresh.[/yellow]")
        refresh_data = True
        refresh_profile = True

    if not refresh_data and index_path.exists():
        console.print("\nWatch index exists, skipping data fetch (use --refresh-data to rebuild).")
        enrichments = json.loads(enrichments_index_path.read_text()) if enrichments_index_path.exists() else {}
    else:
        console.print("\nFetching TMDB metadata...")
        tmdb = TmdbClient(api_key=config.TMDB_API_KEY, cache_dir=config.CACHE_DIR)

        # Load overrides
        title_overrides = ov.load(config.OVERRIDES_PATH)
        if title_overrides:
            console.print(f"  Loaded {len(title_overrides)} title overrides")

        title_type: dict[str, str] = {}
        for e in events:
            key = e.series_name if e.content_type == 'tv' else e.title
            title_type[key] = e.content_type

        # Apply overrides: collect skips and content_type corrections
        skip_titles: set[str] = set()
        ct_overrides: dict[str, str] = {}
        for title, override in title_overrides.items():
            if override.get("skip"):
                skip_titles.add(title)
            if override.get("content_type"):
                ct_overrides[title] = override["content_type"]

        # Filter skipped titles from events so they don't enter the watch index
        if skip_titles:
            events = [e for e in events
                      if (e.series_name if e.content_type == 'tv' else e.title) not in skip_titles]

        # Apply content_type overrides to events so wi.build() persists the corrected type
        for e in events:
            key = e.series_name if e.content_type == 'tv' else e.title
            if key in ct_overrides:
                e.content_type = ct_overrides[key]

        # Recompute title_type after overrides — keys change when content_type flips
        if ct_overrides:
            title_type = {}
            for e in events:
                key = e.series_name if e.content_type == 'tv' else e.title
                title_type[key] = e.content_type

        metadata = {}
        skipped = len(skip_titles)
        with console.status("[bold magenta]Fetching TMDB metadata...[/bold magenta]", spinner="dots"):
            for i, (title, ct) in enumerate(title_type.items()):
                if title in skip_titles:
                    continue
                # Check overrides
                override = title_overrides.get(title)
                if override:
                    if override.get("content_type"):
                        ct = override["content_type"]
                    search_title = override.get("title", title)
                    if override.get("tmdb_id"):
                        cached = tmdb._load_cache(ct, override["tmdb_id"])
                        if cached:
                            metadata[title] = tmdb._parse_metadata(cached, ct)
                        else:
                            try:
                                data = tmdb._fetch_details(override["tmdb_id"], ct)
                                tmdb._save_cache(ct, override["tmdb_id"], data)
                                metadata[title] = tmdb._parse_metadata(data, ct)
                            except Exception as exc:
                                log.warning("Override TMDB fetch failed for %s (ID %d): %s",
                                            title, override["tmdb_id"], exc)
                    else:
                        meta = tmdb.get_metadata(search_title, ct)
                        if meta:
                            metadata[title] = meta
                else:
                    meta = tmdb.get_metadata(title, ct)
                    if meta:
                        metadata[title] = meta
                if (i + 1) % 50 == 0:
                    console.print(f"  {i+1}/{len(title_type)} titles processed...")
        console.print(f"  {len(metadata)} titles with TMDB metadata")
        if skipped:
            console.print(f"  {skipped} titles skipped via overrides")

        console.print("\nBuilding watch index...")
        index = wi.build(events, metadata)
        wi.save(index, config.WATCH_INDEX_PATH)
        console.print(f"  {len(index.entries)} unique titles indexed → {config.WATCH_INDEX_PATH}")

        # Report unmatched titles
        unmatched = [e for e in index.entries if not e.get("tmdb_id")]
        ov.report_unmatched(unmatched, config.OVERRIDES_PATH)

        # Clean up stale cache files for removed/deduped entries
        removed = wi.cleanup_stale_cache(index, config.ENRICHMENT_CACHE_DIR, config.PROVIDERS_CACHE_DIR)
        total_removed = sum(removed.values())
        if total_removed:
            console.print(f"  Cleaned {total_removed} stale cache files "
                          f"({removed['enrichments']} enrichments, "
                          f"{removed['enrichment_index']} index entries, "
                          f"{removed['providers']} provider files)")

        with console.status(f"[bold magenta]Enriching {len(metadata)} titles with Claude Haiku...[/bold magenta]", spinner="dots"):
            enrichments = enrich_batch(metadata, config.ENRICHMENT_CACHE_DIR, llm)
        enrichments_index_path.write_text(json.dumps(enrichments))
        console.print(f"  {len(enrichments)} descriptions cached → {config.ENRICHMENT_CACHE_DIR}")

    profile_path = Path(config.TASTE_PROFILE_PATH)
    if refresh_profile or not profile_path.exists():
        feedback = fb.load(config.FEEDBACK_PATH)
        liked_count = sum(1 for r in feedback["ratings"] if r.get("rating") == "liked")
        disliked_count = sum(1 for r in feedback["ratings"] if r.get("rating") == "disliked")
        if liked_count or disliked_count:
            console.print(f"  Applying feedback: {liked_count} liked, {disliked_count} disliked titles")

        with console.status("[bold magenta]Building taste profile with Claude Sonnet...[/bold magenta]", spinner="dots"):
            scores = compute_scores(events, metadata, config.RECENCY_HALF_LIFE_DAYS)
            scores = fb.apply_score_multipliers(scores, feedback)
            negative_prefs = fb.get_disliked_titles(feedback)
            try:
                profile = build_taste_profile(events, scores, enrichments, llm,
                                              negative_prefs=negative_prefs or None)
            except RuntimeError as exc:
                console.print(f"\n[red]{exc}[/red]")
                console.print("[yellow]Previous profile kept unchanged.[/yellow]")
                sys.exit(1)
        profile_path.parent.mkdir(parents=True, exist_ok=True)
        # Auto-backup previous profile before overwriting
        if profile_path.exists():
            from datetime import datetime
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            backup = profile_path.with_name(f"taste_profile_{ts}.txt")
            profile_path.rename(backup)
            console.print(f"  Previous profile backed up → {backup.name}")
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
    parser.add_argument("--ingest-only", action="store_true",
                        help="Load and report on ingested data without TMDB or LLM calls")
    parser.add_argument("--debug", action="store_true",
                        help="Enable debug logging")
    parser.add_argument("--provider", choices=["anthropic", "gemini", "openai"],
                        help="LLM provider (default: from config/env)")
    args = parser.parse_args()
    from recommender.log import setup_logging
    setup_logging(level_override="DEBUG" if args.debug else None)
    if args.ingest_only:
        run_ingest_only()
    else:
        run_setup(refresh_profile=args.refresh_profile, refresh_data=args.refresh_data,
                  provider=args.provider)
