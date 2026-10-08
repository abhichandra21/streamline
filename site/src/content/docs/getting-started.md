---
title: Getting started
description: Install Streamline, add your keys, run setup, and ask for a first recommendation.
---

## What you need

- Python 3.10 or newer
- A free [TMDB API key](https://www.themoviedb.org/settings/api)
- A key for one LLM provider: Anthropic, Google Gemini, or OpenAI. A local OpenAI-compatible server such as Ollama also works, with no key.
- At least one watch-history export. See [Watch history](/guides/watch-history/) for how to get them.

## Install

```bash
git clone https://github.com/abhichandra21/streamline.git
cd streamline
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

## Add your keys

Keys are read from the environment.
A `.env` file in the repo folder is a convenient way to set them, and is gitignored.

```bash
TMDB_API_KEY=your_key_here
ANTHROPIC_API_KEY=your_key_here
```

## Point it at your exports

Copy `config.local.example.yaml` to `config.local.yaml` and set the path to each export zip you have.
Set a provider to `null` if you don't use it.

```yaml
platform_paths:
  netflix: data/netflix/export.zip
  prime: data/prime_video/Prime Video.zip
  apple_tv: null
```

Run `./recommend setup --ingest-only` to check the zips are readable before the real setup.

## Run setup

```bash
./recommend setup
```

Setup reads your history, matches every title on TMDB, writes a short description of each title with the fast model, and builds your taste profile with the reasoning model.
The first run makes one fast-model call per title, so a large history takes a while and costs a little.
Descriptions are cached, so later runs only pay for new titles.

## Ask for something

```bash
./recommend "good British crime drama"
```

Or start the web UI and open [http://localhost:5051](http://localhost:5051):

```bash
./recommend-web start
```

## Teach it your taste

The taste profile is built from titles you mark **Loved**, not from everything you watched.
Open **Archive**, then **Rate It**, tap the titles you loved, and run:

```bash
./recommend setup --refresh-profile
```

See [Archive, Seen It, and Rate It](/guides/archive/) for more.
