---
title: Plex
description: Record Plex plays and ratings as they happen.
---

Plex sends each play to Streamline as it finishes, instead of as an export.
This needs Plex Pass.

## Set it up

1. Set `PLEX_WEBHOOK_TOKEN` to a long random string.
2. Optionally set `PLEX_URL` (for example `http://192.168.1.2:32400`) and `PLEX_TOKEN` for your Plex server.
3. In Plex Web, open **Settings > Webhooks** and add:

   ```
   http://<streamline-host>:<port>/plex/webhook?token=<PLEX_WEBHOOK_TOKEN>
   ```

   Plex must be able to reach that address.

## What it does

Each finished movie or episode is saved as a `plex` play, for every account on the server.
With `PLEX_URL` and `PLEX_TOKEN` set, shows are matched by Plex's exact TMDB ID instead of by title, and ratings come across too:

| Plex stars | Streamline |
|---|---|
| 4 to 5 | Loved |
| 3 to 3.5 | Fine |
| 2.5 or less | Not for me |

The newer rating wins.
Rating in Plex sends no webhook, so run `./recommend plex ratings` to bring rating changes over by hand.
Streamline never writes anything to Plex.

New plays reach the Archive after the next `./recommend setup --refresh-data` and a web restart.

## Removing a wrong play

Plex plays have no export to rebuild them from, so setup never deletes them.
To remove one by hand:

```bash
sqlite3 data/streamline.db "DELETE FROM watch_events WHERE provider = 'plex' AND title = 'Exact Title'"
```
