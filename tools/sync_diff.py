"""Comparison logic for user-state sync: title footprints, three-way classification, checklist.

Pure functions over plain dicts. No files, no ssh, no database, so the logic
that decides what reaches the live app is testable on its own.
See docs/superpowers/specs/2026-09-14-user-state-sync-design.md.

The unit throughout is a title, not a row, for two reasons. A single user
action can write several tables at once: user_store.mark_watched_from_watchlist
removes a watchlist row, inserts an archive row and may insert a rating, all in
one transaction, so approving rows independently could apply half of it.
And user_store._reconcile_identity promotes a null-tmdb_id row to a concrete
id, which changes a row's identity while leaving the title alone; grouping by
title makes that promotion one change rather than a removal plus an addition.
"""

import hashlib
import json
from dataclasses import dataclass
from enum import Enum

# The sync's notion of a title has to be the application's notion, or the two
# would disagree about what is the same title. _reconcile_identity uses this
# same rule, and a local copy of the regex would drift from it.
from recommender.user_store import _normalize as normalize_title

USER_TABLES = ("saved_titles", "title_ratings", "manual_archive_entries", "show_tracking")
HISTORY_TABLE = "query_history"

# Columns recording only when something happened. A difference confined to
# these is not a change. watched_at is deliberately absent: it orders the
# archive, so a rewatch is real.
BOOKKEEPING_COLUMNS = {
    "saved_titles": ("saved_at", "updated_at"),
    "title_ratings": ("rated_at", "updated_at"),
    "manual_archive_entries": (),
    "show_tracking": ("created_at", "updated_at"),
}

class ChecklistEdited(Exception):
    """The checklist came back structurally different from the one written."""


class State(Enum):
    SERVER_ONLY = "server_only"
    LOCAL_ONLY = "local_only"
    CONFLICT = "conflict"
    UNCLASSIFIED = "unclassified"


@dataclass(frozen=True)
class Footprint:
    """One title's rows across the four user tables."""

    key: tuple
    title: str
    rows: dict  # table -> tuple of comparable rows, canonically ordered


@dataclass(frozen=True)
class Change:
    key: tuple
    title: str
    state: State
    local: Footprint | None
    server: Footprint | None
    baseline: Footprint | None

    @property
    def offered(self) -> bool:
        """Whether this can be promoted.

        Server-only changes arrive via the refresh, so there is nothing to
        promote. And with no baseline, a title absent from this machine
        entirely is withheld: "local does not have this title" cannot be
        told apart from "local never had this title", and the second is far
        likelier. Putting a destructive checkbox behind an unverifiable guess
        is how a first run deletes real data. Local additions and value
        changes are still offered, which is the point of the first run.
        """
        if self.state is State.SERVER_ONLY:
            return False
        if self.state is State.UNCLASSIFIED and self.removes_title:
            return False
        return True

    @property
    def removes_title(self) -> bool:
        """Whether promoting this deletes a title from the server outright.

        Only a title with no local footprint at all counts. A title local does
        have but changed -- marking something watched drops its watchlist row
        -- is an ordinary update to a title the operator demonstrably acted on,
        and the line says so. Treating those as deletions too would put a typed
        confirmation in front of the most common action there is, which trains
        the reflex it is supposed to interrupt.
        """
        return self.local is None and self.server is not None

    @property
    def incoming(self) -> str:
        """What the refresh brings to this machine, for a change that is not promotable."""
        if self.server is None:
            return "removed locally"
        if self.local is None:
            return f"arrives locally: {_describe_values(self.server)}"
        return f"becomes {_describe_values(self.server)} locally"

    @property
    def action(self) -> str:
        """What promoting this does to the server.

        Phrased as the effect on prod, not as what local did, because the two
        read the same on a first run and only one of them is safe to act on: a
        title present only on the server looks like "local removed it" whether
        local removed it or never had it, and ticking it deletes real data.
        """
        return _describe_action(self.local, self.server)

    @property
    def note(self) -> str:
        if self.state is State.CONFLICT:
            return f"conflict, {self._prod_state}"
        if self.state is State.UNCLASSIFIED:
            return f"no baseline, {self._prod_state}"
        return ""

    @property
    def _prod_state(self) -> str:
        if self.server is None:
            return "not on prod"
        return f"prod has {_describe_values(self.server)}"

    @property
    def server_summary(self) -> str:
        return _describe_values(self.server)


# ── footprints ───────────────────────────────────────────────────────────────

def _canonical(row: dict) -> str:
    return json.dumps(row, sort_keys=True, default=str)


def _comparable(table: str, row: dict) -> dict:
    """The row without its id or its bookkeeping timestamps."""
    ignore = {"id", *BOOKKEEPING_COLUMNS.get(table, ())}
    return {k: v for k, v in row.items() if k not in ignore}


def _title_key(table: str, row: dict) -> tuple:
    """(content_type, normalized_title), the application's notion of one title.

    show_tracking carries neither column: it is keyed by tmdb_id alone and
    only ever applies to series, so both are derived.
    """
    if table == "show_tracking":
        return ("tv", normalize_title(row.get("title") or ""))
    normalized = row.get("normalized_title") or normalize_title(row.get("title") or "")
    return (row.get("content_type") or "", normalized)


def build_footprints(snapshot: dict) -> dict:
    """Group a snapshot's rows into one footprint per title.

    Rows are kept as tuples rather than single values because the partial
    unique indexes permit two rows for one title in the same table, one with a
    tmdb_id and one without. Keeping both means the comparison sees the
    duplicate instead of silently dropping half of it.
    """
    groups: dict = {}
    for table in USER_TABLES:
        for row in snapshot.get(table) or []:
            key = _title_key(table, row)
            group = groups.setdefault(key, {"title": None, "rows": {}})
            group["rows"].setdefault(table, []).append(_comparable(table, row))
            if group["title"] is None:
                group["title"] = row.get("title")

    out = {}
    for key, group in groups.items():
        rows = {t: tuple(sorted(r, key=_canonical)) for t, r in group["rows"].items()}
        out[key] = Footprint(key=key, title=group["title"] or key[1], rows=rows)
    return out


def _same(a: Footprint | None, b: Footprint | None) -> bool:
    if a is None or b is None:
        return a is None and b is None
    return a.rows == b.rows


# ── classification ───────────────────────────────────────────────────────────

def classify(baseline: dict | None, local: dict, server: dict) -> list:
    """Compare three snapshots and return every title that is not already in sync.

    Comparing local against the server, and not only against the baseline, is
    what surfaces a title both sides changed to different values. The
    server-side staleness check cannot stand in for this: it only sees changes
    made after the server snapshot was taken.
    """
    base_fps = build_footprints(baseline) if baseline is not None else None
    local_fps = build_footprints(local)
    server_fps = build_footprints(server)

    keys = set(local_fps) | set(server_fps) | set(base_fps or {})
    changes = []
    for key in keys:
        here, there = local_fps.get(key), server_fps.get(key)
        if _same(here, there):
            continue

        if base_fps is None:
            state, was = State.UNCLASSIFIED, None
        else:
            was = base_fps.get(key)
            local_moved, server_moved = not _same(here, was), not _same(there, was)
            if local_moved and server_moved:
                state = State.CONFLICT
            elif local_moved:
                state = State.LOCAL_ONLY
            else:
                state = State.SERVER_ONLY

        display = here or there or was
        changes.append(Change(key=key, title=display.title, state=state,
                              local=here, server=there, baseline=was))

    return sorted(changes, key=lambda c: (c.title.lower(), c.key))


# ── describing a change in words ─────────────────────────────────────────────

_ABSENT = "absent"


def _phrase(table: str, rows: tuple) -> str:
    """One table's state for a title, in the vocabulary the app's own UI uses."""
    if not rows:
        return ""
    row = rows[0]
    if table == "saved_titles":
        return str(row.get("status") or "watchlist")
    if table == "title_ratings":
        return f"rating {row.get('rating')}"
    if table == "manual_archive_entries":
        return "watched"
    # Caught-up progress is the field that actually moves for a followed show;
    # state alone would report "tracking following" on both sides of a change.
    phrase = f"tracking {row.get('state')}"
    season, episode = row.get("caught_up_season"), row.get("caught_up_episode")
    if season and episode:
        phrase += f", caught up S{season}E{episode}"
    return phrase


def _collapse(before: str, after: str) -> str:
    """'rating more' -> 'rating less' reads better as 'rating more -> less'."""
    b, a = before.split(), after.split()
    if len(b) == len(a) == 2 and b[0] == a[0]:
        return f"{b[0]} {b[1]} -> {a[1]}"
    return f"{before} -> {after}"


def _describe_values(footprint) -> str:
    if footprint is None:
        return _ABSENT
    parts = [_phrase(t, footprint.rows[t]) for t in USER_TABLES if footprint.rows.get(t)]
    return ", ".join(p for p in parts if p) or _ABSENT


def _describe_action(local, server) -> str:
    """What promoting this title does to the server: make it match local."""
    if local is None:
        return "REMOVE from prod"
    if server is None:
        return f"add to prod: {_describe_values(local)}"

    parts = []
    for table in USER_TABLES:
        mine, theirs = local.rows.get(table, ()), server.rows.get(table, ())
        if mine == theirs:
            continue
        if mine and not theirs:
            parts.append(f"+{_phrase(table, mine)}")
        elif theirs and not mine:
            parts.append(f"-{_phrase(table, theirs)}")
        else:
            parts.append(_collapse(_phrase(table, theirs), _phrase(table, mine)))
    return ", ".join(parts) or "no change"


# ── checklist ────────────────────────────────────────────────────────────────

_CHECKLIST_HEADER = """\
# Tick a line to make prod{host} match THIS MACHINE for that title.
# Unticked lines are left alone on prod, and discarded here on refresh.
# Save and quit to apply. Quit without saving to cancel.
"""


def _bodies(offered: list) -> list:
    """The text of each line, shared by rendering and parsing so they agree."""
    width = max((len(c.title) for c in offered), default=0)
    return [
        f"{c.title.ljust(width)}  {c.action}" + (f"   ({c.note})" if c.note else "")
        for c in offered
    ]


def render_checklist(changes: list, host: str | None = None) -> str:
    offered = [c for c in changes if c.offered]
    header = _CHECKLIST_HEADER.format(host=f" ({host})" if host else "")
    lines = [header.rstrip(), ""]
    lines += [f"[ ] {body}" for body in _bodies(offered)]
    return "\n".join(lines) + "\n"


def parse_checklist(text: str, changes: list) -> list:
    """Return the ticked changes, refusing anything but a change of checkbox state.

    Ticks map to changes by position, so a deleted or edited line would
    promote a different title than the one that was ticked. Comparing each
    line against what was written makes that a refusal instead.
    """
    offered = [c for c in changes if c.offered]
    expected = [b.strip() for b in _bodies(offered)]

    marks, bodies = [], []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if len(line) < 3 or line[0] != "[" or line[2] != "]":
            raise ChecklistEdited(f"Not a checklist line: {raw!r}")
        marks.append(line[1].strip().lower() == "x")
        bodies.append(line[3:].strip())

    if bodies != expected:
        raise ChecklistEdited(
            "The checklist was edited beyond its checkboxes, so a tick can no longer "
            "be matched to a change. Nothing was written. Rerun and tick again."
        )
    return [change for ticked, change in zip(marks, offered) if ticked]


# ── fingerprint ──────────────────────────────────────────────────────────────

def fingerprint(snapshot: dict) -> str:
    """Hash of the promotable tables, ignoring row order, ids and bookkeeping time.

    Used to notice local state moving while the checklist is open. History is
    excluded: it is downward-only, so a search landing mid-run is not a reason
    to abort.
    """
    payload = {}
    for table in USER_TABLES:
        rows = (_comparable(table, r) for r in snapshot.get(table) or [])
        payload[table] = sorted(_canonical(r) for r in rows)
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
