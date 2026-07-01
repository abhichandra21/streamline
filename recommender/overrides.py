"""Title override system for unmatched or mismatched TMDB lookups.

The override file (data/overrides.json) maps raw titles from streaming
platform exports to corrected information. This runs before TMDB search
and can fix typos, provide correct TMDB IDs, or skip titles entirely.

Format:
{
  "The Matrix III Revolutions": {"title": "The Matrix Revolutions"},
  "21 REPACK": {"title": "21"},
  "Gabriel lglesias: ...": {"title": "Gabriel Iglesias: ..."},
  "Hindu Weddings: Traditions Unveiled": {"skip": true},
  "Some Known Show": {"tmdb_id": 12345, "content_type": "tv"},
  "LOTR: Fellowship": {"tmdb_id": 120, "trust": true}
}

Fields:
  title        — corrected title to search TMDB with
  tmdb_id      — direct TMDB ID (skips search entirely)
  content_type — override content type ("tv" or "movie")
  skip         — if true, ignore this title completely
  trust        — if true, skip the tmdb_id plausibility check (see
                 setup._resolve_tmdb_id_override). Use this when the source
                 title is a deliberate abbreviation or otherwise looks
                 nothing like the real title (e.g. "LOTR: Fellowship" -> The
                 Lord of the Rings: The Fellowship of the Ring) -- the
                 default validation exists to catch accidental/bogus IDs and
                 will reject a legitimate but very different-looking title.
"""

import json
import logging
from pathlib import Path

log = logging.getLogger("recommender.overrides")


def load(path: str) -> dict[str, dict]:
    """Load overrides from JSON file. Returns empty dict if file missing."""
    p = Path(path)
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text())
        log.debug("Loaded %d overrides from %s", len(data), path)
        return data
    except (json.JSONDecodeError, ValueError) as exc:
        log.warning("Failed to load overrides from %s: %s", path, exc)
        return {}


def report_unmatched(
    unmatched_titles: list[dict],
    overrides_path: str,
) -> None:
    """Log unmatched titles and hint about the override system."""
    if not unmatched_titles:
        return

    sample = ", ".join(
        f"[{e.get('content_type', '?')}] {e['title']}" for e in unmatched_titles[:20]
    )
    tail = f" ... and {len(unmatched_titles) - 20} more" if len(unmatched_titles) > 20 else ""
    log.warning("%d titles could not be matched on TMDB: %s%s", len(unmatched_titles), sample, tail)

    p = Path(overrides_path)
    if p.exists():
        log.info("To fix unmatched titles, add entries to %s", overrides_path)
    else:
        first = unmatched_titles[0]["title"] if unmatched_titles else "Some Title"
        log.info(
            'To fix unmatched titles, create %s with entries like: '
            '{"%s": {"title": "Corrected Title Here"}, "Title To Skip": {"skip": true}}',
            overrides_path, first,
        )
