import argparse
import hashlib
import json
import logging
import re
import sys
from datetime import datetime
from pathlib import Path

from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeElapsedColumn,
    TimeRemainingColumn,
)

log = logging.getLogger("recommender.setup")

import config
from recommender.ingestion.netflix import parse as parse_netflix
from recommender.ingestion.prime import parse as parse_prime
from recommender.ingestion.apple_tv import parse as parse_apple_tv
from recommender.ingestion.disney import parse as parse_disney
from recommender.ingestion.hbo import parse as parse_hbo
from recommender.ingestion.manual import parse as parse_manual
from recommender.signals import compute_scores
from recommender.tmdb_client import TmdbClient, TmdbMetadata, MatchHints
from recommender.enricher import (
    enrich_batch,
    enrichment_key,
    enrichment_key_from_parts,
    is_identity_enrichment_index,
)
from recommender.taste_profile_builder import build as build_taste_profile
from recommender.structured_profile import build_structured_profile, save_structured_profile
from recommender.llm import create_client
from recommender import watch_index as wi
from recommender import user_store
from recommender import overrides as ov
from recommender import event_store
from recommender.log import console


def _progress_bar(label: str, *, with_extra: str | None = None) -> Progress:
    """Standard Progress widget for long-running setup steps."""
    columns = [
        SpinnerColumn(),
        TextColumn(f"[bold magenta]{label}"),
        BarColumn(),
        MofNCompleteColumn(),
    ]
    if with_extra:
        columns.append(TextColumn(with_extra))
    columns.extend([
        TimeElapsedColumn(),
        TextColumn("eta"),
        TimeRemainingColumn(),
    ])
    return Progress(*columns, console=console, transient=False)


_PLATFORM_PARSERS = [
    ("netflix", parse_netflix),
    ("prime", parse_prime),
    ("apple_tv", parse_apple_tv),
    ("disney", parse_disney),
    ("hbo", parse_hbo),
]


def _remove_structured_profile(path: str) -> None:
    target = Path(path)
    try:
        target.unlink()
        console.print(f"[yellow]Previous structured taste profile removed → {target}[/yellow]")
    except FileNotFoundError:
        return
    except OSError as exc:
        log.warning("Unable to remove stale structured taste profile at %s: %s", target, exc)
        console.print(f"[yellow]Unable to remove stale structured taste profile: {exc}[/yellow]")


def _compute_file_sha256(path: str) -> str:
    """Compute SHA-256 of a file using chunked reads.

    If `path` is a directory, hash a composite digest over each contained
    file's relative path and content so that any change inside the bundle
    invalidates the snapshot. Used for export sources that ship as a folder
    of CSVs (e.g. the WBD/Max bundle) rather than a single zip.
    """
    from pathlib import Path as _Path
    p = _Path(path)
    if p.is_dir():
        composite = hashlib.sha256()
        for child in sorted(f for f in p.rglob("*") if f.is_file()):
            rel = child.relative_to(p).as_posix()
            composite.update(rel.encode("utf-8"))
            composite.update(b"\0")
            with open(child, "rb") as f:
                while chunk := f.read(8192):
                    composite.update(chunk)
            composite.update(b"\0")
        return composite.hexdigest()
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(8192):
            h.update(chunk)
    return h.hexdigest()


def _build_source_manifest(paths: list[str]) -> tuple[list[dict], str]:
    """Build manifest and snapshot hash for a set of source files.

    Returns (manifest, snapshot_sha256) where manifest is a list of
    {"path": str, "sha256": str} dicts sorted by path.
    """
    manifest = sorted(
        [{"path": p, "sha256": _compute_file_sha256(p)} for p in paths],
        key=lambda m: m["path"],
    )
    manifest_json = json.dumps(manifest, separators=(",", ":"))
    snapshot_sha = hashlib.sha256(manifest_json.encode()).hexdigest()
    return manifest, snapshot_sha


def _dedup_events(events: list) -> tuple[list, int]:
    """Deduplicate watch events in memory using identity key (excludes profile).

    First-seen event wins. Returns (deduped_events, duplicate_count).

    The identity key must match event_store._compute_source_hash fields.
    """
    seen: set[tuple] = set()
    deduped: list = []
    for e in events:
        key = (
            e.platform,
            e.content_type,
            e.series_name,
            e.title,
            e.timestamp.isoformat(timespec="seconds"),
            int(e.watched_duration.total_seconds()),
        )
        if key not in seen:
            seen.add(key)
            deduped.append(e)
    return deduped, len(events) - len(deduped)


def _title_keyed_enrichments(
    raw_enrichments: dict[str, str],
    watch_entries: list[dict],
    metadata: dict,
) -> dict[str, str]:
    """Return the title-keyed view expected by taste_profile_builder.build().

    metadata may be keyed by title string or (title, content_type) tuple.
    """
    if not is_identity_enrichment_index(raw_enrichments):
        return raw_enrichments

    title_keyed: dict[str, str] = {}

    # Works in refresh-profile-only mode, where metadata may be empty but watch_index exists.
    for entry in watch_entries:
        title = entry.get("title", "")
        if not title:
            continue
        key = enrichment_key_from_parts(
            entry.get("content_type"),
            entry.get("tmdb_id"),
            title,
        )
        if key in raw_enrichments:
            title_keyed[title] = raw_enrichments[key]

    # Works in refresh-data mode, where metadata has just been rebuilt.
    for meta_key, meta in metadata.items():
        display_title = meta_key[0] if isinstance(meta_key, tuple) else meta_key
        key = enrichment_key(meta)
        if key in raw_enrichments:
            title_keyed[display_title] = raw_enrichments[key]

    return title_keyed


def _normalize_audit_title(title: str) -> str:
    title = title.lower()
    title = re.sub(r'\s*\([^)]*\)', '', title)
    return title.strip()


def _cache_title(cache_path: Path) -> str:
    try:
        raw = json.loads(cache_path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        log.debug("Unable to inspect TMDB cache %s: %s", cache_path, exc)
        return ""
    return raw.get("name") or raw.get("title") or ""


def _titles_are_compatible(index_title: str, cache_title: str) -> bool:
    index_norm = _normalize_audit_title(index_title)
    cache_norm = _normalize_audit_title(cache_title)
    if not index_norm or not cache_norm:
        return False
    if index_norm == cache_norm:
        return True
    if len(cache_norm) < 4:
        return False
    return cache_norm in index_norm


def _build_hints_map(events: list) -> dict[tuple[str, str], MatchHints]:
    """Build per-title MatchHints from source event data."""
    hints_map: dict[tuple[str, str], MatchHints] = {}
    for e in events:
        key_title = e.series_name if e.content_type == 'tv' else e.title
        map_key = (key_title, e.content_type)
        if map_key in hints_map:
            continue

        release_year = getattr(e, 'release_year_hint', None)

        runtime_minutes = None
        runtime_is_exact = False

        if e.platform == 'apple_tv' and e.total_duration:
            runtime_minutes = int(e.total_duration.total_seconds() / 60)
            runtime_is_exact = True
        elif e.platform in ('netflix', 'prime') and e.content_type == 'movie':
            if e.watched_duration and e.watched_duration.total_seconds() >= 3600:
                runtime_minutes = int(e.watched_duration.total_seconds() / 60)
                runtime_is_exact = False
        # Do not use manual default durations as runtime hints (they are synthetic)

        if release_year or runtime_minutes:
            hints_map[map_key] = MatchHints(
                release_year=release_year,
                runtime_minutes=runtime_minutes,
                runtime_is_exact=runtime_is_exact,
            )

    return hints_map


def _audit_cache_mismatches(
    index,
    cache_dir: str,
    hints_map: dict | None = None,
    audit_output_path: str | None = None,
) -> None:
    """Report watch-index entries whose TMDB cache is suspicious.

    Checks: content-type mismatch, title mismatch, year mismatch,
    runtime mismatch, and weak matches (no poster, zero votes).

    audit_output_path overrides where the full-audit text file is written.
    Defaults to config.TMDB_AUDIT_PATH (the canonical cache location).
    Tests should pass a tmp path to avoid clobbering the real audit file.
    """
    if hints_map is None:
        hints_map = {}
    if audit_output_path is None:
        audit_output_path = config.TMDB_AUDIT_PATH

    mismatched = []
    title_mismatches = []
    year_mismatches = []
    runtime_mismatches = []
    weak_matches = []
    unmatched = []
    missing = []

    for e in index.entries:
        tmdb_id = e.get("tmdb_id")
        ct = e.get("content_type", "movie")
        if not tmdb_id:
            unmatched.append((e["title"], ct))
            continue
        cache_path = Path(cache_dir) / ct / f"{tmdb_id}.json"
        if cache_path.exists():
            try:
                raw = json.loads(cache_path.read_text())
            except (OSError, json.JSONDecodeError):
                continue

            cache_title = raw.get("name") or raw.get("title") or ""
            if cache_title and not _titles_are_compatible(e["title"], cache_title):
                title_mismatches.append((e["title"], ct, tmdb_id, cache_title))

            # Year mismatch check
            hints = hints_map.get((e["title"], ct))
            if hints and hints.release_year:
                date_str = raw.get("first_air_date") if ct == "tv" else raw.get("release_date")
                if date_str and len(date_str) >= 4:
                    cache_year = int(date_str[:4])
                    if abs(cache_year - hints.release_year) > 2:
                        year_mismatches.append((
                            e["title"], ct, tmdb_id, cache_title,
                            hints.release_year, cache_year,
                        ))

            # Runtime mismatch check
            if hints and hints.runtime_minutes:
                if ct == "tv":
                    runtimes = raw.get("episode_run_time", [])
                    cache_runtime = runtimes[0] if runtimes else None
                else:
                    cache_runtime = raw.get("runtime")
                if cache_runtime and cache_runtime > 0 and hints.runtime_minutes > 0:
                    ratio = min(cache_runtime, hints.runtime_minutes) / max(cache_runtime, hints.runtime_minutes)
                    if ratio < 0.7:
                        runtime_mismatches.append((
                            e["title"], ct, tmdb_id, cache_title,
                            hints.runtime_minutes, cache_runtime,
                        ))

            # Weak match check
            poster = raw.get("poster_path")
            vote_count = raw.get("vote_count", 0)
            popularity = raw.get("popularity", 0)
            if not poster and vote_count == 0 and popularity < 2:
                weak_matches.append((e["title"], ct, tmdb_id, cache_title))

            continue

        alt_ct = "movie" if ct == "tv" else "tv"
        alt_path = Path(cache_dir) / alt_ct / f"{tmdb_id}.json"
        if alt_path.exists():
            mismatched.append((e["title"], ct, alt_ct, tmdb_id))
        else:
            missing.append((e["title"], ct, tmdb_id))

    sections = [
        ("unmatched titles (no TMDB ID)",
         unmatched,
         lambda x: f"[{x[1]}] {x[0]}"),
        ("content-type cache mismatches",
         mismatched,
         lambda x: f"{x[0]} — index says {x[1]}, cache at {x[2]}/{x[3]}"),
        ("title mismatches",
         title_mismatches,
         lambda x: f"{x[0]} -> {x[1]}/{x[2]} cached as {x[3]}"),
        ("year mismatches",
         year_mismatches,
         lambda x: f"{x[0]} -> {x[1]}/{x[2]} ({x[3]}): source year {x[4]}, cached year {x[5]}"),
        ("runtime mismatches (>30% off)",
         runtime_mismatches,
         lambda x: f"{x[0]} -> {x[1]}/{x[2]} ({x[3]}): source {x[4]}min, cached {x[5]}min"),
        ("weak TMDB matches (no poster, zero votes)",
         weak_matches,
         lambda x: f"{x[0]} -> {x[1]}/{x[2]} ({x[3]})"),
    ]

    def _report(label, items, formatter, limit=10):
        if not items:
            return
        console.print(f"\n  [yellow]{len(items)} {label} (consider adding overrides):[/yellow]")
        for item in items[:limit]:
            console.print(f"    {formatter(item)}")
        if len(items) > limit:
            console.print(f"    ... and {len(items) - limit} more")

    for label, items, formatter in sections:
        _report(label, items, formatter)

    # Always rewrite the full audit so a clean rerun replaces stale findings
    # from a previous run. Empty sections are still written explicitly with
    # a (0) count so the file is self-describing.
    has_any = any(items for _, items, _ in sections)
    audit_path = Path(audit_output_path)
    try:
        audit_path.parent.mkdir(parents=True, exist_ok=True)
        lines: list[str] = [
            f"# TMDB match audit — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
            f"# Watch index: {len(index.entries)} entries",
            "",
        ]
        if not has_any:
            lines.append("# No mismatches detected.")
            lines.append("")
        for label, items, formatter in sections:
            lines.append(f"## {label} ({len(items)})")
            for item in items:
                lines.append(f"  {formatter(item)}")
            lines.append("")
        audit_path.write_text("\n".join(lines))
        if has_any:
            console.print(f"\n  Full audit written → {audit_path}")
    except OSError as exc:
        log.warning("Failed to write TMDB audit to %s: %s", audit_path, exc)

    if missing:
        log.debug("%d entries with no TMDB cache at all", len(missing))


def _load_platform_events_by_provider(
    fail_on_error: bool = True,
    emit_console: bool = True,
) -> dict[str, tuple[list, list[str]]]:
    """Parse configured provider exports without touching SQLite.

    All-or-nothing per provider: parses ALL configured files before accepting
    any events. If any file fails, that provider is skipped entirely.
    """
    platform_events_by_provider: dict[str, tuple[list, list[str]]] = {}
    ok = True

    for platform, parser in _PLATFORM_PARSERS:
        paths = config.PLATFORM_PATHS.get(platform) or []
        if not paths:
            if emit_console:
                console.print(f"  {platform}: [dim]disabled[/dim]")
            continue

        # Phase 1: parse all files, collecting per-file results
        per_file_events: list[tuple[str, list]] = []
        platform_ok = True
        for path in paths:
            try:
                file_events = parser(path)
                per_file_events.append((path, file_events))
            except (FileNotFoundError, ValueError) as exc:
                if emit_console:
                    console.print(f"  {platform}: [red]FAIL[/red] {exc}")
                else:
                    log.warning("Failed to parse %s export during runtime fallback: %s", platform, exc)
                ok = False
                platform_ok = False
                break

        if not platform_ok:
            continue

        # Phase 2: merge all events for this provider
        all_pevents: list = []
        for _path, file_events in per_file_events:
            all_pevents.extend(file_events)

        # Phase 3: in-memory dedup across files
        deduped_events, dup_count = _dedup_events(all_pevents)

        platform_events_by_provider[platform] = (deduped_events, paths)

        # Phase 4: console reporting
        if emit_console:
            multi_file = len(paths) > 1
            if not deduped_events and not multi_file:
                console.print(f"  {platform}: [green]ok[/green] 0 events (no qualifying watch activity)")
            elif not multi_file:
                dates = [e.timestamp for e in deduped_events]
                console.print(f"  {platform}: [green]ok[/green] {len(deduped_events)} events "
                              f"({min(dates):%Y-%m-%d} to {max(dates):%Y-%m-%d})")
            else:
                # Multi-file: show per-file breakdown
                console.print(f"  {platform}: {len(paths)} files")
                for path, file_events in per_file_events:
                    fname = Path(path).name
                    if file_events:
                        dates = [e.timestamp for e in file_events]
                        console.print(f"    {fname}: {len(file_events):,} events "
                                      f"({min(dates):%Y-%m-%d} to {max(dates):%Y-%m-%d})")
                    else:
                        console.print(f"    {fname}: 0 events")
                if dup_count:
                    console.print(f"  {platform}: {len(deduped_events):,} events after dedup "
                                  f"({dup_count:,} duplicates removed)")
                else:
                    console.print(f"  {platform}: {len(deduped_events):,} events (no duplicates)")

    configured = sum(1 for p in _PLATFORM_PARSERS if config.PLATFORM_PATHS.get(p[0]))
    if configured == 0 and fail_on_error:
        if emit_console:
            console.print("\n[yellow]No providers configured. Set platform_paths in config.local.yaml or config.yaml.[/yellow]")
        sys.exit(1)

    if not ok and fail_on_error:
        if emit_console:
            console.print("\n[red]Validation failed.[/red]")
        sys.exit(1)
    return platform_events_by_provider


def load_platform_events_from_exports(fail_on_error: bool = True) -> list:
    """Load normalized platform events from configured exports without persisting."""
    platform_events_by_provider = _load_platform_events_by_provider(
        fail_on_error=fail_on_error,
        emit_console=False,
    )
    all_events = []
    for pevents, _path in platform_events_by_provider.values():
        all_events.extend(pevents)
    return all_events


def ingest_providers(fail_on_error: bool = True) -> list:
    """Validate configured provider zips, persist to SQLite, and return normalized events."""
    from collections import defaultdict

    console.print("Loading watch history...")

    platform_events_by_provider = _load_platform_events_by_provider(
        fail_on_error=fail_on_error,
        emit_console=True,
    )
    all_events = []
    for pevents, _path in platform_events_by_provider.values():
        all_events.extend(pevents)

    # Parse manual events
    manual_events = []
    if config.MANUAL_TV_PATH and config.MANUAL_MOVIES_PATH:
        try:
            manual_events = parse_manual(config.MANUAL_TV_PATH, config.MANUAL_MOVIES_PATH)
            console.print(f"  manual: [green]ok[/green] {len(manual_events)} events")
        except FileNotFoundError:
            console.print("  manual: [yellow]skipped[/yellow] (files not found)")

    # Persist to SQLite
    event_store.init_db(config.EVENT_DB_PATH)
    # Use configured providers (those with paths set), not just successfully-parsed
    # ones.  A parse failure should not cause removal of that provider's persisted data.
    configured_platforms = [p for p, _ in _PLATFORM_PARSERS if config.PLATFORM_PATHS.get(p)]
    event_store.remove_disabled_providers(config.EVENT_DB_PATH, configured_platforms)

    for platform, (pevents, paths) in platform_events_by_provider.items():
        manifest, snapshot_sha = _build_source_manifest(paths)
        persisted, _total_raw = event_store.replace_provider_events(
            config.EVENT_DB_PATH, platform, pevents, manifest, snapshot_sha,
        )
        console.print(f"  {platform}: {persisted:,} events persisted to SQLite")

    all_events_from_db = event_store.load_events(config.EVENT_DB_PATH)
    all_events_from_db.extend(manual_events)
    
    console.print(f"\n  Total: {len(all_events_from_db)} events")
    
    if all_events:
        by_platform = defaultdict(list)
        for e in all_events:
            by_platform[e.platform].append(e)
        for platform, pevents in sorted(by_platform.items()):
            tv_titles = {e.series_name for e in pevents if e.content_type == "tv"}
            movie_titles = {e.series_name for e in pevents if e.content_type == "movie"}
            console.print(f"  {platform}: {len(tv_titles)} TV shows, {len(movie_titles)} movies")
            
    return all_events_from_db


def run_ingest_only() -> None:
    """Strict preflight validation of configured provider zips, persist to SQLite."""
    ingest_providers(fail_on_error=True)
    console.print("\n[green]All configured providers validated and persisted.[/green]")


def run_setup(refresh_profile: bool = False, refresh_data: bool = False, provider: str | None = None, profile_path: str | None = None) -> None:
    if not config.TMDB_API_KEY:
        console.print("[red]Error: TMDB_API_KEY not set. Export it and re-run.[/red]")
        sys.exit(1)
    try:
        llm = create_client(provider)
    except RuntimeError as exc:
        console.print(f"[red]Error: {exc}[/red]")
        sys.exit(1)
    console.print(f"  LLM provider: {llm.provider}")

    if refresh_profile and not refresh_data:
        console.print("Loading watch history from SQLite...")
        # Refresh flows: load platform events from SQLite, not from zips
        event_store.init_db(config.EVENT_DB_PATH)
        events = event_store.load_events(config.EVENT_DB_PATH)

        manual_events = []
        if config.MANUAL_TV_PATH and config.MANUAL_MOVIES_PATH:
            try:
                manual_events = parse_manual(config.MANUAL_TV_PATH, config.MANUAL_MOVIES_PATH)
                console.print(f"  manual: [green]ok[/green] {len(manual_events)} events")
            except FileNotFoundError:
                console.print("  manual: [yellow]skipped[/yellow] (files not found)")

        events.extend(manual_events)
        if not events:
            console.print("[red]No persisted platform imports found and no manual events available. "
                          "Run ./recommend setup or ./recommend setup --ingest-only first.[/red]")
            sys.exit(1)
        console.print(f"  Total: {len(events)} events (from SQLite + manual)")

    else:
        # Default flow / refresh_data flow: parse zips, persist to SQLite, load back
        events = ingest_providers(fail_on_error=True)
        if not events:
            console.print("[red]No watch events found. Add manual titles or provider exports with qualifying watch activity.[/red]")
            sys.exit(1)


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
        raw_enrichments = json.loads(enrichments_index_path.read_text()) if enrichments_index_path.exists() else {}
        index = wi.load(config.WATCH_INDEX_PATH)
        enrichments = _title_keyed_enrichments(raw_enrichments, index.entries, metadata)
    else:
        console.print("\nFetching TMDB metadata...")
        tmdb = TmdbClient(api_key=config.TMDB_API_KEY, cache_dir=config.CACHE_DIR)

        # Load overrides
        title_overrides = ov.load(config.OVERRIDES_PATH)
        if title_overrides:
            console.print(f"  Loaded {len(title_overrides)} title overrides")

        title_type: dict[tuple[str, str], str] = {}
        for e in events:
            key = e.series_name if e.content_type == 'tv' else e.title
            title_type[(key, e.content_type)] = e.content_type

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
                title_type[(key, e.content_type)] = e.content_type

        # Build source hints for TMDB candidate ranking
        hints_map = _build_hints_map(events)

        metadata = {}
        skipped = len(skip_titles)
        with _progress_bar("Fetching TMDB metadata") as progress:
            task_id = progress.add_task("tmdb", total=len(title_type))
            for i, ((title, _), ct) in enumerate(title_type.items()):
                progress.update(task_id, completed=i + 1)
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
                            metadata[(title, ct)] = tmdb._parse_metadata(cached, ct)
                        else:
                            try:
                                data = tmdb._fetch_details(override["tmdb_id"], ct)
                                tmdb._save_cache(ct, override["tmdb_id"], data)
                                metadata[(title, ct)] = tmdb._parse_metadata(data, ct)
                            except Exception as exc:
                                log.warning("Override TMDB fetch failed for %s (ID %d): %s",
                                            title, override["tmdb_id"], exc)
                    else:
                        hints = hints_map.get((title, ct))
                        meta = tmdb.get_metadata(search_title, ct, hints=hints)
                        if meta:
                            metadata[(title, ct)] = meta
                else:
                    hints = hints_map.get((title, ct))
                    meta = tmdb.get_metadata(title, ct, hints=hints)
                    if meta:
                        metadata[(title, ct)] = meta
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

        # Audit: report cache mismatches (wrong TMDB match candidates)
        _audit_cache_mismatches(index, config.CACHE_DIR, hints_map)

        # Clean up stale cache files for removed/deduped entries
        removed = wi.cleanup_stale_cache(index, config.ENRICHMENT_CACHE_DIR, config.PROVIDERS_CACHE_DIR)
        total_removed = sum(removed.values())
        if total_removed:
            console.print(f"  Cleaned {total_removed} stale cache files "
                          f"({removed['enrichments']} enrichments, "
                          f"{removed['enrichment_index']} index entries, "
                          f"{removed['providers']} provider files)")

        with _progress_bar(
            "Enriching titles",
            with_extra="[dim]cache {task.fields[cache_hits]}[/dim]",
        ) as progress:
            task_id = progress.add_task("enrich", total=len(metadata), cache_hits=0)
            cache_hits = 0

            def _on_progress(done: int, total: int, was_cached: bool) -> None:
                nonlocal cache_hits
                if was_cached:
                    cache_hits += 1
                progress.update(task_id, completed=done, cache_hits=cache_hits)

            raw_enrichments = enrich_batch(metadata, config.ENRICHMENT_CACHE_DIR, llm, on_progress=_on_progress)
        enrichments_index_path.write_text(json.dumps(raw_enrichments))
        console.print(f"  {len(raw_enrichments)} descriptions cached → {config.ENRICHMENT_CACHE_DIR}")

        enrichments = _title_keyed_enrichments(raw_enrichments, index.entries, metadata)

    using_custom_path = profile_path is not None
    resolved_profile_path = Path(profile_path) if using_custom_path else Path(config.TASTE_PROFILE_PATH)
    if refresh_profile or not resolved_profile_path.exists():
        user_store.ensure_user_store(config.EVENT_DB_PATH, config.FEEDBACK_PATH)
        ratings = user_store.load_ratings(config.EVENT_DB_PATH)
        liked_count = sum(1 for r in ratings if r["rating"] == "liked")
        disliked_count = sum(1 for r in ratings if r["rating"] == "disliked")
        if liked_count or disliked_count:
            console.print(f"  Applying feedback: {liked_count} liked, {disliked_count} disliked titles")

        scores = compute_scores(events, metadata, config.RECENCY_HALF_LIFE_DAYS)
        scores = user_store.apply_rating_multipliers(scores, ratings)
        negative_prefs = user_store.get_disliked_titles(config.EVENT_DB_PATH)

        # We don't know batch count until inside build(); use an indeterminate
        # bar that updates once the first callback arrives with the real total.
        with _progress_bar("Building taste profile") as progress:
            task_id = progress.add_task("profile", total=None)

            def _on_batch_progress(done: int, total: int) -> None:
                progress.update(task_id, completed=done, total=total)

            try:
                profile = build_taste_profile(
                    events, scores, enrichments, llm,
                    negative_prefs=negative_prefs or None,
                    on_batch_progress=_on_batch_progress,
                )
            except RuntimeError as exc:
                console.print(f"\n[red]{exc}[/red]")
                console.print("[yellow]Previous profile kept unchanged.[/yellow]")
                sys.exit(1)
            structured_profile = None
            structured_profile_skipped = False
            if not using_custom_path:
                try:
                    structured_profile = build_structured_profile(
                        events,
                        scores,
                        enrichments,
                        llm,
                        negative_prefs=negative_prefs or None,
                    )
                except Exception as exc:
                    structured_profile_skipped = True
                    console.print(f"[yellow]Structured taste profile skipped: {exc}[/yellow]")
        resolved_profile_path.parent.mkdir(parents=True, exist_ok=True)
        # Auto-backup previous profile before overwriting, but only for the canonical path.
        # When writing to a custom path we are creating a new file alongside the default, not replacing it.
        if not using_custom_path and resolved_profile_path.exists():
            from datetime import datetime
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            backup = resolved_profile_path.with_name(f"taste_profile_{ts}.txt")
            resolved_profile_path.rename(backup)
            console.print(f"  Previous profile backed up → {backup.name}")
        resolved_profile_path.write_text(profile)
        console.print(f"  Taste profile saved → {resolved_profile_path}")
        if structured_profile is not None:
            try:
                save_structured_profile(structured_profile, config.STRUCTURED_TASTE_PROFILE_PATH)
                console.print(f"  Structured taste profile saved → {config.STRUCTURED_TASTE_PROFILE_PATH}")
            except Exception as exc:
                console.print(f"[yellow]Structured taste profile skipped: {exc}[/yellow]")
                _remove_structured_profile(config.STRUCTURED_TASTE_PROFILE_PATH)
        elif not using_custom_path and structured_profile_skipped:
            _remove_structured_profile(config.STRUCTURED_TASTE_PROFILE_PATH)
        if not using_custom_path:
            stale_flag = Path(config.PROFILE_STALE_FLAG)
            if stale_flag.exists():
                stale_flag.unlink()
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
    parser.add_argument("--provider", choices=["anthropic", "gemini", "openai", "local"],
                        help="LLM provider (default: from config/env)")
    parser.add_argument("--profile-path", type=str, default=None,
                        help="Write taste profile to this path instead of the default")
    args = parser.parse_args()
    from recommender.log import setup_logging
    setup_logging(level_override="DEBUG" if args.debug else None)
    if args.ingest_only:
        run_ingest_only()
    else:
        run_setup(refresh_profile=args.refresh_profile, refresh_data=args.refresh_data,
                  provider=args.provider, profile_path=args.profile_path)
