---
title: Running as a service
description: Keep the web UI running on a home server.
---

`./recommend-web start` is fine on a laptop.
For an always-on install, run the app under gunicorn with the included systemd unit:

```bash
sudo cp streamline-web.service /etc/systemd/system/
sudo systemctl enable --now streamline-web
```

Edit the `User`, `WorkingDirectory`, and venv paths in `streamline-web.service` first.
The unit reads keys from `.env` with `EnvironmentFile`, so restart the service after changing `.env`.

## Port and address

Set `STREAMLINE_PORT` (default `5051`) and `STREAMLINE_HOST` to choose where it listens.

## Protect it

If anyone else on your network can reach it, set `STREAMLINE_PASSWORD`.
All changes from the browser also need a CSRF token, so other sites can't make changes on your behalf.

## Keep data fresh

The web UI refreshes IMDb ratings and On Deck on its own.
New exports and Plex plays reach the Archive after `./recommend setup --refresh-data`, which you can run on a schedule (a cron job or systemd timer, weekly is plenty).
It never rebuilds the taste profile, so scheduled runs only pay for descriptions of new titles.
