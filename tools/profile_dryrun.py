#!/usr/bin/env python3
"""Dry run of the structured taste profile, with no LLM call.

Usage:
    .venv/bin/python -m tools.profile_dryrun OUT_DIR
        Writes OUT_DIR/structured_prompt.txt (the exact prompt a rebuild would send)
        and OUT_DIR/input.txt (the numbered titles with score and source).
    .venv/bin/python -m tools.profile_dryrun OUT_DIR --apply RESPONSE.json
        Applies a stand-in model's answer the way a rebuild would and prints the
        cluster order the home page would show.

Run it on a local mirror (./recommend-mirror). It reads the same data a
`setup --refresh-profile` reads and writes only to OUT_DIR.
"""
import argparse
import json
from pathlib import Path

import config
from recommender import event_store, user_store
from recommender import watch_index as wi
from recommender.franchise import collapse_collections
from recommender.setup import (
    _archive_events, _drop_archive_duplicates, _profile_scores, _title_keyed_enrichments,
)
from recommender.structured_profile import parse_structured_profile_response, structured_prompt


def _load():
    events = event_store.load_events(config.EVENT_DB_PATH)
    events = events + _drop_archive_duplicates(_archive_events(), events)
    index = wi.load(config.WATCH_INDEX_PATH)
    raw = json.loads((Path(config.ENRICHMENT_CACHE_DIR) / "index.json").read_text())
    enrichments = _title_keyed_enrichments(raw, index.entries, {})
    scores = _profile_scores(
        events, {}, index.entries, user_store.load_ratings(config.EVENT_DB_PATH),
        user_store.list_show_tracking(config.EVENT_DB_PATH),
    )
    scores, enrichments = collapse_collections(
        scores,
        {e["title"]: e["tmdb_id"] for e in index.entries
         if e.get("content_type") == "movie" and e.get("tmdb_id")},
        enrichments, Path(config.CACHE_DIR),
    )
    sources = {e["title"]: ",".join(e.get("platforms") or []) for e in index.entries}
    prompt, scored = structured_prompt(
        events, scores, enrichments, user_store.get_disliked_titles(config.EVENT_DB_PATH))
    return prompt, scored, sources


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("out_dir")
    parser.add_argument("--apply", metavar="RESPONSE")
    args = parser.parse_args()
    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)

    prompt, scored, sources = _load()
    if args.apply:
        profile = parse_structured_profile_response(Path(args.apply).read_text(), scored)
        (out / "structured.json").write_text(json.dumps(profile, indent=2))
        for number, cluster in enumerate(profile["clusters"], start=1):
            print(f"{number:>2}. {cluster['label']}  (weight {cluster['weight']:.2f}, "
                  f"{cluster['co_viewing']}, {len(cluster['members'])} titles)")
        return

    (out / "structured_prompt.txt").write_text(prompt)
    (out / "input.txt").write_text("".join(
        f"{number}. {title}  score {score:.2f}  [{sources.get(title, 'franchise')}]\n"
        for number, (title, score) in enumerate(scored, start=1)))
    print(f"{len(scored)} titles, prompt {len(prompt):,} characters -> {out}")


if __name__ == "__main__":
    main()
