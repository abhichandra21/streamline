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
cp config.local.example.yaml config.local.yaml
```

1. Put your keys in `.env`: `TMDB_API_KEY` plus one LLM key, such as `ANTHROPIC_API_KEY`.
2. Copy your export zips into `data/` and set their paths in `config.local.yaml`, for example `data/netflix/export.zip`. See [Watch history](/guides/watch-history/).
3. Run setup once:

   ```bash
   docker compose run --rm streamline ./recommend setup
   ```

4. Start the web UI:

   ```bash
   docker compose up -d
   ```

   Open [http://localhost:5051](http://localhost:5051).

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

## Where your data lives

The container keeps nothing of its own.
Everything is in the checkout folder, in the same places as a non-Docker install:

| Folder or file | What it holds |
|---|---|
| `data/` | Your exports and `streamline.db` (history, ratings, watchlist, follows, searches) |
| `recommender/cache/` | TMDB data, descriptions, the taste profile, IMDb ratings |
| `logs/` | The app log |
| `config.yaml` | Settings; the Settings page writes here |
| `config.local.yaml` | Your export paths |

Back up `data/streamline.db`; Plex plays and your ratings exist only there.

## Notes

- Create `config.local.yaml` before the first `docker compose` command. If it is missing, Docker creates a folder with that name instead, and the app can't start.
- The container runs as user ID 1000. On Linux, if your user has a different ID, run `sudo chown -R 1000:1000 data recommender/cache logs`.
- Set `STREAMLINE_PASSWORD` in `.env` if anyone else can reach the server. See [Running as a service](/guides/running-as-a-service/).
- To build the image yourself instead of pulling it, run `docker compose build`.
