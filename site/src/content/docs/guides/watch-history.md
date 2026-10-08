---
title: Watch history
description: Where Streamline gets your history, and how to fix titles it can't match.
---

## Exports

Streamline reads the data exports each service gives you under privacy law.
None of these services has a public API for watch history, and scraping breaks their terms and breaks often.
The exports are legitimate, stable, and complete.

| Service | Path in `config.local.yaml` | How to get it |
|---|---|---|
| Netflix | `platform_paths.netflix` | Account Settings > Download your data |
| Prime Video | `platform_paths.prime` | Account > Digital content > Request your data |
| Apple TV | `platform_paths.apple_tv` | Apple's privacy site: Apple Media Services Information |

Point each path at the zip exactly as downloaded.
After adding a newer export, run `./recommend setup --refresh-data`.

A routine data refresh never rebuilds the taste profile.
It does write descriptions for new titles, which are fast-model calls.

## Plex

Plex sends plays as they happen; see [Plex](/guides/plex/).

## Manual lists

Put one title per line in `data/manual/tv.csv` and `data/manual/movies.csv`.
A movie title may end with its year (`Zodiac 2007`), which helps matching.
You can also add a single title with `./recommend --add "Shetland" --type tv`, or **Add watched** in the Archive.

## Fixing titles

When TMDB can't match a title, or matches the wrong one, add it to `data/overrides.json`:

```json
{
  "The Matrix III Revolutions": {"title": "The Matrix Revolutions"},
  "Some Cooking Show Ep 3": {"skip": true},
  "Delhi Cops Episode": {"title": "Delhi Cops", "content_type": "tv"},
  "Specific Movie": {"tmdb_id": 12345}
}
```

Each entry can set the `title` to search for, the `content_type` (`tv` or `movie`), an exact `tmdb_id`, or `skip` to leave the title out.
The next `./recommend setup` notices the change.

## Where it is stored

Watch events, ratings, the watchlist, follows, and search history all live in one SQLite file, `data/streamline.db`.
Plex plays exist only there, so back that file up.
