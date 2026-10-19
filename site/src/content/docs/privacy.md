---
title: Privacy policy
description: What Streamline does with your data, including Google Drive access for backups.
---

Last updated: October 19, 2026.

Streamline is free, open-source software that you run on your own computer or server.
There is no Streamline service, account or company server: the people who make Streamline never receive your data.
This page explains where your data stays and which outside services Streamline talks to on your behalf.

## Data Streamline keeps

Your watch history, ratings, watchlist, followed shows, searches, settings and API keys are stored only on the machine where you run Streamline, in its `data/` folder and cache.
You can read, export or delete them at any time by working with those files directly.

## Outside services

Streamline only contacts these services, using keys and accounts you provide:

- **TMDB** (The Movie Database), for title details, posters and where to watch. Requests contain title names and IDs.
- **IMDb**, to download its public ratings file. Nothing about you is sent.
- **TheTVDB**, if you add a key, for show air times. Requests contain show IDs.
- **Your AI provider** (Anthropic, Google Gemini or OpenAI, whichever you choose), to understand searches and write recommendations. Requests contain your search, titles you have watched, and your taste profile. That provider's own privacy policy applies.
- **Plex**, if you connect it, on your own network, to read your plays and ratings.
- **Cloud storage for backups**, only if you turn it on (see below).

## Google Drive and other cloud backups

If you choose to back up to Google Drive (or another cloud service), Streamline uses [rclone](https://rclone.org/) on your server, signed in with your own Google account.

- **What it accesses:** only the backup folder you name. It uploads Streamline backup files there, lists that folder, and deletes older Streamline backups so only the newest few are kept.
- **What it does not do:** it does not read, change or share any other files in your Drive.
- **Where the sign-in is stored:** the access token stays in rclone's settings on your server. It is never sent to the people who make Streamline or to anyone else.
- **What a backup contains:** your Streamline database, settings, API keys and a part of the cache. Backups are not encrypted, so treat them like the keys inside them.
- **Turning it off:** clear the cloud field in Settings > Backups, delete the backup folder from your Drive if you like, and remove access in your [Google account permissions](https://myaccount.google.com/permissions).

Google user data obtained through Google APIs is used only to store and manage your own backups, is not transferred to anyone else, and is not used for advertising or to train AI models.

## This website

This documentation site is hosted on Cloudflare Pages.
It sets no cookies and has no analytics or tracking of its own; Cloudflare may keep standard server logs.

## Changes and contact

Changes to this policy are made in the open, in the [Streamline repository](https://github.com/abhichandra21/streamline).
Questions: open an issue at [github.com/abhichandra21/streamline/issues](https://github.com/abhichandra21/streamline/issues).
