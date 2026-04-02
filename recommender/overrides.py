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
  "Some Known Show": {"tmdb_id": 12345, "content_type": "tv"}
}

Fields:
  title        — corrected title to search TMDB with
  tmdb_id      — direct TMDB ID (skips search entirely)
  content_type — override content type ("tv" or "movie")
  skip         — if true, ignore this title completely
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
    """Print unmatched titles and hint about the override system."""
    if not unmatched_titles:
        return

    print(f"\n  {len(unmatched_titles)} titles could not be matched on TMDB:")
    for e in unmatched_titles[:20]:
        print(f"    [{e.get('content_type', '?')}] {e['title']}")
    if len(unmatched_titles) > 20:
        print(f"    ... and {len(unmatched_titles) - 20} more")

    p = Path(overrides_path)
    if p.exists():
        print(f"\n  To fix these, add entries to {overrides_path}")
    else:
        print(f"\n  To fix these, create {overrides_path} with overrides:")
        print('    {')
        if unmatched_titles:
            t = unmatched_titles[0]['title']
            print(f'      "{t}": {{"title": "Corrected Title Here"}},')
            print(f'      "Title To Skip": {{"skip": true}}')
        print('    }')
