---
title: Troubleshooting
description: Fixes for the usual first-run problems.
---

Check which version is running first.
It's at the bottom of every page, and `/healthz` returns it as `version`.
After `docker compose pull` and `docker compose up -d`, the version should change.

## Settings can't save: "config.local.yaml is a folder"

Docker made a folder because `config.local.yaml` didn't exist when the container first started.
Stop the container, delete the folder, create the file, and start again:

```bash
docker compose down
rm -r config.local.yaml
touch config.local.yaml
docker compose up -d
```

## The container shows as unhealthy

Before the first build, `/healthz` answers `not ready` and `docker compose ps` shows the container as unhealthy.
That is expected.
Open the app, follow Get started on the Settings page, and it turns healthy once the library is built.

## "Export not found on the server"

Export paths are read from inside the container, relative to the checkout folder, and must be inside `data/`.
Put the export under `data/` on the host (for example `data/netflix.zip`), then enter `data/netflix.zip` in Settings.
Run **Check exports** on the Settings page to see what each export holds without saving anything.

## "Permission denied" in the logs

The container can't write to `data/`, `recommender/cache/`, `logs/` or `config.local.yaml`.
On Linux, run the container as your own user by adding your IDs to `.env` (run `id` to see them):

```bash
PUID=1001
PGID=1001
```

Then `docker compose up -d` again.
Alternatively, give the files to user 1000: `sudo chown -R 1000:1000 data recommender/cache logs config.local.yaml`.

## Missing keys

Get started on the Settings page lists what's missing.
You need a TMDB key and a key for the AI provider you picked (Anthropic, Gemini or OpenAI).
Paste them in the Keys section of Settings; they're saved to `data/keys.env` and never shown again in full.
Keys in `.env` work too, but a key saved on the Settings page wins.

## Still stuck

**Logs** in the menu shows the app log, and each setup run writes `logs/setup-<time>.log`.
Open an issue at [github.com/abhichandra21/streamline](https://github.com/abhichandra21/streamline/issues) with the version and the end of that log.
