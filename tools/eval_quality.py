#!/usr/bin/env python3
"""Run a quality eval of search and profile building on the real local library.

Usage:
    venv/bin/python -m tools.eval_quality [--searches FILE] [--skip-profile] [--code DIR]
                                          [--set KEY=VALUE ...]

Copies the code, data and caches to a scratch folder, rebuilds the taste profile
there, runs each search in FILE (one per line, default data/evals/searches.txt),
and saves everything to data/evals/runs/<timestamp>/ for /grade-quality to score.
The real library is never written to. --code takes the code from another
checkout, such as a fix worktree, while the data still comes from this one.
--set changes a top-level setting in the copy's config.local.yaml only.

Every LLM call goes through ClaudeCodeClient below, so it uses the Claude
subscription of the logged-in `claude` CLI and no API key. TMDB_API_KEY is needed.
"""
import argparse
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from collections import Counter
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from recommender.llm import LLMClient, UsageStats

REPO = Path(__file__).resolve().parent.parent
EVALS_DIR = REPO / "data/evals"
# Prose profile batches are cached; without clearing them a rebuild makes no LLM calls.
_SKIP_CACHE = {"profile_batches", "logs"}


class ClaudeCodeClient(LLMClient):
    """Claude models through the local `claude -p` CLI, billed to a Claude subscription.

    The call is stripped down to match AnthropicClient: empty system prompt, no
    tools, no settings or MCP servers. Two differences remain. Sonnet still thinks
    on long prompts, which the API client turns off, and organization-managed
    instructions are always added. The CLI has no output cap, so max_tokens
    truncation is not reproduced. Usage cost is what the tokens would cost on the API.
    """

    provider = "claude-code"

    def __init__(self, models: dict[str, object]):
        self.models = models
        self.usage = UsageStats()
        # Without this the CLI bills the API key instead of the subscription.
        self._env = {k: v for k, v in os.environ.items()
                     if k not in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")}
        self._env["MAX_THINKING_TOKENS"] = "0"

    def generate(self, prompt: str, role: str = "reason",
                 max_tokens: int = 1000, timeout: float = 30.0) -> str:
        model = self.models.get(role, self.models.get("reason", "claude-sonnet-5-5"))
        cmd = ["claude", "-p", "--model", model, "--output-format", "json",
               "--system-prompt", "", "--tools", "", "--setting-sources", "",
               "--strict-mcp-config", "--no-session-persistence"]
        t0 = time.monotonic()
        # The CLI adds its own startup time, so allow well beyond the API timeout.
        proc = subprocess.run(cmd, input=prompt, capture_output=True, text=True,
                              env=self._env, timeout=max(timeout * 4, 120))
        latency_ms = (time.monotonic() - t0) * 1000
        try:
            data = json.loads(proc.stdout)
        except json.JSONDecodeError:
            raise RuntimeError(f"claude -p failed (model={model}, exit={proc.returncode}): "
                               f"{(proc.stderr or proc.stdout).strip()[-500:]}")
        if data.get("is_error"):
            raise RuntimeError(f"claude -p failed (model={model}): {data.get('result')}")
        usage = data.get("usage") or {}
        self.usage.record(
            model=model,
            input_t=usage.get("input_tokens", 0),
            output_t=usage.get("output_tokens", 0),
            latency_ms=latency_ms,
        )
        self.was_truncated = False
        return data.get("result") or ""


def _claude_code_client() -> ClaudeCodeClient:
    import config
    return ClaudeCodeClient(models=config.LLM_MODELS["anthropic"])


def _load_dotenv() -> None:
    env_path = REPO / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _env() -> dict[str, str]:
    env = dict(os.environ)
    # Any call that bypasses the claude-code provider fails instead of spending API money.
    env["ANTHROPIC_API_KEY"] = "eval-runs-use-the-claude-subscription"
    env["LLM_PROVIDER"] = "anthropic"
    return env


def _copy_code(code_dir: Path, root: Path) -> None:
    """Copy the tracked code (HEAD plus working-tree edits) of code_dir, then this tool on top."""
    files = subprocess.run(["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
                           cwd=code_dir, check=True, capture_output=True).stdout.split(b"\0")
    for name in filter(None, files):
        src = code_dir / name.decode()
        if src.is_file():
            dst = root / name.decode()
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
    # The checkout under test may not have the eval tool, or may have an older one.
    (root / "tools").mkdir(exist_ok=True)
    shutil.copy2(Path(__file__), root / "tools/eval_quality.py")


def _apply_settings(root: Path, settings: list[str]) -> dict:
    import yaml
    path = root / "config.local.yaml"
    local = (yaml.safe_load(path.read_text()) or {}) if path.exists() else {}
    applied = {}
    for item in settings:
        key, sep, value = item.partition("=")
        if not sep or not key.strip():
            raise SystemExit(f"--set needs KEY=VALUE, got {item!r}")
        applied[key.strip()] = yaml.safe_load(value) if value.strip() else ""
    local.update(applied)
    path.write_text(yaml.safe_dump(local, sort_keys=False, allow_unicode=True))
    return applied


def _copy_library(code_dir: Path, root: Path) -> None:
    _copy_code(code_dir, root)
    (root / "data").mkdir(exist_ok=True)
    src = sqlite3.connect(REPO / "data/streamline.db")
    dst = sqlite3.connect(root / "data/streamline.db")
    try:
        src.backup(dst)
    finally:
        src.close()
        dst.close()
    for name in ("data/overrides.json", "data/manual", "config.local.yaml"):
        path = REPO / name
        if path.is_dir():
            shutil.copytree(path, root / name)
        elif path.exists():
            shutil.copy2(path, root / name)
    shutil.copytree(REPO / "recommender/cache", root / "recommender/cache",
                    ignore=lambda d, names: [n for n in names if n in _SKIP_CACHE])


def _run_profile(root: Path, run_dir: Path) -> dict:
    t0 = time.monotonic()
    proc = subprocess.run([sys.executable, "-m", "tools.eval_quality", "_profile"],
                          cwd=root, env=_env(), capture_output=True, text=True)
    (run_dir / "profile_build.log").write_text(proc.stdout + proc.stderr)
    if proc.returncode != 0:
        raise SystemExit(f"Profile build failed, see {run_dir / 'profile_build.log'}")
    shutil.copy2(root / "recommender/cache/taste_profile.txt", run_dir / "profile_new.txt")
    structured = root / "recommender/cache/taste_profile_structured.json"
    if structured.exists():
        shutil.copy2(structured, run_dir / "profile_structured_new.json")
    return {"profile_seconds": round(time.monotonic() - t0)}


def _run_searches(root: Path, searches: Path, run_dir: Path) -> None:
    proc = subprocess.run([sys.executable, "-m", "tools.eval_quality", "_search", str(searches),
                           str(run_dir / "searches.json")],
                          cwd=root, env=_env(), capture_output=True, text=True)
    (run_dir / "searches.log").write_text(proc.stdout + proc.stderr)
    if proc.returncode != 0:
        raise SystemExit(f"Searches failed, see {run_dir / 'searches.log'}")


def _profile() -> None:
    """Runs inside the scratch copy: the same rebuild as `setup --refresh-profile`."""
    from recommender import setup
    from recommender.log import setup_logging

    setup_logging()
    setup.create_client = lambda provider=None: _claude_code_client()
    setup.run_setup(refresh_profile=True)


def _search(searches: Path, out: Path) -> None:
    """Runs inside the scratch copy: answer each search and add the code checks."""
    import config
    from recommender import main as cli
    from recommender import query_engine
    from recommender.log import setup_logging
    from recommender.watch_index import _normalize

    # LOG_LEVEL=DEBUG puts the full pipeline trace in searches.log.
    setup_logging()
    cli.create_client = lambda provider=None: _claude_code_client()
    ctx = cli.load_context()
    watched = ctx.watch_index.normalized_titles
    runs = []
    for query in [q.strip() for q in searches.read_text().splitlines() if q.strip()]:
        t0 = time.monotonic()
        try:
            results = query_engine.ask(query, ctx)
            error = None
        except Exception as exc:  # one failed search should not lose the rest
            results, error = [], f"{type(exc).__name__}: {exc}"
        keys = [(_normalize(r.title), r.content_type) for r in results]
        runs.append({
            "query": query,
            "seconds": round(time.monotonic() - t0),
            "error": error,
            "results": [asdict(r) for r in results],
            "checks": {
                "result_count": len(results),
                "already_watched": [r.title for r, k in zip(results, keys) if k in watched],
                "duplicates": [t for (t, _), n in Counter(keys).items() if n > 1],
                "missing_explanation": [r.title for r in results if not r.explanation.strip()],
            },
        })
    usage = ctx.llm.usage
    out.write_text(json.dumps({"searches": runs, "llm_calls": usage.calls,
                               "input_tokens": usage.input_tokens,
                               "output_tokens": usage.output_tokens,
                               "models": dict(config.LLM_MODELS.get("anthropic", {}))}, indent=2))


def _library_context(run_dir: Path) -> None:
    """Save what the grader needs to judge fit: ratings, follows, watched titles, data checks."""
    import config
    from recommender import user_store
    from recommender import watch_index as wi

    index = wi.load(config.WATCH_INDEX_PATH)
    enrichments = json.loads((Path(config.ENRICHMENT_CACHE_DIR) / "index.json").read_text())
    ratings = user_store.load_ratings(config.EVENT_DB_PATH)
    library = {
        "ratings": {kind: [r["title"] for r in ratings if r["rating"] == kind]
                    for kind in (user_store.RATING_MORE, user_store.RATING_NEUTRAL, user_store.RATING_LESS)},
        "followed_shows": [s["title"] for s in user_store.list_show_tracking(config.EVENT_DB_PATH)],
        "watched": sorted(f"{e['title']} ({e.get('content_type') or 'unknown'})" for e in index.entries),
        "data_checks": {
            "watched_titles": len(index.entries),
            "no_tmdb_match": sorted(e["title"] for e in index.entries if not e.get("tmdb_id")),
            "descriptions": len(enrichments),
        },
    }
    (run_dir / "library.json").write_text(json.dumps(library, indent=2))
    current = Path(config.TASTE_PROFILE_PATH)
    if current.exists():
        shutil.copy2(current, run_dir / "profile_current.txt")
    # Search and the home page read the structured profile, so it is graded too.
    structured = Path(config.STRUCTURED_TASTE_PROFILE_PATH)
    if structured.exists():
        shutil.copy2(structured, run_dir / "profile_structured_current.json")


def main() -> None:
    if len(sys.argv) == 4 and sys.argv[1] == "_search":
        _search(Path(sys.argv[2]), Path(sys.argv[3]))
        return
    if sys.argv[1:] == ["_profile"]:
        _profile()
        return
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--searches", type=Path, default=EVALS_DIR / "searches.txt")
    parser.add_argument("--skip-profile", action="store_true", help="Run the searches only")
    parser.add_argument("--code", type=Path, default=REPO,
                        help="Checkout whose code is evaluated (default: this one)")
    parser.add_argument("--set", action="append", default=[], metavar="KEY=VALUE",
                        help="Override a top-level setting in the scratch copy only")
    args = parser.parse_args()
    args.code = args.code.resolve()

    _load_dotenv()
    if not os.environ.get("TMDB_API_KEY"):
        raise SystemExit("TMDB_API_KEY is not set")
    if not args.searches.exists():
        raise SystemExit(f"No searches file at {args.searches}. /grade-quality writes one.")
    if shutil.which("claude") is None:
        raise SystemExit("The claude CLI is not on PATH")

    run_dir = EVALS_DIR / "runs" / datetime.now().strftime("%Y%m%d-%H%M%S")
    run_dir.mkdir(parents=True)
    def git(*cmd: str) -> str:
        return subprocess.run(["git", *cmd], cwd=args.code, capture_output=True, text=True).stdout.strip()
    meta = {"code": str(args.code), "branch": git("branch", "--show-current"),
            "commit": git("rev-parse", "--short", "HEAD"),
            "dirty": bool(git("status", "--porcelain", "--untracked-files=no"))}
    _library_context(run_dir)

    with tempfile.TemporaryDirectory(prefix="streamline-eval-") as tmp:
        root = Path(tmp)
        print(f"Copying the library to {root}")
        _copy_library(args.code, root)
        if args.set:
            meta["settings"] = _apply_settings(root, args.set)
        if not args.skip_profile:
            print("Rebuilding the taste profile (several minutes)")
            meta.update(_run_profile(root, run_dir))
        print(f"Running {sum(1 for q in args.searches.read_text().splitlines() if q.strip())} searches")
        _run_searches(root, args.searches.resolve(), run_dir)
    (run_dir / "meta.json").write_text(json.dumps(meta, indent=2))
    print(f"Saved {run_dir}")


if __name__ == "__main__":
    main()
