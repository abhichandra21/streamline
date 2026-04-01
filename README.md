# Streaming Recommender

A personal streaming recommendation engine that knows your actual taste — built on your real watch history from Netflix, Prime Video, and any manually tracked titles.

## How It Works

Two phases:

1. **Setup (run once)** — parses your watch history, fetches metadata from TMDB, enriches each title with a semantic description via Claude Haiku, and builds a taste profile via Claude Sonnet.
2. **Query (any time)** — ask anything in natural language. Claude Sonnet parses your intent, finds candidates via TMDB Discover, filters out what you've already watched, and ranks the results against your taste profile.

## Prerequisites

- Python 3.10+
- `pip install -r requirements.txt`
- TMDB API key (free at themoviedb.org)
- Anthropic API key

## Setup

**1. Export API keys**

```bash
export TMDB_API_KEY=your_key_here
export ANTHROPIC_API_KEY=your_key_here
```

**2. Add your watch history**

Place your exported CSV files under `data/`:

| Platform | Path |
|----------|------|
| Netflix | `data/netflix/<account_id>/CONTENT_INTERACTION/ViewingActivity.csv` |
| Prime Video | `data/prime_video/Your Prime Video Viewing Activity/Viewing History.csv` |
| Manual list | `data/manual/tv.csv` and `data/manual/movies.csv` |

Netflix and Prime exports are available from their account settings pages. The manual files are plain text, one title per line. Movie titles may include a trailing year (`Zodiac 2007`) which is stripped automatically.

**3. Run offline setup**

```bash
python3 -m recommender.setup
```

This takes 10-15 minutes on first run (mostly TMDB fetches and Claude Haiku enrichment). Everything is cached — subsequent runs complete in seconds.

## Usage

**Single query:**

```bash
python3 -m recommender.main "good British crime drama"
python3 -m recommender.main "feel-good Bollywood romance from the 90s"
python3 -m recommender.main "something slow-burn and Korean"
python3 -m recommender.main "give me a few dark Scandinavian thrillers"
```

**Interactive mode:**

```bash
python3 -m recommender.main
```

## Setup Flags

| Flag | Effect |
|------|--------|
| *(none)* | Skip data fetch and taste profile if they already exist |
| `--refresh-data` | Re-fetch TMDB metadata, rebuild watch index and enrichments |
| `--refresh-profile` | Rebuild taste profile from existing enrichments |

## Data Sources

- **Netflix / Prime Video** — full viewing history with timestamps and durations
- **Manual lists** — `data/manual/tv.csv` (series names) and `data/manual/movies.csv` (movie titles, optional year suffix)

Manual lists are gitignored along with all `data/` files since they contain personal watch history.

## Running Tests

```bash
python3 -m pytest tests/ -v
```
