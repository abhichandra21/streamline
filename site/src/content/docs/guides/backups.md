---
title: Backups
description: Daily backups of your ratings, follows and settings, to the server, a second folder, and cloud storage.
---

Your ratings, follows, watchlist, Seen it marks and watch history live in one database on the machine that runs Streamline.
Nothing else has a copy, so Streamline backs it up for you.

## What happens on its own

While the web app runs, it makes one backup a day.
**Settings > Backups** shows when the last one ran, whether each copy worked, and lists the saved backups with a download link each.
**Back up now** makes one straight away.

Each backup is one zip file holding:

- the database (`data/streamline.db`): ratings, follows, watchlist, Seen it, searches and imported watch history
- your settings (`config.local.yaml`)
- your keys: the ones saved on the Settings page (`data/keys.env`) and `.env`
- title overrides (`data/overrides.json`) and manual lists (`data/manual/`)
- what the AI wrote, which would cost money to rebuild and come out different: your taste profile, taste rows and the notes on each title (`recommender/cache/taste_*`, `recommender/cache/enrichments/`)
- the watch index and TMDB's details for each title (`recommender/cache/watch_index.json`, `recommender/cache/tmdb/movie/`, `recommender/cache/tmdb/tv/`), so a restored copy works without your watch-history exports

The rest of the cache (IMDb ratings, streaming providers, release dates) is left out; Streamline fetches it again on its own.
A backup is about 50 MB.

:::caution
Backups are not encrypted. Anyone who can open the zip can read your keys.
:::

## Where backups go

Every place keeps the newest 7 backups by default (change **Keep** on the page). Older ones are deleted, but only files Streamline made.

1. **On the server**, always: `data/backups/`.
2. **A second folder**, optional: any folder the server can see, such as a mounted NAS share or a USB drive. Enter its full path, for example `/mnt/nas/streamline`.
3. **Cloud storage**, optional, through [rclone](https://rclone.org/): Google Drive, Dropbox, OneDrive, S3, Backblaze and many more.

If a copy fails (a drive not mounted, a cloud sign-in that expired), the backup on the server is still saved, and Settings shows what went wrong.
If the backup itself fails, for example on a full disk, Settings says so next to the last good one, and Streamline tries again every hour.

### Setting up Google Drive (or another cloud)

Do this once, in a terminal on the server.
If rclone is not installed yet, install it first (`sudo apt install rclone` on Debian and Ubuntu, or see [rclone's install page](https://rclone.org/install/)).

```bash
rclone config
```

Choose **n** (new remote), name it, for example `gdrive`, and pick **drive** for Google Drive.
Accept the defaults, and sign in to Google when asked.
On a server with no browser, answer **n** to "Use web browser to automatically authenticate", and rclone tells you the command to run on a computer that has one.

Then, in **Settings > Backups**, enter the remote name and a folder, like `gdrive:Streamline`, and save.
**Back up now** checks it straight away.

### With Docker

The image includes rclone and keeps its settings in `data/rclone.conf`, inside the data mount. Set it up with:

```bash
docker compose run --rm streamline rclone config
```

For a second folder, mount it into the container in `compose.yaml` (for example `- /mnt/nas/streamline:/backups`) and enter the path inside the container, `/backups`.

## From the terminal

```bash
./recommend backup
```

makes a backup and sends it to the same places as the daily one.
It is handy in your own cron job if the web app is not always running.

## Restoring

1. Stop Streamline (`./recommend-web stop`, `sudo systemctl stop streamline-web`, or `docker compose down`).
2. Delete the database's side files if they are there, then unzip the backup into the Streamline folder. Every file goes back to its usual place:

   ```bash
   cd ~/streamline
   rm -f data/streamline.db-wal data/streamline.db-shm
   unzip -o data/backups/streamline-backup-20261019-030000.zip
   ```

3. Start Streamline again.
