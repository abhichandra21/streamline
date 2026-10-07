import argparse
import hashlib
import json
import logging
import re
import shutil
import sqlite3
import sys
import unicodedata
from datetime import datetime, timedelta
from difflib import SequenceMatcher
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
from recommender import imdb_ratings, language_catalog
from recommender.franchise import collapse_collections, collection_members
from recommender.signals import STRONG_WEIGHT, compute_scores
from recommender.tmdb_client import TmdbClient, TmdbMetadata, MatchHints
from recommender.enricher import (
    enrich_batch,
    enrichment_key,
    enrichment_key_from_parts,
    is_identity_enrichment_index,
)
from recommender.taste_profile_builder import build as build_taste_profile
from recommender.structured_profile import save_structured_profile
from recommender.taste_rows import build_tag_profile
from recommender.llm import create_client
from recommender import watch_index as wi
from recommender import user_store
from recommender import overrides as ov
from recommender import event_store
from recommender.log import console
from recommender.ingestion.base import SYNTHETIC_TIMESTAMP_PLATFORMS, WatchEvent


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


# A title that's entirely single letters separated by periods, e.g. "I.T."
# or "U.S.A." -- an acronym-style title where the periods are load-bearing.
# Stripping them would collapse "I.T." (the 2016 film) to the same string as
# "It" (Stephen King's), making two distinct works compare as equal.
_ACRONYM_RE = re.compile(r'^(?:[a-z]\.){2,}$|^(?:[a-z]\.){1,}[a-z]$')


def _normalize_audit_title(title: str) -> str:
    title = unicodedata.normalize("NFKD", title)
    title = "".join(c for c in title if not unicodedata.combining(c))
    title = title.lower()
    title = re.sub(r'\s*\([^)]*\)', '', title)
    title = title.replace('&', 'and')
    title = re.sub(r'^(the|a|an)\s+', '', title.strip())
    title = title.strip()
    if _ACRONYM_RE.match(title):
        return title
    # Drop punctuation entirely rather than leaving it to the fuzzy-ratio
    # fallback -- "WALL-E" vs "WALL·E", "'83" vs "83" are the same
    # title with different punctuation glyphs, not a cosmetic near-miss.
    title = re.sub(r'[^\w\s]', '', title)
    return title.strip()


def _extract_parenthetical_year(title: str) -> int | None:
    """Extract a trailing "(YYYY)" disambiguator from a raw title, e.g. the
    "2005" in "Doctor Who (2005)".

    _normalize_audit_title strips parenthetical content before any title
    comparison, so when a title's *only* disambiguating signal is a
    parenthetical year and hints_map has no release_year for it, that
    signal would otherwise be discarded before the year-mismatch check ever
    sees it -- silently hiding same-title reboot/remake collisions.
    """
    match = re.search(r'\((\d{4})\)\s*$', title)
    return int(match.group(1)) if match else None


def _cache_title(cache_path: Path) -> str:
    try:
        raw = json.loads(cache_path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        log.debug("Unable to inspect TMDB cache %s: %s", cache_path, exc)
        return ""
    return raw.get("name") or raw.get("title") or ""


# Deliberately high: only meant to absorb cosmetic differences normalization
# doesn't already catch (stray punctuation, minor spelling). "Avatar" vs
# "Avatar: The Way of Water" scores ~0.41 and "Dune" vs "Dune: Part Two"
# scores ~0.47 -- comfortably below this, so franchise/sequel collisions are
# still rejected.
_TITLE_COMPATIBLE_THRESHOLD = 0.85


def _titles_are_compatible(index_title: str, cache_title: str) -> bool:
    index_norm = _normalize_audit_title(index_title)
    cache_norm = _normalize_audit_title(cache_title)
    if not index_norm or not cache_norm:
        return False
    if index_norm == cache_norm:
        return True
    # No substring/containment acceptance: a shorter title being contained in
    # a longer one does not mean they're the same work. "Avatar" is a
    # substring of "Avatar: The Way of Water", and "Apollo 11" is a
    # substring of "Man on the Moon: The Epic Journey of Apollo 11" -- both
    # are wrong-match cases the audit exists to catch, not cosmetic
    # variants. Only a high overall similarity ratio is accepted instead.
    return SequenceMatcher(None, index_norm, cache_norm).ratio() >= _TITLE_COMPATIBLE_THRESHOLD


# "Furious 7 - Extended Edition" is Furious 7; the suffix names a cut, not a
# different work. A separator is required so titles that ARE the phrase
# ("The Final Cut") are left alone.
_EDITION_SUFFIX_RE = re.compile(
    r"(?:\s*[-\u2013\u2014:]\s*|\s+)(?:"
    r"(?:extended|unrated|theatrical|director'?s|special|remastered|ultimate|final|collector'?s)"
    r"(?:\s+[\w']+)?\s+(?:edition|cut|version)"
    r"|extended|unrated|uncut)\s*$",
    re.IGNORECASE,
)



def _strip_edition_suffix(title: str) -> str:
    title = title.replace("\u2019", "'").replace("\u2018", "'")
    title = re.sub(r"\s*\([^)]*\)\s*$", "", title)
    return _EDITION_SUFFIX_RE.sub("", title).strip()


def _resolve_tmdb_id_override(
    tmdb: TmdbClient, title: str, ct: str, tmdb_id: int, search_title: str | None = None,
    trust: bool = False, rejected: list[str] | None = None,
) -> object | None:
    """Resolve a `{"tmdb_id": X}` override, rejecting it if the resolved
    TMDB entry doesn't plausibly match the source title.

    A bare tmdb_id override is trusted blindly today, which lets a bogus
    override (e.g. an LLM parroting a wrong ID back from an audit report)
    permanently pin a title to the wrong work. This validates the override
    the same way the audit validates indexed entries, before persisting it.

    Direct IDs exist specifically to route around failed/noisy searches
    (raw non-Latin titles, a corrected title supplied via the override's own
    "title" field), so the check accepts a match against any of: the raw
    source title, the override's corrected search_title, the cache's
    localized title/name, or the cache's original_title/original_name.

    Deliberate abbreviations ("LOTR: Fellowship" for The Lord of the Rings:
    The Fellowship of the Ring) look nothing like the real title by any
    string-similarity measure, so `trust=True` (the override's own "trust"
    field) skips the plausibility check entirely -- this is the escape
    hatch back to the pre-validation "force this ID" behavior for entries
    the user has manually verified.

    Returns parsed TmdbMetadata if the override is plausible (or trusted),
    or None if it was rejected (caller should fall back to a fresh search)
    or the fetch failed. Each failure is also appended to `rejected` so setup
    can list them at the end instead of leaving them in the log.
    """
    cached = tmdb._load_cache(ct, tmdb_id)
    if cached is not None:
        raw = cached
    else:
        try:
            raw = tmdb._fetch_details(tmdb_id, ct)
        except Exception as exc:
            msg = f"Override TMDB fetch failed for {title} (ID {tmdb_id}): {exc}"
            log.warning(msg)
            console.print(f"  [yellow]{msg}[/yellow]")
            if rejected is not None:
                rejected.append(f"{title!r}: tmdb_id {tmdb_id} could not be fetched")
            return None
        tmdb._save_cache(ct, tmdb_id, raw)

    if trust:
        return tmdb._parse_metadata(raw, ct)

    cache_title = raw.get("name") or raw.get("title") or ""
    cache_original_title = raw.get("original_name") or raw.get("original_title") or ""

    source_titles = {title}
    if search_title:
        source_titles.add(search_title)
    # A stripped title must match exactly after normalization: fuzzy matching
    # would let "Scream 2 - Extended Edition" pass for "Scream 3".
    stripped_titles = {
        stripped for t in source_titles
        if (stripped := _strip_edition_suffix(t)) and stripped != t
    }
    cache_titles = {t for t in (cache_title, cache_original_title) if t}

    is_plausible = any(
        _titles_are_compatible(source, cache) for source in source_titles for cache in cache_titles
    ) or any(
        _normalize_audit_title(stripped) == _normalize_audit_title(cache)
        for stripped in stripped_titles for cache in cache_titles
    )
    if not is_plausible:
        msg = (
            f"Rejecting override for {title!r}: tmdb_id {tmdb_id} resolves to "
            f"{cache_title!r} (original: {cache_original_title!r}), which does not "
            f"plausibly match the source title. Falling back to a fresh search — "
            f"fix or remove this entry in the overrides file."
        )
        log.warning(msg)
        console.print(f"  [yellow]{msg}[/yellow]")
        if rejected is not None:
            rejected.append(f"{title!r}: tmdb_id {tmdb_id} is {cache_title!r}")
        return None

    return tmdb._parse_metadata(raw, ct)


_USER_STATE_TABLES = ("show_tracking", "title_ratings", "saved_titles", "manual_archive_entries")


def _snapshot_previous_index(index_path: str, ts: str | None = None) -> tuple[Path | None, str | None]:
    """Copy the index about to be overwritten to watch_index_<ts>.json beside
    it. Returns (backup path or None if there was nothing to copy, error)."""
    src = Path(index_path)
    if not src.exists():
        return None, None
    ts = ts or datetime.now().strftime("%Y%m%d_%H%M%S")
    backup = src.with_name(f"{src.stem}_{ts}{src.suffix}")
    try:
        shutil.copy2(src, backup)
    except OSError as exc:
        log.warning("Could not back up previous watch index: %s", exc)
        return None, str(exc)
    return backup, None


def _read_user_state(db_path: str) -> dict[str, list[dict]]:
    """Every row of the user tables, read through a read-only connection."""
    state: dict[str, list[dict]] = {t: [] for t in _USER_STATE_TABLES}
    if not Path(db_path).exists():
        return state
    conn = sqlite3.connect(Path(db_path).resolve().as_uri() + "?mode=ro", uri=True, timeout=30.0)
    conn.row_factory = sqlite3.Row
    try:
        for table in _USER_STATE_TABLES:
            try:
                state[table] = [dict(r) for r in conn.execute(f"SELECT * FROM {table}")]
            except sqlite3.OperationalError as exc:
                if "no such table" not in str(exc):
                    raise
    finally:
        conn.close()
    return state


def _index_keys(entries: list[dict]) -> dict[tuple[str, int], list[str]]:
    keys: dict[tuple[str, int], list[str]] = {}
    for e in entries:
        if e.get("tmdb_id"):
            keys.setdefault((e.get("content_type", "movie"), e["tmdb_id"]), []).append(e["title"])
    return keys


def _find_gone_keys(old_entries: list[dict], new_entries: list[dict]) -> list[dict]:
    """Every (content_type, tmdb_id) in the old index that the new one lacks,
    with the old titles and the new entries whose title matches one of them.
    Keyed by ID, not title, so merges, collisions and respellings are covered."""
    old_keys = _index_keys(old_entries)
    new_keys = _index_keys(new_entries)
    new_by_title: dict[str, list[dict]] = {}
    for e in new_entries:
        new_by_title.setdefault(_normalize_audit_title(e["title"]), []).append({
            "title": e["title"], "content_type": e.get("content_type", "movie"),
            "tmdb_id": e.get("tmdb_id") or None,
        })
    gone = []
    for (ct, tmdb_id), titles in old_keys.items():
        if (ct, tmdb_id) in new_keys:
            continue
        likely = []
        for t in titles:
            for cand in new_by_title.get(_normalize_audit_title(t), []):
                if cand not in likely:
                    likely.append(cand)
        gone.append({
            "old": {"content_type": ct, "tmdb_id": tmdb_id},
            "old_titles": titles,
            "likely_new": likely,
        })
    return gone


def _attach_user_state(gone: list[dict], state: dict[str, list[dict]], new_keys: set) -> list[dict]:
    """Add `user_state` (the actual rows) to each gone key and return those
    that have any. A row with the same id but the other content type counts
    as a "type mismatch", whether or not the other key is still indexed."""
    with_state = []
    for change in gone:
        ct, tmdb_id = change["old"]["content_type"], change["old"]["tmdb_id"]
        found: dict[str, list[dict]] = {}
        for table, rows in state.items():
            for row in rows:
                if row.get("tmdb_id") != tmdb_id:
                    continue
                row_ct = row.get("content_type") or "tv"  # show_tracking is TV only
                if row_ct == ct:
                    found.setdefault(table, []).append({**row, "match": "exact"})
                else:
                    label = ("type mismatch (other type still in index)"
                             if (row_ct, tmdb_id) in new_keys else "type mismatch")
                    found.setdefault(table, []).append({**row, "match": label})
        change["user_state"] = found
        if found:
            with_state.append(change)
    return with_state


def _find_ambiguous_keys(old_entries: list[dict], new_entries: list[dict]) -> list[dict]:
    """Keys in both indexes whose watched titles or platforms differ. The ID
    survived but it may now stand for a different watched title (e.g. /10 and
    /20 becoming /30 and /10). `last_watched` is ignored: it changes on every
    new watch."""
    def collect(entries):
        out: dict[tuple[str, int], dict] = {}
        for e in entries:
            if not e.get("tmdb_id"):
                continue
            rec = out.setdefault((e.get("content_type", "movie"), e["tmdb_id"]),
                                 {"titles": set(), "platforms": set()})
            rec["titles"].add(_normalize_audit_title(e["title"]))
            rec["platforms"].update(e.get("platforms") or [])
        return out

    old, new = collect(old_entries), collect(new_entries)
    ambiguous = []
    for key in old.keys() & new.keys():
        if old[key] != new[key]:
            ambiguous.append({
                "old": {"content_type": key[0], "tmdb_id": key[1]},
                "old_titles": sorted(old[key]["titles"]), "new_titles": sorted(new[key]["titles"]),
                "old_platforms": sorted(old[key]["platforms"]),
                "new_platforms": sorted(new[key]["platforms"]),
            })
    return ambiguous


def _build_rematch_report(backup_path: Path | None, backup_error: str | None,
                          new_entries: list[dict]) -> dict:
    """Compare the previous index (the backup) with the new one and write the
    JSON report right away so a later failure cannot lose it. Read-only on
    the user DB. Never raises."""
    if backup_error:
        return {"status": "failed", "error": f"could not back up the previous index: {backup_error}"}
    if backup_path is None:
        return {"status": "none"}
    try:
        try:
            old_entries = wi.load(str(backup_path)).entries
        except Exception as exc:
            log.warning("Previous watch index unreadable: %s", exc)
            return {"status": "unreadable", "path": str(backup_path)}
        gone = _find_gone_keys(old_entries, new_entries)
        candidates = _find_ambiguous_keys(old_entries, new_entries)
        if not gone and not candidates:
            return {"status": "ok", "all_changes": [], "with_state": [], "ambiguous": []}
        state_error = None
        ambiguous: list[dict] = []
        try:
            state = _read_user_state(config.EVENT_DB_PATH)
            new_keys = set(_index_keys(new_entries))
            with_state = _attach_user_state(gone, state, new_keys)
            ambiguous = _attach_user_state(candidates, state, new_keys)
        except Exception as exc:
            state_error = str(exc)
            log.warning("Could not read user state for the rematch report: %s", exc)
            with_state = []
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        out_path = Path(config.WATCH_INDEX_PATH).parent / f"rematches_{ts}.json"
        out_path.write_text(json.dumps({
            "previous_index_backup": str(backup_path),
            "user_state_check_failed": state_error,
            "with_user_state": with_state,
            "ambiguous": ambiguous,
            "ambiguous_candidates": candidates if state_error else [],
            "all_changes": gone,
        }, indent=2, default=str))
        return {"status": "ok", "path": str(out_path), "backup": str(backup_path),
                "all_changes": gone, "with_state": with_state, "ambiguous": ambiguous,
                "ambiguous_candidates": candidates if state_error else [],
                "state_error": state_error}
    except Exception as exc:
        log.warning("Rematch report failed: %s", exc)
        return {"status": "failed", "error": str(exc)}


def _describe_gone(change: dict) -> str:
    old = f"{change['old']['content_type']}/{change['old']['tmdb_id']}"
    titles = ", ".join(change["old_titles"])
    likely = ", ".join(
        f"{c['content_type']}/{c['tmdb_id']}" if c["tmdb_id"] else "unmatched"
        for c in change["likely_new"]
    ) or "unknown"
    return f"{titles}: {old} -> {likely}"


def _describe_ambiguous(change: dict) -> str:
    key = f"{change['old']['content_type']}/{change['old']['tmdb_id']}"
    return (f"{key}: titles {change['old_titles']} -> {change['new_titles']}, "
            f"platforms {change['old_platforms']} -> {change['new_platforms']}")


def _describe_state(change: dict) -> str:
    parts = []
    for table, rows in change.get("user_state", {}).items():
        label = {"show_tracking": "followed", "saved_titles": "saved",
                 "manual_archive_entries": "manual entry"}.get(table)
        if table == "title_ratings":
            label = "rated " + "/".join(str(r["rating"]) for r in rows)
        elif table == "show_tracking" and any(r["state"] == "ignored" for r in rows):
            label = "ignored"
        mismatch = next((r["match"] for r in rows if r["match"] != "exact"), None)
        if mismatch:
            label += f" ({mismatch})"
        parts.append(label)
    return ", ".join(parts)


def _print_rematch_summary(result: dict) -> None:
    status = result.get("status")
    if status == "unreadable":
        console.print(f"\n[yellow]Rematch check skipped: previous index unreadable "
                      f"({result['path']})[/yellow]")
    elif status == "failed":
        console.print(f"\n[yellow]Rematch check failed: {result['error']}[/yellow]")
    elif status == "ok" and (result["all_changes"] or result["ambiguous"]
                             or result.get("ambiguous_candidates")):
        console.print(f"\n[yellow]{len(result['all_changes'])} TMDB matches from the last build "
                      f"are gone. Full list: {result['path']} (previous index: {result['backup']})[/yellow]")
        if result["state_error"]:
            console.print(f"  [red]Could not read user state ({result['state_error']}); "
                          f"follows or ratings may be attached to these:[/red]")
            for change in result["all_changes"]:
                console.print(f"  {_describe_gone(change)}")
            for change in result["ambiguous_candidates"]:
                console.print(f"  {_describe_ambiguous(change)}")
        elif result["with_state"]:
            console.print(f"[yellow]{len(result['with_state'])} have user state on the old ID "
                          f"(not moved; re-follow or re-rate if the new match is right):[/yellow]")
            for change in result["with_state"]:
                console.print(f"  {_describe_gone(change)} ({_describe_state(change)})")
        if result["ambiguous"]:
            console.print(f"[yellow]{len(result['ambiguous'])} IDs survived but now stand for different "
                          f"watched titles or platforms, with user state attached "
                          f"(same ID, different watched title):[/yellow]")
            for change in result["ambiguous"]:
                console.print(f"  {_describe_ambiguous(change)} ({_describe_state(change)})")


def _print_override_problems(rejected: list[str], duplicate_keys: list[str]) -> None:
    """List overrides that are not doing what the file says, at the end of setup."""
    if rejected:
        console.print(f"\n[yellow]{len(rejected)} tmdb_id overrides were rejected "
                      f"(fell back to a fresh search):[/yellow]")
        for line in rejected:
            console.print(f"  {line}")
    if duplicate_keys:
        console.print(f"\n[yellow]{len(duplicate_keys)} duplicate keys in the overrides file "
                      f"(only the last entry of each is used):[/yellow]")
        for key in duplicate_keys:
            console.print(f"  {key}")


def _build_hints_map(events: list) -> dict[tuple[str, str], MatchHints]:
    """Build per-title MatchHints from source event data.

    Merges hints across every event sharing a (title, content_type) key
    instead of taking only the first event seen. A title watched on
    multiple platforms can have complementary hints spread across events --
    e.g. a Netflix partial watch supplying only a rough runtime, while a
    manual entry for the same title supplies the release year. Taking only
    the first event would silently drop whichever hint the other event
    carried.
    """
    accum: dict[tuple[str, str], dict] = {}
    for e in events:
        key_title = e.series_name if e.content_type == 'tv' else e.title
        map_key = (key_title, e.content_type)

        release_year = getattr(e, 'release_year_hint', None)
        language = getattr(e, 'language_hint', None)

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

        # Synthetic timestamps must never become (or lower) a first-watch date.
        watch_date = (
            e.timestamp.date()
            if e.platform not in SYNTHETIC_TIMESTAMP_PLATFORMS and e.timestamp else None
        )

        if not (release_year or runtime_minutes or language or watch_date):
            continue

        fields = accum.setdefault(map_key, {
            "release_year": None, "runtime_minutes": None,
            "runtime_is_exact": False, "language": None, "first_watch_date": None,
        })
        if watch_date and (fields["first_watch_date"] is None or watch_date < fields["first_watch_date"]):
            fields["first_watch_date"] = watch_date
        if release_year and not fields["release_year"]:
            fields["release_year"] = release_year
        if language and not fields["language"]:
            fields["language"] = language
        if runtime_minutes and (
            fields["runtime_minutes"] is None
            or (runtime_is_exact and not fields["runtime_is_exact"])
        ):
            # Prefer an exact runtime measurement (apple_tv) over an
            # inexact one (netflix/prime watched-duration heuristic).
            fields["runtime_minutes"] = runtime_minutes
            fields["runtime_is_exact"] = runtime_is_exact

    return {key: MatchHints(**fields) for key, fields in accum.items()}


def _build_tmdb_id_hints(events: list) -> dict[tuple[str, str], int]:
    """Map each (title, content_type) group to the exact TMDB ID a source supplied.

    Only Plex supplies IDs today. Setup fetches one TMDB entry per group, so
    the first hinted event in a group decides; a later event in the same
    group with a different ID is logged, not used.
    """
    hints: dict[tuple[str, str], int] = {}
    # An archive ID must not rewrite the match of a title that has real history.
    history_keys = {
        (e.series_name if e.content_type == 'tv' else e.title, e.content_type)
        for e in events if e.platform != 'archive'
    }
    for e in events:
        tmdb_id = getattr(e, 'tmdb_id_hint', None)
        if not tmdb_id:
            continue
        key = (e.series_name if e.content_type == 'tv' else e.title, e.content_type)
        if e.platform == 'archive' and key in history_keys:
            continue
        if key not in hints:
            hints[key] = tmdb_id
        elif hints[key] != tmdb_id:
            log.warning(
                "Conflicting TMDB IDs for %r (%s): keeping %s, ignoring %s",
                key[0], key[1], hints[key], tmdb_id,
            )
    return hints


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

            # Year mismatch check. Falls back to a parenthetical year in
            # the raw title itself when hints_map has no release_year --
            # otherwise that's the only disambiguating signal available and
            # _titles_are_compatible already stripped it before comparison.
            hints = hints_map.get((e["title"], ct))
            hint_year = hints.release_year if hints else None
            if not hint_year:
                hint_year = _extract_parenthetical_year(e["title"])
            if hint_year:
                date_str = raw.get("first_air_date") if ct == "tv" else raw.get("release_date")
                if date_str and len(date_str) >= 4:
                    cache_year = int(date_str[:4])
                    if abs(cache_year - hint_year) > 2:
                        year_mismatches.append((
                            e["title"], ct, tmdb_id, cache_title,
                            hint_year, cache_year,
                        ))

            # Runtime mismatch check. Only meaningful for an exact runtime
            # measurement (apple_tv). The netflix/prime heuristic hint is
            # just how much of the movie was watched, not its runtime -- a
            # legitimately-matched 120min movie stopped at 61 minutes would
            # otherwise fail the ratio check against its own correct cache
            # entry, producing a false-positive audit mismatch.
            if hints and hints.runtime_minutes and hints.runtime_is_exact:
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

    # High-confidence real bugs: entries failing the title check AND a
    # year/runtime check. A title mismatch alone is often cosmetic noise
    # (punctuation, diacritics); failing two independent checks at once is a
    # much stronger signal of an actual wrong match.
    title_mismatch_keys = {(t, c, tid) for t, c, tid, _ in title_mismatches}
    year_mismatch_keys = {(t, c, tid) for t, c, tid, *_ in year_mismatches}
    runtime_mismatch_keys = {(t, c, tid) for t, c, tid, *_ in runtime_mismatches}
    high_confidence_keys = title_mismatch_keys & (year_mismatch_keys | runtime_mismatch_keys)

    # Same-title-but-decades-off is just as strong a signal even though the
    # title itself is compatible: a reboot/remake collision (e.g. "Doctor
    # Who" 2005 cached as the unrelated 1963 original) has an identical
    # normalized title, so it never lands in title_mismatches at all and
    # would otherwise be invisible to this section.
    _SEVERE_YEAR_GAP = 10
    severe_year_entries = {
        (t, c, tid): (cache_t, hint_year, cache_year)
        for t, c, tid, cache_t, hint_year, cache_year in year_mismatches
        if abs(hint_year - cache_year) >= _SEVERE_YEAR_GAP
    }
    high_confidence_keys |= set(severe_year_entries)

    high_confidence = []
    for item in title_mismatches:
        key = (item[0], item[1], item[2])
        if key in high_confidence_keys:
            high_confidence.append((item[0], item[1], item[2], f"cached as {item[3]}"))
    for (t, c, tid), (cache_t, hint_year, cache_year) in severe_year_entries.items():
        if (t, c, tid) not in title_mismatch_keys:
            high_confidence.append((
                t, c, tid,
                f"cached as {cache_t} ({cache_year}) but source year is {hint_year}",
            ))

    sections = [
        ("likely real bugs (title + year/runtime both off, or same title decades off)",
         high_confidence,
         lambda x: f"{x[0]} -> {x[1]}/{x[2]} {x[3]}"),
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
        # The high-confidence section is the prioritized signal -- show it
        # in full rather than truncating to 10 like the noisier sections.
        limit = len(high_confidence) if items is high_confidence else 10
        _report(label, items, formatter, limit=limit)

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


def _import_manual_events() -> list | None:
    """Parse the manual files and replace the stored "manual" snapshot.

    Returns the parsed events, or None when the files are missing. Missing
    files leave the stored events alone; existing files that parse to zero
    events replace them with nothing.
    """
    if not (config.MANUAL_TV_PATH and config.MANUAL_MOVIES_PATH):
        return None
    try:
        events = parse_manual(config.MANUAL_TV_PATH, config.MANUAL_MOVIES_PATH)
    except FileNotFoundError:
        console.print("  manual: [yellow]skipped[/yellow] (files not found, keeping stored events)")
        return None
    event_store.init_db(config.EVENT_DB_PATH)
    manifest, snapshot_sha = _build_source_manifest(
        [config.MANUAL_TV_PATH, config.MANUAL_MOVIES_PATH])
    persisted, _total_raw = event_store.replace_provider_events(
        config.EVENT_DB_PATH, "manual", events, manifest, snapshot_sha,
    )
    console.print(f"  manual: {persisted:,} events persisted to SQLite")
    return events


def _profile_scores(
    events: list[WatchEvent],
    metadata: dict,
    index_entries: list[dict],
    ratings: list[dict],
    tracking: list[dict],
) -> dict[str, float]:
    """Taste-profile scores. Ratings and follows match titles by TMDB ID.

    With viewing signals off, More and Follow are built into the weights and
    Less-rated titles are left out, so they only appear as "not for you".
    """
    key_for_tmdb = {
        (e["content_type"], e["tmdb_id"]): e["title"]
        for e in index_entries if e.get("tmdb_id")
    }

    def keys_rated(rating: str) -> set[str]:
        # Both the TMDB-matched title and the stored title, since two event titles
        # can share one TMDB identity and the index keeps only one of them.
        return {
            key
            for r in ratings if user_store.normalize_rating(r["rating"]) == rating
            for key in (user_store.rating_score_key(r, key_for_tmdb), r["title"])
        }

    followed = {
        key_for_tmdb.get(("tv", t["tmdb_id"]), t["title"])
        for t in tracking if t["state"] == "following"
    }
    scores = compute_scores(
        events, metadata, config.RECENCY_HALF_LIFE_DAYS,
        followed_keys=followed, more_keys=keys_rated(user_store.RATING_MORE),
    )
    if config.USE_VIEWING_SIGNALS:
        return user_store.apply_rating_multipliers(scores, ratings, key_for_tmdb)
    less = keys_rated(user_store.RATING_LESS)
    return {key: score for key, score in scores.items() if key not in less}


def _archive_events() -> list[WatchEvent]:
    """Turn "Seen it" archive entries into watch events for this run only.

    They live in their own table, so they are loaded fresh each setup and never
    stored as provider events. Their date is when the owner tapped, which is why
    the 'archive' platform is in SYNTHETIC_TIMESTAMP_PLATFORMS.
    """
    user_store.ensure_user_store(config.EVENT_DB_PATH, config.FEEDBACK_PATH)
    events = []
    for row in user_store.list_manual_archive(config.EVENT_DB_PATH):
        is_tv = row["content_type"] == "tv"
        duration = timedelta(minutes=config.MANUAL_TV_DURATION_MINUTES if is_tv
                             else config.MANUAL_MOVIE_DURATION_MINUTES)
        try:
            timestamp = datetime.fromisoformat(row["watched_at"]).replace(tzinfo=None)
        except ValueError:
            timestamp = datetime.now()
        events.append(WatchEvent(
            platform="archive",
            title=row["title"],
            content_type=row["content_type"],
            series_name=row["title"],
            watched_duration=duration,
            total_duration=duration,
            timestamp=timestamp,
            profile="",
            tmdb_id_hint=row["tmdb_id"] or None,
        ))
    return events


def _drop_archive_duplicates(archive: list[WatchEvent], events: list[WatchEvent]) -> list[WatchEvent]:
    """Drop archive entries whose TMDB ID already belongs to provider or manual history.

    History IDs come from source hints (Plex) and from the last watch index, where
    entries with a non-archive platform are real history.
    """
    seen = {(e.content_type, e.tmdb_id_hint) for e in events if e.tmdb_id_hint}
    index_path = Path(config.WATCH_INDEX_PATH)
    if index_path.exists():
        for entry in wi.load(str(index_path)).entries:
            if entry.get("tmdb_id") and set(entry.get("platforms") or []) - {"archive"}:
                seen.add((entry.get("content_type", "movie"), entry["tmdb_id"]))
    return [e for e in archive if not (e.tmdb_id_hint and (e.content_type, e.tmdb_id_hint) in seen)]


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

    manual_events = _import_manual_events()
    if manual_events is not None:
        all_events.extend(manual_events)

    all_events_from_db = event_store.load_events(config.EVENT_DB_PATH)

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


def refresh_imdb_ratings() -> bool:
    """Download IMDb's ratings file into the local copy.

    Ratings are an enhancement over TMDB's, so a failure warns and leaves the
    previous copy (or TMDB-only ratings) in place instead of failing setup.
    """
    console.print("\nRefreshing IMDb ratings...")
    try:
        count = imdb_ratings.refresh(config.IMDB_RATINGS_DB_PATH)
    except Exception as exc:
        console.print(f"  IMDb ratings: [yellow]not refreshed[/yellow] ({type(exc).__name__}: {exc}). "
                      "Ratings keep using the previous copy, or TMDB if there is none.")
        return False
    console.print(f"  IMDb ratings: [green]ok[/green] {count:,} titles")
    return True


def refresh_language_lists(only_existing: bool = False) -> bool:
    """Rebuild Find's IMDb-rated language lists from TMDB.

    With only_existing, rebuild just the lists already built once that are a
    day old, so a plain setup run does not start a first build nobody asked
    for. A failure warns and keeps the previous list.
    """
    if not config.TMDB_API_KEY:
        console.print("  Language lists: [yellow]skipped[/yellow] (TMDB_API_KEY not set)")
        return False
    ok = True
    tmdb = TmdbClient(api_key=config.TMDB_API_KEY, cache_dir=config.CACHE_DIR)
    for code, label in language_catalog.LANGUAGE_OPTIONS:
        saved = language_catalog.load(config.FIND_CACHE_DIR, code)
        if only_existing and (saved is None or not language_catalog.build_is_due(config.FIND_CACHE_DIR, code)):
            continue
        console.print(f"\nBuilding the {label} list for Find (the first build takes a few minutes)...")
        try:
            count = language_catalog.build(tmdb, code, config.IMDB_RATINGS_DB_PATH, config.FIND_CACHE_DIR)
        except Exception as exc:
            console.print(f"  {label} list: [yellow]not rebuilt[/yellow] ({type(exc).__name__}: {exc}). "
                          "Find keeps the previous list, if any.")
            ok = False
            continue
        console.print(f"  {label} list: [green]ok[/green] {count:,} IMDb-rated titles")
    return ok


def run_ingest_only() -> None:
    """Strict preflight validation of configured provider zips, persist to SQLite."""
    ingest_providers(fail_on_error=True)
    console.print("\n[green]All configured providers validated and persisted.[/green]")


def run_setup(refresh_profile: bool = False, refresh_data: bool = False, provider: str | None = None,
              profile_path: str | None = None, rethink_themes: bool = False) -> None:
    if rethink_themes:
        refresh_profile = True
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
        # Installs from before manual events were stored have none yet: import once.
        if not event_store.load_events(config.EVENT_DB_PATH, provider="manual"):
            _import_manual_events()
        events = event_store.load_events(config.EVENT_DB_PATH)

        if not events:
            console.print("[red]No persisted watch events found. "
                          "Run ./recommend setup or ./recommend setup --ingest-only first.[/red]")
            sys.exit(1)
        console.print(f"  Total: {len(events)} events (from SQLite)")

    else:
        # Default flow / refresh_data flow: parse zips, persist to SQLite, load back
        events = ingest_providers(fail_on_error=True)
        if not events:
            console.print("[red]No watch events found. Add manual titles or provider exports with qualifying watch activity.[/red]")
            sys.exit(1)

    archive_events = _drop_archive_duplicates(_archive_events(), events)
    if archive_events:
        console.print(f"  Plus {len(archive_events)} \"Seen it\" archive entries")
        events = events + archive_events

    enrichments_index_path = Path(config.ENRICHMENT_CACHE_DIR) / "index.json"
    metadata: dict = {}
    rejected_overrides: list[str] = []
    duplicate_override_keys: list[str] = []
    rematch_result: dict | None = None

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

    if not refresh_data and index_path.exists() and archive_events:
        # Archive entries added since the last build are not in the index, so they
        # have no metadata or enrichment yet.
        known = wi.load(config.WATCH_INDEX_PATH)
        if any(
            # Typed identity when the row has an ID; title only for rows without one.
            ((e.content_type, e.tmdb_id_hint) not in known.tmdb_keys) if e.tmdb_id_hint
            else ((wi._normalize(e.title), e.content_type) not in known.normalized_titles)
            for e in archive_events
        ):
            console.print("\n[yellow]New \"Seen it\" archive entries — triggering data + profile refresh.[/yellow]")
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
        duplicate_override_keys = ov.find_duplicate_keys(config.OVERRIDES_PATH)
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
        tmdb_id_hints = _build_tmdb_id_hints(events)

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
                        meta = _resolve_tmdb_id_override(
                            tmdb, title, ct, override["tmdb_id"], search_title=search_title,
                            trust=bool(override.get("trust")), rejected=rejected_overrides,
                        )
                        if meta:
                            metadata[(title, ct)] = meta
                        else:
                            hints = hints_map.get((title, ct))
                            meta = tmdb.get_metadata(search_title, ct, hints=hints)
                            if meta:
                                metadata[(title, ct)] = meta
                    else:
                        hints = hints_map.get((title, ct))
                        meta = tmdb.get_metadata(search_title, ct, hints=hints)
                        if meta:
                            metadata[(title, ct)] = meta
                else:
                    meta = None
                    hinted_id = tmdb_id_hints.get((title, ct))
                    if hinted_id:
                        # The source matched its own file to this ID (Plex's
                        # agent), so it is trusted like a verified override.
                        meta = _resolve_tmdb_id_override(tmdb, title, ct, hinted_id, trust=True)
                    if meta is None:
                        hints = hints_map.get((title, ct))
                        meta = tmdb.get_metadata(title, ct, hints=hints)
                    if meta:
                        metadata[(title, ct)] = meta
        console.print(f"  {len(metadata)} titles with TMDB metadata")
        if skipped:
            console.print(f"  {skipped} titles skipped via overrides")

        console.print("\nBuilding watch index...")
        index = wi.build(events, metadata)
        # Keep the old index and write the rematch report before anything later
        # in setup can fail; only the console summary waits for the end.
        backup_path, backup_error = _snapshot_previous_index(config.WATCH_INDEX_PATH)
        if backup_error:
            console.print(f"[red]Could not back up the existing watch index ({backup_error}). "
                          f"Left it unchanged; fix permissions and rerun setup.[/red]")
            sys.exit(1)
        wi.save(index, config.WATCH_INDEX_PATH)
        rematch_result = _build_rematch_report(backup_path, backup_error, index.entries)
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
        more_count = sum(1 for r in ratings if r["rating"] == user_store.RATING_MORE)
        less_count = sum(1 for r in ratings if r["rating"] == user_store.RATING_LESS)
        neutral_count = sum(1 for r in ratings if r["rating"] == user_store.RATING_NEUTRAL)
        if more_count or less_count or neutral_count:
            console.print(
                f"  Applying feedback: {more_count} more like this, "
                f"{less_count} less like this, {neutral_count} neutral"
            )

        scores = _profile_scores(
            events, metadata, index.entries, ratings,
            user_store.list_show_tracking(config.EVENT_DB_PATH),
        )
        film_ids = {e["title"]: e["tmdb_id"] for e in index.entries
                    if e.get("content_type") == "movie" and e.get("tmdb_id")}
        collections = collection_members(list(scores), film_ids, Path(config.CACHE_DIR))
        scores, enrichments = collapse_collections(scores, film_ids, enrichments, Path(config.CACHE_DIR))
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
                    loves = {t: s for t, s in scores.items() if s >= STRONG_WEIGHT}
                    structured_profile, warnings = build_tag_profile(
                        loves, enrichments, index.entries, collections, llm,
                        negative_prefs or None, rethink=rethink_themes,
                    )
                    for warning in warnings:
                        console.print(f"[yellow]{warning}[/yellow]")
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
        if structured_profile is None and structured_profile_skipped:
            # The home page and search read the structured profile, so the last good
            # one stays in place rather than falling back to the prose profile.
            console.print("[yellow]Previous structured taste profile kept.[/yellow]")
        if not using_custom_path:
            stale_flag = Path(config.PROFILE_STALE_FLAG)
            if stale_flag.exists():
                stale_flag.unlink()
    else:
        console.print("\nTaste profile exists, skipping (use --refresh-profile to rebuild).")

    if imdb_ratings.refresh_is_due(config.IMDB_RATINGS_DB_PATH):
        refresh_imdb_ratings()
    else:
        console.print("\nIMDb ratings are less than a day old, skipping (use --refresh-imdb to force).")
    refresh_language_lists(only_existing=True)

    _print_override_problems(rejected_overrides, duplicate_override_keys)
    if rematch_result:
        _print_rematch_summary(rematch_result)
    console.print("\n[green]Setup complete![/green]")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run offline setup for the recommender")
    parser.add_argument("--refresh-profile", action="store_true",
                        help="Rebuild taste profile even if it exists")
    parser.add_argument("--refresh-data", action="store_true",
                        help="Re-fetch TMDB metadata, watch index, and enrichments")
    parser.add_argument("--ingest-only", action="store_true",
                        help="Load and report on ingested data without TMDB or LLM calls")
    parser.add_argument("--refresh-imdb", action="store_true",
                        help="Only re-download IMDb ratings and rebuild Find's language lists, then exit")
    parser.add_argument("--debug", action="store_true",
                        help="Enable debug logging")
    parser.add_argument("--provider", choices=["anthropic", "gemini", "openai", "local"],
                        help="LLM provider (default: from config/env)")
    parser.add_argument("--profile-path", type=str, default=None,
                        help="Write taste profile to this path instead of the default")
    parser.add_argument("--rethink-themes", action="store_true",
                        help="Rebuild the taste themes from scratch, keeping theme ids where the taste "
                             "still exists. Also rebuilds the prose taste profile, which is a paid call")
    args = parser.parse_args()
    from recommender.log import setup_logging
    setup_logging(level_override="DEBUG" if args.debug else None)
    if args.refresh_imdb:
        ratings_ok = refresh_imdb_ratings()
        lists_ok = refresh_language_lists() if ratings_ok else False
        sys.exit(0 if ratings_ok and lists_ok else 1)
    elif args.ingest_only:
        run_ingest_only()
    else:
        run_setup(refresh_profile=args.refresh_profile, refresh_data=args.refresh_data,
                  provider=args.provider, profile_path=args.profile_path,
                  rethink_themes=args.rethink_themes)
