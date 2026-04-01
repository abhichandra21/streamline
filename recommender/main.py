import argparse
import csv
import sys

import config
from recommender.engine import Recommendation, recommend
from recommender.ingestion.netflix import parse as parse_netflix
from recommender.ingestion.prime import parse as parse_prime
from recommender.signals import compute_scores
from recommender.taste_profile import build_profile
from recommender.tmdb_client import TmdbClient


def load_all_events():
    events = []
    parsers = {"netflix": parse_netflix, "prime": parse_prime}
    for platform, parser in parsers.items():
        path = config.PLATFORM_PATHS.get(platform)
        if path:
            events.extend(parser(path))
    return events


def print_results(results: dict[str, list[Recommendation]]) -> None:
    for label, recs in results.items():
        header = "TV SHOW RECOMMENDATIONS" if label == "tv" else "MOVIE RECOMMENDATIONS"
        print(f"\n{'═' * 62}")
        print(f"  {header}")
        print(f"{'═' * 62}")
        for i, rec in enumerate(recs, 1):
            genres = ", ".join(rec.genres[:3])
            print(
                f"  {i:2}. {rec.title:<36} Score: {rec.score:.2f}"
                f"  ★ {rec.vote_average:.1f}  [{genres}]"
            )
            if rec.because_you_watched:
                print(f"      Because you watched: {rec.because_you_watched}")


def save_csv(results: dict[str, list[Recommendation]], path: str) -> None:
    with open(path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["Rank", "Type", "Title", "Score", "TMDB Rating",
                          "Genres", "Because You Watched"])
        for label, recs in results.items():
            for i, rec in enumerate(recs, 1):
                writer.writerow([
                    i, label, rec.title, f"{rec.score:.3f}",
                    rec.vote_average, "; ".join(rec.genres), rec.because_you_watched,
                ])


def main():
    parser = argparse.ArgumentParser(description="Streaming recommendation system")
    parser.add_argument("--type", choices=["tv", "movies"], default=None)
    parser.add_argument("--top", type=int, default=config.DEFAULT_TOP_N)
    parser.add_argument("--save", type=str, default=None)
    parser.add_argument("--refresh-cache", action="store_true")
    args = parser.parse_args()

    if not config.TMDB_API_KEY:
        print("Error: TMDB_API_KEY not set.", file=sys.stderr)
        print("Get a free key at https://www.themoviedb.org/settings/api", file=sys.stderr)
        sys.exit(1)

    client = TmdbClient(api_key=config.TMDB_API_KEY, cache_dir=config.CACHE_DIR)
    if args.refresh_cache:
        client.clear_cache()
        print("Cache cleared.")

    print("Loading watch history...")
    events = load_all_events()
    print(f"  {len(events)} watch events loaded.")

    tv_series = {e.series_name for e in events if e.content_type == "tv"}
    movies_set = {e.title for e in events if e.content_type == "movie"}
    all_to_enrich = [(t, "tv") for t in tv_series] + [(t, "movie") for t in movies_set]

    print(f"Fetching TMDB metadata for {len(all_to_enrich)} watched titles...")
    watched_metadata = {}
    for i, (title, ct) in enumerate(all_to_enrich):
        meta = client.get_metadata(title, ct)
        if meta:
            watched_metadata[title] = meta
        if (i + 1) % 20 == 0:
            print(f"  {i+1}/{len(all_to_enrich)} enriched...")

    scores = compute_scores(events, watched_metadata, config.RECENCY_HALF_LIFE_DAYS)

    tv_scores = {k: v for k, v in scores.items() if k in tv_series}
    movie_scores = {k: v for k, v in scores.items() if k in movies_set}
    tv_watched_meta = {k: v for k, v in watched_metadata.items() if k in tv_series}
    movie_watched_meta = {k: v for k, v in watched_metadata.items() if k in movies_set}

    tv_profile = build_profile(tv_scores, tv_watched_meta)
    movie_profile = build_profile(movie_scores, movie_watched_meta)
    watched_titles = set(watched_metadata.keys())

    results = {}

    if args.type in ("tv", None):
        print("Fetching TV candidate pool...")
        tv_candidates = client.get_candidates("tv", size=config.CANDIDATE_POOL_SIZE)
        print(f"  {len(tv_candidates)} candidates.")
        results["tv"] = recommend(
            "tv", tv_profile, tv_candidates, watched_titles, tv_watched_meta,
            top_n=args.top, min_vote_count=config.MIN_VOTE_COUNT,
        )

    if args.type in ("movies", None):
        print("Fetching movie candidate pool...")
        movie_candidates = client.get_candidates("movie", size=config.CANDIDATE_POOL_SIZE)
        print(f"  {len(movie_candidates)} candidates.")
        results["movies"] = recommend(
            "movie", movie_profile, movie_candidates, watched_titles, movie_watched_meta,
            top_n=args.top, min_vote_count=config.MIN_VOTE_COUNT,
        )

    print_results(results)

    if args.save:
        save_csv(results, args.save)
        print(f"\nSaved to {args.save}")


if __name__ == "__main__":
    main()
