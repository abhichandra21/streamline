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

_LABELS = {
    "saved_titles": "watchlist",
    "title_ratings": "rating",
    "manual_archive_entries": "archive",
    "show_tracking": "tracking",
}

# The one column worth showing when a row changed rather than appeared.
_VALUE_COLUMN = {
    "saved_titles": "status",
    "title_ratings": "rating",
    "manual_archive_entries": "watched_at",
    "show_tracking": "state",
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
        """Whether this can be promoted. Server-only changes arrive via the refresh."""
        return self.state is not State.SERVER_ONLY

    @property
    def summary(self) -> str:
        """What local did, against the baseline, or against the server when unclassified."""
        reference = self.server if self.state is State.UNCLASSIFIED else self.baseline
        return _describe_delta(self.local, reference)

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

def _value_of(table: str, rows: tuple) -> str:
    if not rows:
        return ""
    value = rows[0].get(_VALUE_COLUMN[table])
    return str(value) if value is not None else ""


def _describe_delta(local: Footprint | None, reference: Footprint | None) -> str:
    """What local has that the reference did not, in the app's own vocabulary."""
    parts = []
    for table in USER_TABLES:
        mine = (local.rows.get(table, ()) if local else ())
        theirs = (reference.rows.get(table, ()) if reference else ())
        if mine == theirs:
            continue
        label = _LABELS[table]
        if mine and not theirs:
            parts.append(f"{label} +")
        elif theirs and not mine:
            parts.append(f"{label} -")
        else:
            before, after = _value_of(table, theirs), _value_of(table, mine)
            parts.append(f"{label} {before} -> {after}" if before != after else f"{label} changed")
    return ", ".join(parts) or "(no change)"


def _describe_values(footprint: Footprint | None) -> str:
    if footprint is None:
        return "absent"
    parts = [f"{_LABELS[t]} {_value_of(t, footprint.rows[t])}".strip()
             for t in USER_TABLES if footprint.rows.get(t)]
    return ", ".join(parts) or "absent"


# ── checklist ────────────────────────────────────────────────────────────────

_CHECKLIST_HEADER = """\
# Tick what should go to prod{host}.
# Save and quit to apply. Quit without saving to cancel.
# Unticked items are discarded when local refreshes.
"""


def _line_body(change: Change) -> str:
    body = f"{change.title}  {change.summary}"
    if change.state is State.CONFLICT:
        body += f"   PROD HAS: {change.server_summary}"
    elif change.state is State.UNCLASSIFIED:
        body += f"   PROD HAS: {change.server_summary}  (no baseline yet)"
    return body


def render_checklist(changes: list, host: str | None = None) -> str:
    offered = [c for c in changes if c.offered]
    lines = [_CHECKLIST_HEADER.format(host=f" ({host})" if host else ""), ""]
    lines += [f"[ ] {_line_body(c)}" for c in offered]
    return "\n".join(lines) + "\n"


def parse_checklist(text: str, changes: list) -> list:
    """Return the ticked changes, refusing anything but a change of checkbox state.

    Ticks map to changes by position, so a deleted or edited line would
    promote a different title than the one that was ticked. Comparing each
    line against what was written makes that a refusal instead.
    """
    offered = [c for c in changes if c.offered]
    expected = [_line_body(c) for c in offered]

    marks, bodies = [], []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if len(line) < 3 or line[0] != "[" or line[2] != "]":
            raise ChecklistEdited(f"Not a checklist line: {raw!r}")
        marks.append(line[1].strip().lower() == "x")
        bodies.append(line[3:].strip())

    if bodies != [e.strip() for e in expected]:
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
