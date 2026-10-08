---
title: Home Assistant
description: A read-only JSON API and calendar feed for dashboards.
---

Streamline serves a small read-only API for dashboards such as Home Assistant.
It never calls an LLM, so polling it costs nothing.

## Endpoints

| Endpoint | Returns |
|---|---|
| `GET /api/summary` | `ready_now_count`, `coming_soon_count`, `watchlist_count`, one `next_up` card or `null`, `library` counts, `shows_checked_at`, `shows_refreshing`, `taste_profile_built_at` |
| `GET /api/on-deck` | `ready_now` and `coming_soon` lists of cards, plus `shows_checked_at` and `shows_refreshing` |
| `GET /api/watchlist` | `watchlist`: saved titles with `title`, `content_type`, `tmdb_id`, `saved_at`, `poster_url` |
| `GET /api/coming-soon.ics` | An iCal feed of upcoming episodes as all-day events |
| `GET /healthz` | `status`: `ok`, `busy`, or `not ready` |

A card has `tmdb_id`, `title`, `season_number`, `latest_aired_episode`, `available_episode_count`, `next_season_number`, `next_episode_number`, `next_air_date`, and `poster_url`.
Any field can be `null`.
Before setup has run, every endpoint returns `503` with `{"status": "not ready", "reason": "setup not run"}`.

## Authentication

With no `STREAMLINE_PASSWORD`, the API is open.
With a password set, set `STREAMLINE_API_TOKEN` too and send it as `Authorization: Bearer <token>`.
The token only works for `GET /api/*`, so a dashboard can read but can't change anything.

## Example sensors

```yaml
# configuration.yaml
rest:
  - resource: http://streamline.local:5051/api/summary
    scan_interval: 900
    headers:
      Authorization: !secret streamline_api_token
    sensor:
      - name: Streamline Ready Now
        icon: mdi:television-play
        value_template: "{{ value_json.ready_now_count }}"
      - name: Streamline Watchlist
        icon: mdi:bookmark-multiple
        value_template: "{{ value_json.watchlist_count }}"
```

Store the secret as `Bearer <token>`, including the word Bearer.

For a calendar, add the **Remote Calendar** integration with the URL `http://streamline.local:5051/api/coming-soon.ics`.
