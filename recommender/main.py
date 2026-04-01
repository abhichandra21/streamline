import logging
import sys
from pathlib import Path

import anthropic
from rich.console import Console
from rich.panel import Panel

import config
from recommender import feedback as fb
from recommender.ingestion.netflix import parse as parse_netflix
from recommender.ingestion.prime import parse as parse_prime
from recommender.ingestion.manual import parse as parse_manual
from recommender.models import Recommendation
from recommender.tmdb_client import TmdbClient
from recommender.query_engine import RecommendContext, ask
from recommender import watch_index as wi

log = logging.getLogger("recommender")

console_err = Console(stderr=True)   # spinners, progress, warnings
console_out = Console()              # recommendation results


def load_context() -> RecommendContext:
    events = []
    for platform, parser in [("netflix", parse_netflix), ("prime", parse_prime)]:
        path = config.PLATFORM_PATHS.get(platform)
        if path:
            events.extend(parser(path))
    if config.MANUAL_TV_PATH and config.MANUAL_MOVIES_PATH:
        events.extend(parse_manual(config.MANUAL_TV_PATH, config.MANUAL_MOVIES_PATH))

    index_path = Path(config.WATCH_INDEX_PATH)
    if not index_path.exists():
        console_err.print("[red]Watch index not found. Run: python -m recommender.setup[/red]")
        sys.exit(1)

    profile_path = Path(config.TASTE_PROFILE_PATH)
    if not profile_path.exists():
        console_err.print("[red]Taste profile not found. Run: python -m recommender.setup[/red]")
        sys.exit(1)

    return RecommendContext(
        taste_profile=profile_path.read_text(),
        watch_index=wi.load(config.WATCH_INDEX_PATH),
        events=events,
        tmdb_client=TmdbClient(api_key=config.TMDB_API_KEY, cache_dir=config.CACHE_DIR),
        anthropic_client=anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY),
        cache_dir=config.ENRICHMENT_CACHE_DIR,
        providers_cache_dir=config.PROVIDERS_CACHE_DIR,
        watch_region=config.WATCH_REGION,
        streaming_platforms=list(config.STREAMING_PLATFORMS),
    )


def print_recommendations(results: list[Recommendation], query: str) -> None:
    if not results:
        console_out.print("[yellow]No recommendations found.[/yellow]")
        return
    console_out.print(f'\n[bold]Results for:[/bold] "{query}"\n')
    for i, rec in enumerate(results, 1):
        genres_str = ", ".join(rec.genres[:3])
        title_line = f"{rec.title}  ★ {rec.vote_average:.1f}  [{genres_str}]"
        body = rec.explanation
        if rec.streaming_providers:
            body += f"\n\n[dim]Available on: {', '.join(rec.streaming_providers[:4])}[/dim]"
        panel = Panel(
            body,
            title=f"[bold]{i}. {title_line}[/bold]",
            border_style="green",
            padding=(0, 2),
        )
        console_out.print(panel)


def _handle_feedback_command(line: str) -> bool:
    """Handle interactive feedback commands. Returns True if line was a feedback command."""
    # +liked <title> / +loved <title>
    for prefix in ("+liked ", "+loved ", "+like "):
        if line.lower().startswith(prefix):
            title = line[len(prefix):].strip()
            data = fb.load(config.FEEDBACK_PATH)
            fb.add_rating(data, title, "liked")
            fb.save(data, config.FEEDBACK_PATH)
            console_out.print(f"[green]Marked as liked:[/green] {title}")
            console_out.print("[dim]Run --refresh-profile to update your taste profile.[/dim]")
            return True

    # -disliked <title> / +disliked <title>
    for prefix in ("-disliked ", "+disliked ", "-dislike ", "+dislike "):
        if line.lower().startswith(prefix):
            title = line[len(prefix):].strip()
            data = fb.load(config.FEEDBACK_PATH)
            fb.add_rating(data, title, "disliked")
            fb.save(data, config.FEEDBACK_PATH)
            console_out.print(f"[yellow]Marked as disliked:[/yellow] {title}")
            console_out.print("[dim]Run --refresh-profile to update your taste profile.[/dim]")
            return True

    # +add <title> [tv|movie]
    if line.lower().startswith("+add "):
        rest = line[5:].strip()
        parts = rest.rsplit(" ", 1)
        if len(parts) == 2 and parts[1].lower() in ("tv", "movie"):
            title, ct = parts[0].strip(), parts[1].lower()
        else:
            title, ct = rest, "tv"
        data = fb.load(config.FEEDBACK_PATH)
        fb.add_addition(data, title, ct)
        fb.save(data, config.FEEDBACK_PATH)
        console_out.print(f"[green]Added to watch history:[/green] {title} ({ct})")
        console_out.print("[dim]Run --refresh-profile to update your taste profile.[/dim]")
        return True

    return False


def main() -> None:
    import argparse
    parser = argparse.ArgumentParser(description="Streaming Recommender")
    parser.add_argument("query", nargs="*", help="Recommendation query")
    parser.add_argument("--debug", action="store_true", help="Enable debug logging")
    parser.add_argument("-n", type=int, default=None, help="Number of results (overrides default)")
    # Feedback flags
    parser.add_argument("--liked", metavar="TITLE", help="Mark a title as liked")
    parser.add_argument("--disliked", metavar="TITLE", help="Mark a title as disliked")
    parser.add_argument("--add", metavar="TITLE", help="Add a title to watch history")
    parser.add_argument("--type", choices=["tv", "movie"], default="tv",
                        help="Content type for --add (default: tv)")
    args = parser.parse_args()

    level = logging.DEBUG if args.debug else logging.WARNING
    logging.basicConfig(
        level=logging.WARNING,
        format="%(name)s %(levelname)s: %(message)s",
        stream=sys.stderr,
    )
    logging.getLogger("recommender").setLevel(level)

    # Handle feedback-only invocations (no API keys needed).
    if args.liked or args.disliked or args.add:
        data = fb.load(config.FEEDBACK_PATH)
        if args.liked:
            fb.add_rating(data, args.liked, "liked")
            console_out.print(f"[green]Marked as liked:[/green] {args.liked}")
        if args.disliked:
            fb.add_rating(data, args.disliked, "disliked")
            console_out.print(f"[yellow]Marked as disliked:[/yellow] {args.disliked}")
        if args.add:
            fb.add_addition(data, args.add, args.type)
            console_out.print(f"[green]Added to watch history:[/green] {args.add} ({args.type})")
        fb.save(data, config.FEEDBACK_PATH)
        console_out.print("[dim]Run python -m recommender.setup --refresh-profile to update your taste profile.[/dim]")
        return

    if not config.ANTHROPIC_API_KEY:
        console_err.print("[red]Error: ANTHROPIC_API_KEY not set.[/red]")
        sys.exit(1)
    if not config.TMDB_API_KEY:
        console_err.print("[red]Error: TMDB_API_KEY not set.[/red]")
        sys.exit(1)

    ctx = load_context()
    log.debug("Context loaded: %d events, %d watched titles",
              len(ctx.events), len(ctx.watch_index.entries))

    if args.query:
        query = " ".join(args.query)
        with console_err.status("[bold magenta]Thinking...[/bold magenta]", spinner="dots"):
            results = ask(query, ctx, top_n_override=args.n)
        print_recommendations(results, query)
        return

    console_out.print("Streaming Recommender — ask me anything about what to watch.")
    console_out.print("Feedback: [bold]+liked Title[/bold], [bold]+disliked Title[/bold], [bold]+add Title tv|movie[/bold]")
    console_out.print('Type [bold]exit[/bold] to quit.\n')
    while True:
        try:
            line = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not line or line.lower() == "exit":
            break
        if _handle_feedback_command(line):
            continue
        with console_err.status("[bold magenta]Thinking...[/bold magenta]", spinner="dots"):
            results = ask(line, ctx, top_n_override=args.n)
        print_recommendations(results, line)


if __name__ == "__main__":
    main()
