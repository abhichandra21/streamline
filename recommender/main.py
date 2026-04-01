import argparse
import csv
import sys

import config
from recommender.ingestion.netflix import parse as parse_netflix
from recommender.ingestion.prime import parse as parse_prime
from recommender.tmdb_client import TmdbClient


def load_all_events():
    events = []
    parsers = {"netflix": parse_netflix, "prime": parse_prime}
    for platform, parser in parsers.items():
        path = config.PLATFORM_PATHS.get(platform)
        if path:
            events.extend(parser(path))
    return events


def print_results(results):
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


def save_csv(results, path: str) -> None:
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
        sys.exit(1)

    client = TmdbClient(api_key=config.TMDB_API_KEY, cache_dir=config.CACHE_DIR)

    print("Loading watch history...")
    events = load_all_events()


if __name__ == "__main__":
    main()
