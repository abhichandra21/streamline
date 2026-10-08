---
title: Command line
description: Every ./recommend and ./recommend-web command.
---

## Searching

```bash
./recommend "paranoid spy thriller like The Night Manager"
./recommend "why not Slow Horses?"
./recommend                              # interactive session
./recommend -n 5 "dark thriller"         # override the result count
./recommend --provider gemini "spy thriller"
./recommend --debug "spy thriller"       # full pipeline trace
./recommend --history                    # recent searches
```

Inside the interactive session: `+more Title`, `+fine Title`, `+less Title`, and `+add Title tv|movie`.

## Ratings and history

```bash
./recommend --more "Tinker Tailor Soldier Spy"   # Loved / more like this
./recommend --fine "The Night Agent"             # it was fine
./recommend --less "The Long Season"             # not for me / less like this
./recommend --add "Shetland" --type tv           # add to watch history
./recommend plex ratings                         # bring Plex rating changes over
```

## Setup

```bash
./recommend setup                        # first-time setup
./recommend setup --ingest-only          # check the export zips, no TMDB or LLM calls
./recommend setup --refresh-data         # re-read history, TMDB, descriptions; never the profile
./recommend setup --refresh-profile      # rebuild the taste profile
./recommend setup --rethink-themes       # regroup the taste rows from scratch (paid calls)
./recommend setup --refresh-imdb         # re-download IMDb ratings and Find's language lists
```

## Web UI

```bash
./recommend-web start                    # http://localhost:5051
./recommend-web stop
./recommend-web restart
./recommend-web status
./recommend-web logs
```

## Shell completion

```bash
source completions/recommend.bash        # bash
source completions/_recommend.zsh        # zsh
```
