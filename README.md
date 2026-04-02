# Streamline

A personal streaming recommendation engine that knows your actual taste — built on your real watch history from Netflix, Prime Video, and any manually tracked titles.

## How It Works

Two phases:

1. **Setup (run once)** — parses your watch history, fetches metadata from TMDB, enriches each title with a semantic description via Claude Haiku, and builds a full taste profile via Claude Sonnet (processes all enriched titles in batches, no limit).
2. **Query (any time)** — ask anything in natural language. Claude Sonnet parses your intent, finds candidates via TMDB Discover + Claude semantic suggestions (hybrid generation), filters out what you've already watched, annotates streaming availability, and ranks the results against your taste profile.

## Prerequisites

- Python 3.10+
- TMDB API key (free at themoviedb.org)
- Anthropic API key

## Quick Start

```bash
# 1. Create venv and install dependencies
python3 -m venv venv && source venv/bin/activate && pip install -r requirements.txt

# 2. Add your API keys to .env (gitignored)
cat > .env << 'EOF'
TMDB_API_KEY=your_key_here
ANTHROPIC_API_KEY=your_key_here
EOF

# 3. Add your watch history under data/ (see below)

# 4. Run offline setup
./recommend setup

# 5. Ask for recommendations
./recommend "good British crime drama"
```

## Usage

Everything goes through `./recommend`:

```bash
# Queries
./recommend "paranoid spy thriller like The Night Manager"
./recommend "give me 5 feel-good Bollywood comedies"
./recommend "why not Slow Horses?"              # explains why a title wasn't recommended

# Interactive mode (conversational — supports "more like that", refinements)
./recommend

# Setup
./recommend setup                               # first-time setup
./recommend setup --refresh-data                # re-fetch TMDB + rebuild everything
./recommend setup --refresh-profile             # rebuild taste profile only

# Feedback
./recommend --liked "Tinker Tailor Soldier Spy"
./recommend --disliked "The Long Season"
./recommend --add "Shetland" --type tv

# Options
./recommend --debug "spy thriller"              # full pipeline trace
./recommend -n 5 "dark thriller"                # override result count
```

## Web UI

```bash
./recommend-web start                           # http://localhost:5050
./recommend-web stop
./recommend-web status
./recommend-web restart
./recommend-web logs
```

The web UI provides a search interface, taste profile dashboard with expandable clusters, and a watch history archive with list/grid/compact views.

Port and host are configurable via `STREAMLINE_PORT` and `STREAMLINE_HOST` environment variables.

## Watch History Sources

Place exported CSV files under `data/`:

| Platform | Path |
|----------|------|
| Netflix | `data/netflix/<account_id>/CONTENT_INTERACTION/ViewingActivity.csv` |
| Prime Video | `data/prime_video/Your Prime Video Viewing Activity/Viewing History.csv` |
| Manual list | `data/manual/tv.csv` and `data/manual/movies.csv` |

Netflix and Prime exports are available from their account settings pages. Manual files are plain text, one title per line. Movie titles may include a trailing year (`Zodiac 2007`) which is stripped automatically.

## Configuration

All in `config.py` and `.env`:

| Key | Default | Description |
|-----|---------|-------------|
| `TMDB_API_KEY` | (env) | TMDB v3 API key |
| `ANTHROPIC_API_KEY` | (env) | Anthropic API key |
| `DEFAULT_TOP_N` | 3 | Default number of results per query |
| `MIN_VOTE_COUNT` | 20 | Minimum TMDB votes for discover candidates |
| `RECENCY_HALF_LIFE_DAYS` | 90 | Scoring decay — days until recency score halves |
| `WATCH_REGION` | US | Region for streaming availability lookup |
| `STREAMING_PLATFORMS` | [] | Your subscribed platforms (filters results when set) |

## Running Tests

```bash
python3 -m pytest tests/ -v
```
