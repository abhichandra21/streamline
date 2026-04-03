# Homeserver Roadmap

## Scope

This roadmap assumes Streamline is a single-user, self-hosted app that runs on a homeserver beside tools like Plex, Jellyfin, Radarr, and Sonarr.

Out of scope:

- SaaS concerns
- Multi-user profiles
- Team or enterprise administration

## Phase 1: Release Gate

These items should be finished before calling the app a solid homeserver release.

### Deployment and runtime

- Ship a `Dockerfile` and a documented Compose setup
- Persist `data/`, `config.yaml`, and cache directories with mounted volumes
- Replace the Flask development server with a production entrypoint such as `gunicorn` or `waitress`
- Keep the systemd unit as an optional deployment path, not the primary one

### Safety and access control

- Add a simple app auth mode for direct deployments
- Support a documented reverse-proxy-auth mode for users who already protect apps at the proxy layer
- Add CSRF protection for all write actions
- Keep localhost-first defaults

### Background jobs and status

- Move setup, profile rebuild, and provider refresh work out of the request thread
- Add a job status view with current state, last run time, duration, and error output
- Show whether the taste profile is stale and why

### Reliability

- Stop swallowing ingestion errors silently
- Surface source-specific failures in the UI
- Remove shared mutable request state from the web app
- Make usage tracking request-local instead of process-global
- Fix startup checks so the web runner only requires keys for the configured provider

### Operations

- Add `/healthz` and `/status`
- Report current provider, cache status, last import time, last profile build time, and last failure
- Expose version/build information in the UI

## Phase 2: Strong v1.0 Features

These features make the app materially more useful for daily homeserver use.

### Feedback loop

- Add `like`, `dislike`, `hide`, `mark watched`, and `save for later` actions directly on result cards
- Let users review and undo recent feedback from the web UI

### Recommendation workflow

- Add a dedicated watchlist page for saved recommendations
- Keep recommendation history easy to revisit and reuse
- Show clear result states:
  - in your library
  - available on your streaming services
  - available elsewhere
  - not currently available

### Import and data quality

- Add an import health dashboard with last sync time, item counts, unmatched titles, and override count
- Add a web UI for reviewing unmatched titles and editing overrides without hand-editing JSON
- Add scheduled refresh jobs for metadata, provider availability, and taste profile rebuilds

### Library workflow

- Add a local "add to library" queue even before external integrations land
- Add library gap analysis for titles that strongly fit the profile but are missing from the library

## Phase 3: Ecosystem Integrations

These integrations are the highest-value expansion path for a homeserver audience.

### Media servers

- Plex import and watch-state sync
- Jellyfin import and watch-state sync
- Emby import and watch-state sync

### Download and request tools

- Radarr handoff for movie recommendations
- Sonarr handoff for series recommendations
- Overseerr or Jellyseerr handoff for users who prefer a request workflow

### Metadata bridges

- Trakt import for users with existing watch history outside local media servers

### Follow-up automation

- Track saved titles and surface them when they become available in the local library
- Track saved titles and surface them when they become available on configured streaming services

## Priority Order

1. Deployment and runtime hardening
2. Background jobs and status visibility
3. Web-native feedback actions
4. Library-aware recommendation states
5. Plex and Jellyfin import
6. Radarr and Sonarr handoff
