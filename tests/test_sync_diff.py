"""Pure comparison logic for user-state sync: footprints, classification, checklist."""

import pytest

from tools.sync_diff import (
    ChecklistEdited,
    State,
    build_footprints,
    classify,
    fingerprint,
    parse_checklist,
    render_checklist,
)


# ── snapshot builders ────────────────────────────────────────────────────────

def saved(title, ct="tv", tmdb=None, status="watchlist", at="t1"):
    return {"id": 1, "title": title, "normalized_title": title.lower(),
            "content_type": ct, "tmdb_id": tmdb, "status": status,
            "saved_at": at, "updated_at": at}


def rating(title, ct="tv", tmdb=None, value="more", at="t1"):
    return {"id": 1, "title": title, "normalized_title": title.lower(),
            "content_type": ct, "tmdb_id": tmdb, "rating": value,
            "rated_at": at, "updated_at": at}


def archive(title, ct="tv", tmdb=None, watched_at="2026-09-01", source="web"):
    return {"id": 1, "title": title, "normalized_title": title.lower(),
            "content_type": ct, "tmdb_id": tmdb, "watched_at": watched_at,
            "source": source}


def tracking(title, tmdb=1, state="following", at="t1", season=None, episode=None):
    return {"tmdb_id": tmdb, "title": title, "state": state,
            "tracking_from_season": None, "caught_up_season": season,
            "caught_up_episode": episode, "created_at": at, "updated_at": at}


def snap(saved_titles=(), title_ratings=(), manual_archive_entries=(),
         show_tracking=(), query_history=()):
    return {
        "saved_titles": list(saved_titles),
        "title_ratings": list(title_ratings),
        "manual_archive_entries": list(manual_archive_entries),
        "show_tracking": list(show_tracking),
        "query_history": list(query_history),
    }


def only(changes, state):
    return [c for c in changes if c.state is state]


# ── footprints ───────────────────────────────────────────────────────────────

def test_rows_across_tables_group_into_one_footprint():
    """One title touched in several tables is one unit of approval, not several."""
    fps = build_footprints(snap(
        saved_titles=[saved("Fargo")],
        title_ratings=[rating("Fargo")],
        manual_archive_entries=[archive("Fargo")],
    ))
    assert len(fps) == 1
    fp = next(iter(fps.values()))
    assert fp.key == ("tv", "fargo")
    assert set(fp.rows) == {"saved_titles", "title_ratings", "manual_archive_entries"}


def test_show_tracking_groups_with_the_same_title():
    """show_tracking has no content_type or normalized_title, so it is derived."""
    fps = build_footprints(snap(
        saved_titles=[saved("Andor", ct="tv")],
        show_tracking=[tracking("Andor")],
    ))
    assert len(fps) == 1
    assert set(next(iter(fps.values())).rows) == {"saved_titles", "show_tracking"}


def test_same_title_different_content_type_stays_separate():
    fps = build_footprints(snap(saved_titles=[saved("Fargo", ct="tv"),
                                              saved("Fargo", ct="movie")]))
    assert set(fps) == {("tv", "fargo"), ("movie", "fargo")}


def test_history_is_not_part_of_any_footprint():
    """History moves downward only, so it never appears as a promotable change."""
    fps = build_footprints(snap(query_history=[{"id": 1, "timestamp": "t", "entry": "{}"}]))
    assert fps == {}


# ── classification ───────────────────────────────────────────────────────────

def test_identical_sides_produce_nothing():
    base = snap(saved_titles=[saved("Fargo")])
    assert classify(base, base, base) == []


def test_server_only_change_is_not_offered_upward():
    """The refresh brings it down; there is nothing to promote."""
    base = snap()
    server = snap(saved_titles=[saved("Dune", ct="movie")])
    changes = classify(base, snap(), server)
    assert [c.state for c in changes] == [State.SERVER_ONLY]
    assert not changes[0].offered


def test_local_only_change_is_offered():
    changes = classify(snap(), snap(saved_titles=[saved("Fargo")]), snap())
    assert [c.state for c in changes] == [State.LOCAL_ONLY]
    assert changes[0].offered


def test_local_removal_is_offered_as_an_explicit_removal_from_prod():
    """Phrased as the effect on prod, since ticking it deletes live data."""
    base = snap(saved_titles=[saved("Fargo")])
    changes = classify(base, snap(), base)
    assert changes[0].state is State.LOCAL_ONLY
    assert changes[0].action == "REMOVE from prod"


def test_both_sides_changed_same_title_is_a_conflict_showing_the_server_value():
    """The case the first revision got wrong: this must not look like a plain local edit."""
    base = snap(title_ratings=[rating("Andor", value="neutral")])
    local = snap(title_ratings=[rating("Andor", value="less")])
    server = snap(title_ratings=[rating("Andor", value="more")])
    changes = classify(base, local, server)
    assert [c.state for c in changes] == [State.CONFLICT]
    assert changes[0].offered
    assert "more" in changes[0].server_summary


def test_no_baseline_offers_differences_as_unclassified():
    """First run must not discard the keepers this tool exists to rescue."""
    local = snap(saved_titles=[saved("Fargo")])
    server = snap(saved_titles=[saved("Dune", ct="movie")])
    changes = classify(None, local, server)
    assert {c.state for c in changes} == {State.UNCLASSIFIED}
    # Dune is only on prod, so it is reported but not promotable.
    assert [c.title for c in changes if c.offered] == ["Fargo"]


def test_a_title_only_on_the_server_is_not_offered_without_a_baseline():
    """The case that deleted real data on the first live run.

    With no baseline, "local deleted it" and "local never had it" cannot be
    told apart, and the second is far likelier. Offering the deletion reading
    as a checkbox puts a destructive action behind an unverifiable guess, so
    it is withheld and reported as something arriving here instead.
    """
    server = snap(saved_titles=[saved("Dune", ct="movie")])
    changes = classify(None, snap(), server)
    assert changes[0].removes_title
    assert not changes[0].offered
    assert changes[0].incoming == "arrives locally: watchlist"
    assert "Dune" not in render_checklist(changes)


def test_a_first_run_still_offers_local_additions():
    """Withholding removals must not withhold the keepers this exists to rescue."""
    changes = classify(None, snap(saved_titles=[saved("Fargo")]), snap())
    assert changes[0].offered
    assert changes[0].action == "add to prod: watchlist"


def test_a_first_run_still_offers_a_value_change():
    changes = classify(None,
                       snap(title_ratings=[rating("Andor", tmdb=9, value="less")]),
                       snap(title_ratings=[rating("Andor", tmdb=9, value="more")]))
    assert changes[0].offered
    assert changes[0].action == "rating more -> less"


def test_marking_watched_is_not_treated_as_a_title_removal():
    """It drops a watchlist row, but the title is one local demonstrably acted on."""
    local = snap(manual_archive_entries=[archive("Sinners", ct="movie", tmdb=7)])
    server = snap(saved_titles=[saved("Sinners", ct="movie", tmdb=7)])
    changes = classify(None, local, server)
    assert not changes[0].removes_title
    assert changes[0].offered


def test_a_title_absent_from_prod_says_so_rather_than_saying_absent():
    changes = classify(None, snap(saved_titles=[saved("Fargo")]), snap())
    assert changes[0].note == "no baseline, not on prod"


def test_marking_watched_reads_as_one_line_with_both_halves():
    """One action across two tables, one checkbox."""
    base = snap(saved_titles=[saved("Sinners", ct="movie", tmdb=7)])
    local = snap(manual_archive_entries=[archive("Sinners", ct="movie", tmdb=7)])
    changes = classify(base, local, base)
    assert changes[0].action == "-watchlist, +watched"


def test_no_baseline_does_not_offer_titles_that_already_match():
    both = snap(saved_titles=[saved("Fargo")])
    assert classify(None, both, both) == []


# ── what counts as a change ──────────────────────────────────────────────────

def test_bookkeeping_timestamps_alone_are_not_a_change():
    base = snap(saved_titles=[saved("Fargo", at="t1")])
    local = snap(saved_titles=[saved("Fargo", at="t9")])
    assert classify(base, local, base) == []


def test_watched_at_is_a_real_change():
    """It orders the archive, so a rewatch matters."""
    base = snap(manual_archive_entries=[archive("Fargo", watched_at="2026-01-01")])
    local = snap(manual_archive_entries=[archive("Fargo", watched_at="2026-09-01")])
    changes = classify(base, local, base)
    assert [c.state for c in changes] == [State.LOCAL_ONLY]


def test_row_ids_are_never_compared():
    base = snap(saved_titles=[{**saved("Fargo"), "id": 1}])
    local = snap(saved_titles=[{**saved("Fargo"), "id": 47}])
    assert classify(base, local, base) == []


def test_gaining_a_tmdb_id_is_one_changed_title_not_a_removal_plus_addition():
    """_reconcile_identity promotes a null-tmdb row; that must not split the title."""
    base = snap(saved_titles=[saved("Fargo", tmdb=None)])
    local = snap(saved_titles=[saved("Fargo", tmdb=12345)])
    changes = classify(base, local, base)
    assert len(changes) == 1
    assert changes[0].state is State.LOCAL_ONLY
    assert changes[0].key == ("tv", "fargo")


# ── checklist ────────────────────────────────────────────────────────────────

def test_checklist_round_trip_returns_only_ticked_titles():
    changes = classify(
        snap(),
        snap(saved_titles=[saved("Fargo"), saved("Test Movie", ct="movie")]),
        snap(),
    )
    text = render_checklist(changes)
    ticked = text.replace("[ ] Fargo", "[x] Fargo", 1)
    assert [c.title for c in parse_checklist(ticked, changes)] == ["Fargo"]


def test_checklist_arrives_with_every_box_empty():
    changes = classify(snap(), snap(saved_titles=[saved("Fargo")]), snap())
    text = render_checklist(changes)
    assert "[ ]" in text and "[x]" not in text
    assert parse_checklist(text, changes) == []


def test_checklist_shows_the_server_value_on_a_conflict():
    changes = classify(
        snap(title_ratings=[rating("Andor", value="neutral")]),
        snap(title_ratings=[rating("Andor", value="less")]),
        snap(title_ratings=[rating("Andor", value="more")]),
    )
    assert "more" in render_checklist(changes)


def test_checklist_only_lists_offered_changes():
    changes = classify(snap(), snap(), snap(saved_titles=[saved("Dune", ct="movie")]))
    assert "Dune" not in render_checklist(changes)


@pytest.mark.parametrize("mangle", [
    lambda t: t.replace("Fargo", "Fargoo"),
    lambda t: "\n".join(l for l in t.splitlines() if "Fargo" not in l),
    lambda t: t + "\n[x] Injected  watchlist +",
])
def test_an_edited_checklist_is_refused(mangle):
    """Ticks map to changes by line, so a structural edit could promote the wrong title."""
    changes = classify(
        snap(),
        snap(saved_titles=[saved("Fargo"), saved("Sinners", ct="movie")]),
        snap(),
    )
    with pytest.raises(ChecklistEdited):
        parse_checklist(mangle(render_checklist(changes)), changes)


def test_comments_and_blank_lines_are_ignored():
    changes = classify(snap(), snap(saved_titles=[saved("Fargo")]), snap())
    text = render_checklist(changes) + "\n\n# a trailing note\n"
    assert parse_checklist(text, changes) == []


# ── fingerprint ──────────────────────────────────────────────────────────────

def test_fingerprint_changes_when_a_promotable_table_changes():
    a = fingerprint(snap(saved_titles=[saved("Fargo")]))
    b = fingerprint(snap(saved_titles=[saved("Fargo"), saved("Dune", ct="movie")]))
    assert a != b


def test_fingerprint_ignores_bookkeeping_and_history():
    """Otherwise every run would abort on a timestamp or a search nobody cares about."""
    a = fingerprint(snap(saved_titles=[saved("Fargo", at="t1")]))
    b = fingerprint(snap(saved_titles=[saved("Fargo", at="t9")],
                         query_history=[{"id": 1, "timestamp": "t", "entry": "{}"}]))
    assert a == b


def test_fingerprint_is_row_order_independent():
    rows = [saved("Fargo"), saved("Dune", ct="movie")]
    assert fingerprint(snap(saved_titles=rows)) == fingerprint(snap(saved_titles=rows[::-1]))


def test_caught_up_progress_is_named_when_it_changes():
    """state stays "following" on both sides, so reporting only state says nothing."""
    base = snap(show_tracking=[tracking("The Bear", tmdb=136315)])
    local = snap(show_tracking=[tracking("The Bear", tmdb=136315, season=5, episode=8)])
    changes = classify(base, local, base)
    assert "caught up S5E8" in changes[0].action


def test_a_tracked_show_arriving_locally_names_its_progress():
    base = snap(show_tracking=[tracking("The Bear", tmdb=136315)])
    server = snap(show_tracking=[tracking("The Bear", tmdb=136315, season=5, episode=8)])
    changes = classify(base, base, server)
    assert not changes[0].offered
    assert changes[0].incoming == "becomes tracking following, caught up S5E8 locally"


def test_a_show_with_no_progress_yet_reads_plainly():
    changes = classify(None, snap(show_tracking=[tracking("Andor", tmdb=9)]), snap())
    assert changes[0].action == "add to prod: tracking following"
