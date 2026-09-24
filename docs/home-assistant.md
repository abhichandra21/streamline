# Home Assistant dashboard

Streamline serves a small read-only API so Home Assistant can show what's ready to watch, what's coming soon, and what's on the watchlist.
It never calls the LLM, so polling it every few minutes is cheap.

## Endpoints

| Endpoint | Returns |
|---|---|
| `GET /api/summary` | Counts, one `next_up` show, library size, and refresh state. Small enough for sensor states. |
| `GET /api/on-deck` | `ready_now` and `coming_soon` show cards, soonest first. |
| `GET /api/watchlist` | Saved titles with `saved_at` and `poster_url`. |
| `GET /api/coming-soon.ics` | iCal feed of upcoming episodes and season premieres, as all-day events. Includes next episodes of Ready now shows. |

Example `/api/summary`:

```json
{
  "ready_now_count": 3,
  "coming_soon_count": 5,
  "watchlist_count": 12,
  "next_up": {"tmdb_id": 95480, "title": "Slow Horses", "season_number": 5,
              "latest_aired_episode": null, "available_episode_count": null,
              "next_season_number": 5, "next_episode_number": 4,
              "next_air_date": "2026-09-26",
              "poster_url": "https://image.tmdb.org/t/p/w300/..."},
  "library": {"total": 1840, "tv": 420, "movies": 1420},
  "shows_checked_at": "2026-09-24T06:00:00+00:00",
  "shows_refreshing": false,
  "taste_profile_built_at": "2026-09-20T08:12:00+00:00"
}
```

Every show card has the same keys.
A key with nothing to report is `null`, and an empty section is `[]`, so templates don't break.
`next_up` is the show whose next episode airs soonest, from Ready now or Coming soon, or `null` when nothing is dated.
A Ready now show can already have unwatched episodes and a next one on the way.
`season_number` is the season you're watching, and `next_season_number` is the season the next episode belongs to.
They differ when the next episode opens a new season.

Before `./recommend setup` has run, every endpoint returns `503` with `{"status": "not ready"}`.
Home Assistant then marks the sensors unavailable instead of showing zeros.

## Freshness

Requesting `/api/summary`, `/api/on-deck`, or `/api/coming-soon.ics` refreshes release data exactly like opening `/shows` does.
Followed shows are checked every 24 hours and discovery every 7 days.
Only one refresh runs at a time, it calls TMDB only, and it shows up in `/status` like any other job.
The response comes back right away from the current data.
While a refresh is running, `shows_refreshing` is `true`.
`shows_checked_at` says when the last full check finished.

`/api/watchlist` reads local state only and never starts a refresh.

## Auth

When `STREAMLINE_PASSWORD` is not set, the API is open to anyone who can reach the server, like the rest of the app.

When it is set, every API request needs a credential.
Two work:

- **Basic auth with the password.** Any username. This is the same password that can change settings and delete data.
- **A read-only token.** Set `STREAMLINE_API_TOKEN` on the server, then send `Authorization: Bearer <token>`. The token only opens `GET /api/*`. Every other page and every write still needs the password.

Prefer the token for Home Assistant, so the dashboard never holds a credential that can change anything.

## Sensors

With the token:

```yaml
rest:
  - resource: http://streamline.local:5051/api/summary
    headers:
      Authorization: !secret streamline_api_bearer   # "Bearer <token>"
    scan_interval: 900
    sensor:
      - name: Streamline Ready Now
        value_template: "{{ value_json.ready_now_count }}"
      - name: Streamline Watchlist
        value_template: "{{ value_json.watchlist_count }}"
      - name: Streamline Next Up
        value_template: "{{ value_json.next_up.title if value_json.next_up else 'Nothing' }}"
        json_attributes_path: "$.next_up"
        json_attributes: [season_number, next_episode_number, next_air_date, poster_url]

  - resource: http://streamline.local:5051/api/on-deck
    headers:
      Authorization: !secret streamline_api_bearer
    scan_interval: 900
    sensor:
      - name: Streamline On Deck
        value_template: "{{ value_json.ready_now | length }}"
        json_attributes: [ready_now, coming_soon]
```

With the password instead, replace `headers:` with:

```yaml
    authentication: basic
    username: ha
    password: !secret streamline_password
```

Lists go in attributes, not the state, because Home Assistant limits a state to 255 characters.

## Dashboard ideas

**Tile cards** for `sensor.streamline_ready_now` and `sensor.streamline_watchlist`.
For an up/busy indicator, add a REST sensor on `/healthz`, which needs no auth and returns `ok`, `busy`, or `not ready` in `status`.

**Markdown card** listing the ready-now shows:

```yaml
type: markdown
title: Ready to watch
content: >
  {% for show in state_attr('sensor.streamline_on_deck', 'ready_now') or [] %}
  - **{{ show.title }}** S{{ show.season_number }}, {{ show.available_episode_count }} new
  {% else %}
  Nothing new.
  {% endfor %}
```

**Picture card** for the Next Up poster:

```yaml
template:
  - image:
      - name: Streamline Next Up Poster
        url: "{{ state_attr('sensor.streamline_next_up', 'poster_url') }}"
```

```yaml
type: picture-entity
entity: image.streamline_next_up_poster
```

The stock picture card does not render templates, which is why the URL goes through a template image entity.

**Calendar card.**
Add the **Remote Calendar** integration with the URL `http://streamline.local:5051/api/coming-soon.ics`, then add a calendar card for it.
The same URL works in phone and desktop calendar apps.
Each episode keeps the same event ID, so a date change moves the event instead of duplicating it.
The feed follows the same auth rules as the rest of the API.
Calendar clients usually can't send a bearer token, so when `STREAMLINE_PASSWORD` is set, plan on the calendar using Basic auth with the password, if the client supports it.
The read-only token does not help here.

## Automation: notify on a new episode

```yaml
automation:
  - alias: Streamline new episode
    trigger:
      - platform: state
        entity_id: sensor.streamline_ready_now
    condition:
      - condition: template
        value_template: >
          {{ trigger.from_state.state | int(0) < trigger.to_state.state | int(0) }}
    action:
      - service: notify.mobile_app_phone
        data:
          message: "Something new is ready to watch in Streamline."
```

## Not included

The API is read-only.
Actions like "mark caught up" or "save to watchlist" from Home Assistant buttons are not supported.
