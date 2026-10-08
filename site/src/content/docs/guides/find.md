---
title: Find
description: The best-rated titles you haven't seen, filtered and sorted, with no LLM involved.
---

**Find** lists unwatched titles straight from TMDB, for a few filters you choose.
It makes no LLM calls and ignores your taste profile, so it is fast, free, and predictable.

![Find page with filters and the top unwatched films of the last six months](../../../assets/screenshots/find.jpg)

## Filters

- **Type**: movies or TV
- **Released in the last**: 30 days up to 10 years
- **Genre** and an exact **TMDB keyword**
- **Minimum rating**
- **Sort by**: rating, newest, and more

Titles you've watched, saved, or marked Not interested are left out.
Scroll to keep going; the list continues as far as TMDB does.

## Original-language lists

Set **Original language** to Hindi for a different list.
Streamline builds it in the background from every TMDB page for that language over 10 years, attaches IMDb ratings, and sorts by IMDb.
Titles need at least 500 IMDb votes.
The list is built the first time you ask for it and rebuilt daily, or on demand with `./recommend setup --refresh-imdb`.

The main list is ordered by TMDB; the language lists by IMDb.
Both ratings are shown on every row.
