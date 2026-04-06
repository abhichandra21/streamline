import csv
import logging
import re
import tempfile
import zipfile
from datetime import datetime, timedelta
from pathlib import Path

from .base import WatchEvent

log = logging.getLogger("recommender.ingestion.prime")

MIN_WATCH_SECONDS = 20 * 60  # 20 minutes — filters abandoned/skimmed content

# Matches the season/series suffix at the end of a Prime title.
# Covers: " - Season 2", ", Season 1", " Season Two", " The Final Season", etc.
_SEASON_RE = re.compile(
    r'(?:\s*[,-]?\s+(?:Season|Series)\s+(?:\d+|\w+)|\s+The\s+Final\s+Season)\s*$',
    re.IGNORECASE,
)

_KEEP_MATERIAL = {'Feature', 'Full'}

_TARGET_CSV = 'Viewing History.csv'


def _classify(title: str) -> tuple[str, str]:
    """Return (content_type, series_name) for a Prime title.

    Strategy: strip the season suffix, then split on the last '-' to separate
    the optional episode title from the series name. Episode titles may contain
    hyphens themselves (e.g. "Mid-way to Mid-town"), so we always use the last
    '-' as the separator.
    """
    base = _SEASON_RE.sub('', title)
    if base == title:
        return 'movie', title
    if '-' in base:
        series_name = base.rsplit('-', 1)[1].strip().rstrip(',')
    else:
        series_name = base.strip().rstrip(',')
    return 'tv', series_name


def _clean(s: str) -> str:
    return s.strip().strip('"')


def _parse_csv(filepath: str) -> list[WatchEvent]:
    """Parse Prime Video Viewing History CSV into WatchEvents."""
    events = []
    with open(filepath, newline='', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        for row in reader:
            if _clean(row['Material Type Description']) not in _KEEP_MATERIAL:
                continue
            try:
                secs = float(row['Seconds Viewed'])
            except (ValueError, TypeError):
                continue
            if secs < MIN_WATCH_SECONDS:
                continue
            try:
                timestamp = datetime.strptime(
                    _clean(row['Playback Start Datetime (UTC)']), '%Y-%m-%dT%H:%M:%SZ'
                )
            except ValueError:
                continue
            title = _clean(row['Title'])
            content_type, series_name = _classify(title)
            events.append(WatchEvent(
                platform='prime',
                title=title,
                content_type=content_type,
                series_name=series_name,
                watched_duration=timedelta(seconds=secs),
                total_duration=None,
                timestamp=timestamp,
                profile=_clean(row['Profile Type']),
            ))
    return events


def parse(zip_path: str) -> list[WatchEvent]:
    """Parse Prime Video watch history from a data export zip.

    Args:
        zip_path: Path to the Prime Video data export .zip file.

    Raises:
        FileNotFoundError: If zip_path does not exist.
        ValueError: If the file is not a .zip or the expected CSV is missing.
    """
    p = Path(zip_path)
    if not p.exists():
        raise FileNotFoundError(f'Prime Video export not found: {zip_path}')
    if p.suffix != '.zip':
        raise ValueError(f'Prime Video path must be a .zip file, got: {zip_path}')

    with tempfile.TemporaryDirectory(prefix='prime_') as work_dir:
        try:
            with zipfile.ZipFile(zip_path, 'r') as zf:
                zf.extractall(work_dir)
        except zipfile.BadZipFile as exc:
            raise ValueError(f'Invalid zip file: {zip_path} ({exc})') from exc

        found = list(Path(work_dir).rglob(_TARGET_CSV))
        if not found:
            raise ValueError(f'{_TARGET_CSV} not found inside {zip_path}')

        log.info('Prime Video: reading %s', found[0])
        return _parse_csv(str(found[0]))
