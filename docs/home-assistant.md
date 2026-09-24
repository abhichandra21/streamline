# Home Assistant dashboard

Home Assistant shows Streamline on its own sidebar dashboard, **Streamline** at `http://192.168.1.50:8123/streamline-tracker`.
Five REST sensors poll the Streamline API on Optimusprime every 15 minutes, a Remote Calendar reads the iCal feed, and one automation notifies the phone when more shows are ready.
Everything below is what is deployed as of 2026-09-23, so this file can rebuild the setup from scratch.

## What is deployed

All paths are in the HA config folder, `/usr/share/hassio/homeassistant/` on 192.168.1.50 (`/config` inside the container).

| Piece | Entity or location | Where it lives |
|---|---|---|
| REST sensors | `sensor.streamline_ready_now`, `sensor.streamline_watchlist`, `sensor.streamline_next_up`, `sensor.streamline_on_deck`, `sensor.streamline_status` | `rest.yaml`, included from `configuration.yaml` |
| Next Up poster | `image.streamline_next_up_poster` | end of `template_sensors.yaml` |
| Calendar | `calendar.streamline_coming_soon` | Remote Calendar integration (UI) |
| Notification | `automation.streamline_ready_now_increased`, sends to `notify.mobile_app_xeon17` | end of `automations.yaml` |
| Dashboard | Streamline, URL `streamline-tracker`, shown in the sidebar | storage mode, `.storage/lovelace.streamline_tracker` |
| Display font | Big Shoulders Display | dashboard resource (CSS) |
| card-mod early load | `/hacsfiles/lovelace-card-mod/card-mod.js` | `frontend.yaml`, `extra_module_url` |

The dashboard needs card-mod, which is installed through HACS (4.2.1).

## The API

Base URL: `http://192.168.1.101:5050`. No auth. Every endpoint is a read-only GET.

| Endpoint | Returns |
|---|---|
| `GET /api/summary` | `ready_now_count`, `coming_soon_count`, `watchlist_count`, one `next_up` card or `null`, `library` counts, `shows_checked_at`, `shows_refreshing`, `taste_profile_built_at` |
| `GET /api/on-deck` | `ready_now` and `coming_soon` lists of cards, plus `shows_checked_at` and `shows_refreshing` |
| `GET /api/watchlist` | `watchlist`: saved titles with `title`, `content_type`, `tmdb_id`, `saved_at`, `poster_url` |
| `GET /api/coming-soon.ics` | iCal feed of upcoming episodes as all-day events |
| `GET /healthz` | `status`: `ok`, `busy`, or `not ready` |

A card has `tmdb_id`, `title`, `season_number`, `latest_aired_episode`, `available_episode_count`, `next_season_number`, `next_episode_number`, `next_air_date`, and `poster_url`.
Any of them can be `null`, and an empty list is `[]`.
For a Ready now show, `season_number` is the season being watched and `next_season_number` is the season of the next episode.
They differ when the next episode starts a new season.

Before Streamline's setup has run, every endpoint, `/healthz` included, returns `503` with `{"status": "not ready", "reason": "setup not run"}`.

## 1. REST sensors

Add this line to `configuration.yaml`, next to the other includes:

```yaml
rest: !include rest.yaml
```

Put these three resources in `rest.yaml`.
The file also holds other REST resources (Tailscale), so add these rather than replacing the file.

```yaml
- resource: http://192.168.1.101:5050/api/summary
  scan_interval: 900
  sensor:
    - name: Streamline Ready Now
      unique_id: streamline_ready_now
      icon: mdi:television-play
      availability: "{{ value_json is defined and value_json.ready_now_count is defined }}"
      value_template: "{{ value_json.ready_now_count }}"
    - name: Streamline Watchlist
      unique_id: streamline_watchlist
      icon: mdi:bookmark-multiple
      availability: "{{ value_json is defined and value_json.watchlist_count is defined }}"
      value_template: "{{ value_json.watchlist_count }}"
    - name: Streamline Next Up
      unique_id: streamline_next_up
      icon: mdi:play-circle
      availability: "{{ value_json is defined and value_json.next_up is defined }}"
      value_template: "{{ (value_json.next_up.title if value_json.next_up else none) or 'Nothing' }}"
      json_attributes:
        - next_up

- resource: http://192.168.1.101:5050/api/on-deck
  scan_interval: 900
  sensor:
    # The lists live in attributes because a state is capped at 255 characters.
    - name: Streamline On Deck
      unique_id: streamline_on_deck
      icon: mdi:playlist-play
      availability: "{{ value_json is defined and value_json.ready_now is defined }}"
      value_template: "{{ value_json.ready_now | length + value_json.coming_soon | length }}"
      json_attributes:
        - ready_now
        - coming_soon
        - shows_checked_at
        - shows_refreshing

- resource: http://192.168.1.101:5050/healthz
  scan_interval: 900
  sensor:
    # Stays available on the pre-setup 503 so it can show "not ready".
    - name: Streamline Status
      unique_id: streamline_status
      icon: mdi:heart-pulse
      availability: "{{ value_json is defined and value_json.status is defined }}"
      value_template: "{{ value_json.status }}"
```

Why every sensor has an `availability` template: HA's REST integration does not treat a 503 as an error.
It parses the 503 body and would otherwise keep the sensors available with empty values.
Checking for each sensor's own key makes the sensors unavailable before setup.
The status sensor only checks for `status`, so it stays available and shows `not ready`.
If Optimusprime is unreachable, every sensor goes unavailable.

The On Deck lists go in attributes because HA caps a state at 255 characters.
Its state is the total number of shows on deck.

## 2. Next Up poster

The stock picture card does not render templates, so the poster URL goes through a template image entity.
Add this to the end of `template_sensors.yaml`, which is the file behind `template:`:

```yaml
- image:
    - name: Streamline Next Up Poster
      unique_id: streamline_next_up_poster
      availability: >
        {% set n = state_attr('sensor.streamline_next_up', 'next_up') %}
        {{ n is mapping and n.poster_url is string and n.poster_url != '' }}
      url: "{{ (state_attr('sensor.streamline_next_up', 'next_up') or {}).get('poster_url', '') }}"
```

## 3. Calendar

Remote Calendar is set up in the UI only.

1. Go to **Settings > Devices & services > Add integration > Remote Calendar**.
2. Set **Calendar name** to `Streamline Coming Soon`.
3. Set **Calendar URL** to `http://192.168.1.101:5050/api/coming-soon.ics`, then submit.

That creates `calendar.streamline_coming_soon`.

## 4. Notification

Add this to the end of `automations.yaml`:

```yaml
- id: streamline_ready_now_increased
  alias: Streamline - Ready Now increased
  description: Notifies when more shows have new episodes ready to watch. Changes
    to or from unavailable are ignored, so a restart does not notify.
  triggers:
  - trigger: state
    entity_id: sensor.streamline_ready_now
  conditions:
  - condition: template
    value_template: '{{ trigger.from_state is not none and trigger.to_state is not none
      and trigger.from_state.state | is_number and trigger.to_state.state | is_number
      and trigger.to_state.state | int > trigger.from_state.state | int }}'
  actions:
  - action: notify.mobile_app_xeon17
    data:
      title: Streamline
      message: '{{ trigger.to_state.state }} shows ready now (was {{ trigger.from_state.state
        }}). Next up: {{ states(''sensor.streamline_next_up'') }}'
  mode: single
```

The automation only fires when the count moves from one number to a higher one.
A change to or from `unavailable` is ignored, so a restart does not notify.
The catch: if the API drops out and comes back with a higher count, that jump is not notified.

## 5. Load card-mod early

The dashboard's look comes from card-mod CSS.
Loaded only as a dashboard resource, card-mod can arrive after the cards render.
When that happens the page shows unstyled stock cards: in testing, styles applied on 1 of 5 fresh loads.
Loading it through `extra_module_url` fixed it (5 of 5).

`frontend.yaml` now reads:

```yaml
themes: !include_dir_merge_named themes
extra_module_url:
  - /hacsfiles/hass-hue-icons/hass-hue-icons.js
  - /local/community/custom-brand-icons/custom-brand-icons.js
  - /hacsfiles/lovelace-card-mod/card-mod.js
```

Keep the existing card-mod dashboard resource as well.
The file had no trailing newline, so make sure the new entry lands on its own line.

## 6. Check and restart

```bash
ssh -i ~/.ssh/id_rsa_optimus root@192.168.1.50
ha core check
ha core restart
```

A new top-level `rest:` key and a `frontend.yaml` change both need a restart, not a reload.
Back up each file before editing it, as `<file>.<YYYYMMDD_HHMMSS>.bak`.
The HA config repo gitignores `*.bak`.

## 7. Dashboard

### Font

Add a dashboard resource: **Settings > Dashboards > (three-dot menu) > Resources > Add resource**.

- URL: `https://fonts.googleapis.com/css2?family=Big+Shoulders+Display:wght@600;800&display=swap`
- Type: **Stylesheet**

### Create the dashboard

1. Go to **Settings > Dashboards > Add dashboard > New dashboard from scratch**.
2. Set the title to `Streamline` and the icon to `mdi:television-play`.
3. Leave **Show in sidebar** on, the same as Net Sentinel.
4. The URL must contain a hyphen, so use `streamline-tracker`.
5. Open the dashboard, click the pencil, then choose **(three-dot menu) > Raw configuration editor**.
6. Replace everything with the YAML below and save.

### Layout

- **Ready to watch:** a full-width row of posters, one per Ready now show, captioned as title and `S<season>, <available_episode_count> new`. A strip of SMPTE test-pattern color bars sits above it. On a phone the posters wrap three to a row.
- **Tiles:** Ready now, Watchlist and Streamline status, in a strip under the posters.
- **Next up:** a small poster from the template image entity, with the title and episode details beside it.
- **Coming up:** the calendar card in list view, with the view switcher and the "all-day" labels hidden.

Headings and show titles use Big Shoulders Display. All other text uses HA's own font.
Colors come from the HA theme, so light and dark mode both work. The only fixed colors are the test-pattern bars.

Notes for editing it:

- The Markdown card strips `class` and `style` attributes from HTML, so the CSS targets plain elements (`figure`, `strong`, `span`, and so on) inside each card.
- HA's own Markdown styles win over card-mod unless every declaration is `!important`.
- In a section with `column_span: 3` or `2`, a card needs `columns: full` to span the whole section.

### Dashboard YAML

```yaml
title: Streamline
views:
- title: Streamline
  path: streamline
  icon: mdi:television-play
  type: sections
  max_columns: 3
  sections:
  - type: grid
    column_span: 3
    cards:
    - type: markdown
      content: |
        {% set shows = state_attr('sensor.streamline_on_deck', 'ready_now') or [] %}<hr>

        ## Ready to watch

        {% if shows %}<div>{% for s in shows %}<figure>{% if s.poster_url %}<img src="{{ s.poster_url }}" alt="">{% else %}<em>{{ s.title or 'Untitled' }}</em>{% endif %}<figcaption><strong>{{ s.title or 'Untitled' }}</strong><span>S{{ s.season_number if s.season_number is not none else '?' }}, {{ s.available_episode_count if s.available_episode_count is not none else '?' }} new</span></figcaption></figure>{% endfor %}</div>{% else %}<p>Nothing new yet. New episodes show up here as they air.</p>{% endif %}
      grid_options:
        columns: full
      card_mod:
        style:
          .: 'ha-card { background: none !important; border: none !important; box-shadow: none !important; } ha-markdown { padding: 0 !important; } ha-card { border-radius: 0 !important; }'
          ha-markdown$: |
            hr { border: 0 !important; border-radius: 0 !important; height: 6px !important; margin: 0 0 20px !important; background: linear-gradient(90deg, #c0c0c0 0 14.28%, #c0c000 0 28.57%, #00c0c0 0 42.85%, #00c000 0 57.14%, #c000c0 0 71.42%, #c00000 0 85.71%, #0000c0 0) !important; }
            h2 { font-family: 'Big Shoulders Display', var(--ha-font-family-body, Roboto), sans-serif !important; font-weight: 800 !important; font-size: 52px !important; line-height: .95 !important; margin: 0 0 20px !important; }
            div { display: grid !important; grid-template-columns: repeat(auto-fit, minmax(132px, 1fr)) !important; gap: 24px 16px !important; }
            figure { margin: 0 !important; min-width: 0 !important; max-width: 240px !important; }
            img, em { display: block !important; width: 100% !important; aspect-ratio: 2 / 3 !important; object-fit: cover !important; border-radius: 4px !important;
              background: var(--secondary-background-color) !important; }
            em { box-sizing: border-box !important; padding: 12px !important; font-style: normal !important; font-family: 'Big Shoulders Display', var(--ha-font-family-body, Roboto), sans-serif !important; font-size: 22px !important; }
            figcaption { margin-top: 10px !important; }
            strong { display: block !important; font-family: 'Big Shoulders Display', var(--ha-font-family-body, Roboto), sans-serif !important; font-weight: 600 !important; font-size: 21px !important; line-height: 1.05 !important;
              overflow-wrap: anywhere !important; }
            span { display: block !important; margin-top: 3px !important; font-size: 13px !important; color: var(--secondary-text-color) !important; }
            p { margin: 0 !important; color: var(--secondary-text-color) !important; }
            @media (max-width: 600px) {
              h2 { font-size: 40px !important; }
              div { grid-template-columns: repeat(3, 1fr) !important; gap: 18px 10px !important; }
              strong { font-size: 17px !important; }
            }
    - type: tile
      entity: sensor.streamline_ready_now
      name: Ready now
      vertical: false
      grid_options:
        columns: 12
      card_mod:
        style: 'ha-card { box-shadow: none !important; }'
    - type: tile
      entity: sensor.streamline_watchlist
      name: Watchlist
      vertical: false
      grid_options:
        columns: 12
      card_mod:
        style: 'ha-card { box-shadow: none !important; }'
    - type: tile
      entity: sensor.streamline_status
      name: Streamline
      vertical: false
      grid_options:
        columns: 12
      card_mod:
        style: 'ha-card { box-shadow: none !important; }'
  - type: grid
    cards:
    - type: markdown
      content: '## Next up'
      grid_options:
        columns: full
        rows: auto
      card_mod:
        style:
          .: 'ha-card { background: none !important; border: none !important; box-shadow: none !important; } ha-markdown { padding: 0 !important; } ha-card { overflow: visible !important; } ha-markdown { padding-top: 8px !important; overflow: visible !important; }'
          ha-markdown$: |
            h2 { font-family: 'Big Shoulders Display', var(--ha-font-family-body, Roboto), sans-serif !important; font-weight: 800 !important; font-size: 34px !important; line-height: 1.1 !important; margin: 0 !important; }
    - type: picture-entity
      entity: image.streamline_next_up_poster
      show_name: false
      show_state: false
      aspect_ratio: '2:3'
      tap_action:
        action: none
      grid_options:
        columns: 4
      card_mod:
        style: 'ha-card { border: none !important; border-radius: 4px !important; box-shadow: none !important; }'
    - type: markdown
      content: |
        {% set n = state_attr('sensor.streamline_next_up', 'next_up') %}{% if n %}<strong>{{ n.title or 'Untitled' }}</strong>

        {% if n.available_episode_count %}{{ n.available_episode_count }} episode{{ 's' if n.available_episode_count != 1 }} ready in season {{ n.season_number }}.{% endif %}
        {% if n.next_air_date and n.next_episode_number %}S{{ n.next_season_number }}E{{ n.next_episode_number }} airs {{ as_datetime(n.next_air_date).strftime('%A, %B %-d') }}.{% endif %}{% else %}<strong>Nothing queued</strong>

        Start a show in Streamline and it lands here.{% endif %}
      grid_options:
        columns: 8
      card_mod:
        style:
          .: 'ha-card { background: none !important; border: none !important; box-shadow: none !important; } ha-markdown { padding: 0 !important; }'
          ha-markdown$: |
            strong { display: block !important; font-family: 'Big Shoulders Display', var(--ha-font-family-body, Roboto), sans-serif !important; font-weight: 800 !important; font-size: 34px !important; line-height: 1 !important; margin: 2px 0 10px !important; color: var(--primary-text-color) !important; }
            p { margin: 0 0 6px !important; color: var(--primary-text-color) !important; line-height: 1.45 !important; }
  - type: grid
    column_span: 2
    cards:
    - type: markdown
      content: '## Coming up'
      grid_options:
        columns: full
        rows: auto
      card_mod:
        style:
          .: 'ha-card { background: none !important; border: none !important; box-shadow: none !important; } ha-markdown { padding: 0 !important; } ha-card { overflow: visible !important; } ha-markdown { padding-top: 8px !important; overflow: visible !important; }'
          ha-markdown$: |
            h2 { font-family: 'Big Shoulders Display', var(--ha-font-family-body, Roboto), sans-serif !important; font-weight: 800 !important; font-size: 34px !important; line-height: 1.1 !important; margin: 0 !important; }
    - type: calendar
      entities:
      - calendar.streamline_coming_soon
      initial_view: listWeek
      grid_options:
        columns: full
      card_mod:
        style:
          .: 'ha-card { box-shadow: none !important; }'
          ha-card ha-full-calendar$: |
            ha-button-toggle-group { display: none; }
            .header { padding-bottom: 4px; }
            h1 { font-family: 'Big Shoulders Display', var(--ha-font-family-body, Roboto), sans-serif; font-weight: 600; font-size: 24px; white-space: nowrap; }
            .fc-theme-standard .fc-list { border: none; }
            .fc-list-day-cushion { background: none; padding-top: 14px; }
            .fc-list-day-text, .fc-list-day-side-text { font-family: 'Big Shoulders Display', var(--ha-font-family-body, Roboto), sans-serif; font-weight: 600; font-size: 19px; }
            .fc-list-day-side-text { color: var(--secondary-text-color); }
            .fc-list-event-time { display: none; }
            .fc-list-event td { border-color: transparent; }
```

## Checking it works

- **Sensors:** every `sensor.streamline_*` shows a real value in **Developer tools > States**. On 2026-09-23 they were Ready Now 6, Watchlist 13, Next Up Dark Matter, On Deck 41, and Status ok.
- **Calendar:** `calendar.streamline_coming_soon` lists events on the dashboard.
- **Poster:** `image.streamline_next_up_poster` shows a poster.
- **Styling:** after a hard refresh, the dashboard shows the color bars and the condensed headings, not stock cards. If it shows stock cards, card-mod is loading late; check step 5.

## Rolling back

- **REST sensors:** delete the three Streamline resources from `rest.yaml`. Remove the `rest: !include rest.yaml` line only if nothing else is left in `rest.yaml`.
- **Poster:** delete the Streamline image block from `template_sensors.yaml`.
- **Automation:** delete the `streamline_ready_now_increased` automation.
- **Calendar:** remove the Remote Calendar entry under **Settings > Devices & services**.
- **Dashboard:** delete it under **Settings > Dashboards**, and delete the Big Shoulders resource.
- **card-mod line:** keep it, since Net Sentinel also benefits.
- **Finish:** run `ha core check`, then `ha core restart`.
