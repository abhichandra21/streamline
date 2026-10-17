---
title: Docker
description: Run Streamline with Docker Compose.
---

Docker is the easiest way to run Streamline on a home server.
The image is published for both x86 and ARM (including Raspberry Pi and Apple silicon) at `ghcr.io/abhichandra21/streamline`.

## Set it up

```bash
git clone https://github.com/abhichandra21/streamline.git
cd streamline
mkdir -p data recommender/cache logs
touch config.local.yaml
```

1. Copy your export files into `data/`, for example `data/netflix/export.zip`. See [Watch history](/guides/watch-history/).
2. Start the web UI:

   ```bash
   docker compose up -d
   ```

3. Open [http://localhost:5051](http://localhost:5051) and follow **Get started** in Settings: add your keys, point at your exports, then build your library. See [Getting started](/getting-started/).

Keys can also go in `.env` (`TMDB_API_KEY` plus one LLM key, such as `ANTHROPIC_API_KEY`), and setup also runs from the terminal with `docker compose run --rm streamline ./recommend setup`.

## Everyday commands

Every `./recommend` command works inside the container:

```bash
docker compose run --rm streamline ./recommend setup --refresh-profile
docker compose run --rm streamline ./recommend setup --refresh-data
docker compose exec streamline ./recommend "gritty British crime drama"
```

Update to the latest version with:

```bash
git pull
docker compose pull
docker compose up -d
```

The version at the bottom of every page shows whether the update took.

If `git pull` says your local changes to `config.yaml` would be overwritten, see [Settings](/guides/settings/).

## Where your data lives

The container keeps nothing of its own.
Everything is in the checkout folder, in the same places as a non-Docker install:

| Folder or file | What it holds |
|---|---|
| `data/` | Your exports and `streamline.db` (history, ratings, watchlist, follows, searches) |
| `recommender/cache/` | TMDB data, descriptions, the taste profile, IMDb ratings |
| `logs/` | The app log |
| `config.yaml` | The shipped default settings; updated by `git pull` |
| `config.local.yaml` | Your export paths, and anything you change on the Settings page |

Back up `data/streamline.db`; Plex plays and your ratings exist only there.

## Notes

- Create `config.local.yaml` before the first `docker compose` command. If it is missing, Docker creates a folder with that name instead, and Settings can't save.
- Until the first build finishes, `docker compose ps` shows the container as starting for a few minutes and then unhealthy. That is expected; it turns healthy once the library is built.
- The container runs as user ID 1000. On Linux, if your user has a different ID, set `PUID` and `PGID` in `.env` to your IDs (run `id` to see them).
- For other first-run problems, see [Troubleshooting](/guides/troubleshooting/).
- Set `STREAMLINE_PASSWORD` in `.env` if anyone else can reach the server. See [Running as a service](/guides/running-as-a-service/).
- To build the image yourself instead of pulling it, run `docker compose build`.
