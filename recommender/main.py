import logging
import sys
from pathlib import Path

from rich.console import Console
from rich.panel import Panel

import config
from recommender import user_store
from recommender.user_state import UserStateIndex
from recommender.event_store import load_events
from recommender.llm import create_client
from recommender.models import Recommendation
from recommender.tmdb_client import TmdbClient
from recommender.query_engine import RecommendContext, ConversationContext, ask
from recommender import watch_index as wi
from recommender import history

def _events_loader_fallback() -> list:
    events = load_events(config.EVENT_DB_PATH)
    if not events and not Path(config.EVENT_DB_PATH).exists():
        from recommender.setup import load_platform_events_from_exports
        return load_platform_events_from_exports(fail_on_error=False)
    return events

log = logging.getLogger("recommender")

console_err = Console(stderr=True)   # spinners, progress, warnings
console_out = Console()              # recommendation results


def _load_user_state():
    if Path(config.EVENT_DB_PATH).exists():
        user_store.ensure_user_store(config.EVENT_DB_PATH, config.FEEDBACK_PATH)
    return UserStateIndex.load(config.EVENT_DB_PATH)


def load_context(provider: str | None = None) -> RecommendContext:
    index_path = Path(config.WATCH_INDEX_PATH)
    if not index_path.exists():
        console_err.print("[red]Watch index not found. Run: ./recommend setup[/red]")
        sys.exit(1)

    profile_path = Path(config.TASTE_PROFILE_PATH)
    if not profile_path.exists():
        console_err.print("[red]Taste profile not found. Run: ./recommend setup[/red]")
        sys.exit(1)

    try:
        llm = create_client(provider)
    except RuntimeError as exc:
        console_err.print(f"[red]Error: {exc}[/red]")
        sys.exit(1)

    ctx = RecommendContext(
        taste_profile=profile_path.read_text(),
        watch_index=wi.load(config.WATCH_INDEX_PATH),
        tmdb_client=TmdbClient(api_key=config.TMDB_API_KEY, cache_dir=config.CACHE_DIR),
        llm=llm,
        cache_dir=config.ENRICHMENT_CACHE_DIR,
        _events_loader=_events_loader_fallback,
        providers_cache_dir=config.PROVIDERS_CACHE_DIR,
        watch_region=config.WATCH_REGION,
        streaming_platforms=list(config.STREAMING_PLATFORMS),
        user_state=_load_user_state(),
    )
    return ctx


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
            user_store.ensure_user_store(config.EVENT_DB_PATH, config.FEEDBACK_PATH)
            try:
                ct = user_store.resolve_rating_content_type(config.EVENT_DB_PATH, title)
            except ValueError as exc:
                console_out.print(f"[red]{exc}[/red]")
                return True
            user_store.rate_title(config.EVENT_DB_PATH, title, ct, "liked")
            console_out.print(f"[green]Marked as liked:[/green] {title}")
            console_out.print("[dim]Run --refresh-profile to update your taste profile.[/dim]")
            return True

    # -disliked <title> / +disliked <title>
    for prefix in ("-disliked ", "+disliked ", "-dislike ", "+dislike "):
        if line.lower().startswith(prefix):
            title = line[len(prefix):].strip()
            user_store.ensure_user_store(config.EVENT_DB_PATH, config.FEEDBACK_PATH)
            try:
                ct = user_store.resolve_rating_content_type(config.EVENT_DB_PATH, title)
            except ValueError as exc:
                console_out.print(f"[red]{exc}[/red]")
                return True
            user_store.rate_title(config.EVENT_DB_PATH, title, ct, "disliked")
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
        user_store.ensure_user_store(config.EVENT_DB_PATH, config.FEEDBACK_PATH)
        user_store.add_to_archive(config.EVENT_DB_PATH, title, ct, source="cli")
        console_out.print(f"[green]Added to watch history:[/green] {title} ({ct})")
        console_out.print("[dim]Run --refresh-profile to update your taste profile.[/dim]")
        return True

    return False


def _show_history(limit: int | None = None) -> None:
    """Display recent query history."""
    entries = history.load(limit=limit or 20)
    if not entries:
        console_out.print("[dim]No query history yet.[/dim]")
        return
    console_out.print(f"[bold]Recent queries[/bold] ({len(entries)} shown)\n")
    for e in entries:
        ts = e["timestamp"][:19].replace("T", " ")
        n_results = len(e.get("results", []))
        titles = ", ".join(r["title"] for r in e.get("results", [])[:3])
        console_out.print(f"[dim]{ts}[/dim]  [bold]{e['query']}[/bold]")
        if titles:
            console_out.print(f"  {n_results} results: {titles}")
        console_out.print(f"  [dim]{e.get('provider', '')} | {e.get('usage', '')}[/dim]\n")


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
    parser.add_argument("--history", action="store_true", help="Show recent query history")
    parser.add_argument("--provider", choices=["anthropic", "gemini", "openai"],
                        help="LLM provider (default: from config/env)")
    args = parser.parse_args()

    from recommender.log import setup_logging
    setup_logging(level_override="DEBUG" if args.debug else None)

    # Handle feedback-only invocations (no API keys needed).
    if args.liked or args.disliked or args.add:
        user_store.ensure_user_store(config.EVENT_DB_PATH, config.FEEDBACK_PATH)
        try:
            if args.liked:
                ct = user_store.resolve_rating_content_type(config.EVENT_DB_PATH, args.liked)
                user_store.rate_title(config.EVENT_DB_PATH, args.liked, ct, "liked")
                console_out.print(f"[green]Marked as liked:[/green] {args.liked}")
            if args.disliked:
                ct = user_store.resolve_rating_content_type(config.EVENT_DB_PATH, args.disliked)
                user_store.rate_title(config.EVENT_DB_PATH, args.disliked, ct, "disliked")
                console_out.print(f"[yellow]Marked as disliked:[/yellow] {args.disliked}")
        except ValueError as exc:
            console_out.print(f"[red]{exc}[/red]")
            return
        if args.add:
            user_store.add_to_archive(config.EVENT_DB_PATH, args.add, args.type, source="cli")
            console_out.print(f"[green]Added to watch history:[/green] {args.add} ({args.type})")
        console_out.print("[dim]Run python -m recommender.setup --refresh-profile to update your taste profile.[/dim]")
        return

    # Handle history command (no API keys needed)
    if args.history:
        _show_history(limit=args.n)
        return

    if not config.TMDB_API_KEY:
        console_err.print("[red]Error: TMDB_API_KEY not set.[/red]")
        sys.exit(1)

    ctx = load_context(provider=args.provider)
    log.debug("Context loaded: %d watched titles", len(ctx.watch_index.entries))

    if args.query:
        query = " ".join(args.query)
        ctx.llm.usage.reset()
        with console_err.status("[bold magenta]Thinking...[/bold magenta]", spinner="dots"):
            results = ask(query, ctx, top_n_override=args.n)
        print_recommendations(results, query)
        console_err.print(f"[dim]{ctx.llm.usage.summary()}[/dim]")
        history.record(query, results, ctx.llm.provider, ctx.llm.usage.summary())
        return

    console_out.print("Streaming Recommender — ask me anything about what to watch.")
    console_out.print("Feedback: [bold]+liked Title[/bold], [bold]+disliked Title[/bold], [bold]+add Title tv|movie[/bold]")
    console_out.print('Type [bold]exit[/bold] to quit.\n')
    from recommender.query_engine import QueryIntent
    conv_ctx: ConversationContext | None = None
    while True:
        try:
            line = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not line or line.lower() == "exit":
            break
        if _handle_feedback_command(line):
            ctx.user_state = UserStateIndex.load(config.EVENT_DB_PATH)
            continue
        # Create conv_ctx before ask() so ask() can write the real parsed intent into it
        # on the very first turn — otherwise "what else?" on turn 2 reuses a placeholder.
        if conv_ctx is None:
            placeholder = QueryIntent(
                genres=[], origin_countries=[], languages=[], mood_descriptors=[],
                similar_to=[], max_runtime_minutes=None, year_from=None, year_to=None,
                unwatched_only=True, special_intent=None, content_type="both",
                top_n=config.DEFAULT_TOP_N, platforms=[],
            )
            conv_ctx = ConversationContext(
                last_query=line, last_intent=placeholder, last_results=[],
            )
        ctx.llm.usage.reset()
        with console_err.status("[bold magenta]Thinking...[/bold magenta]", spinner="dots"):
            results = ask(line, ctx, top_n_override=args.n, conv_ctx=conv_ctx)
        print_recommendations(results, line)
        console_err.print(f"[dim]{ctx.llm.usage.summary()}[/dim]")
        history.record(line, results, ctx.llm.provider, ctx.llm.usage.summary())
        conv_ctx.last_query = line
        conv_ctx.last_results = results
        conv_ctx.excluded_titles.update(r.title for r in results)


if __name__ == "__main__":
    main()
