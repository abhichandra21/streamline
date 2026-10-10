---
title: Configuration
description: Every setting, where it lives, and its default.
---

Settings live in a few places:

- **Environment variables** — secrets only (`TMDB_API_KEY`, `ANTHROPIC_API_KEY`, `GEMINI_API_KEY`, `OPENAI_API_KEY`, and for Plex `PLEX_WEBHOOK_TOKEN`, `PLEX_URL`, `PLEX_TOKEN`)
- **`.env`** — optional local convenience for setting those environment variables (gitignored)
- **`config.yaml`** — the shipped defaults, tracked in git
- **`config.local.yaml`** — your overrides, loaded after `config.yaml` and gitignored: watch-history paths, and everything saved from the Settings page

## LLM providers

```yaml
# config.yaml
provider: anthropic                    # or "gemini", "openai", or "local"

models:
  anthropic:
    fast: claude-haiku-4-5-20251001    # enrichment (high volume, cheap)
    reason: claude-sonnet-5-5          # intent, ranking, profile (complex reasoning)
  gemini:
    fast: gemini-2.5-flash
    reason: gemini-2.5-pro
  openai:
    fast: gpt-4.1-mini
    reason: gpt-4.1
  local:
    fast: gpt-oss:120b                 # any OpenAI-compatible endpoint (e.g. Ollama)
    reason: gpt-oss:120b
    base_url: http://localhost:11434/v1
    timeout_scale: 5                   # local inference is slower; scales llm.timeout_*
```

Switch providers by changing `provider:` in config or per-query with `--provider gemini`.

By default, each provider reads its API key from the standard environment variable name:

- Anthropic: `ANTHROPIC_API_KEY`
- Gemini: `GEMINI_API_KEY`
- OpenAI / compatible: `OPENAI_API_KEY`
- Local: uses the `openai` client against `models.local.base_url`; no key needed for most self-hosted servers

Only add `models.<provider>.api_key_env` in config when you need a non-standard variable name.

## Quality filters

```yaml
min_rating: 6.5      # minimum rating, IMDb first, TMDB fallback (0 to disable)
min_year: 2000        # minimum release year (0 to disable)
min_vote_count: 20    # filter obscure titles
```

Title overrides are covered on [Watch history](/guides/watch-history/#fixing-titles).

## All settings

All shared settings in `config.yaml`:

### LLM
| Setting | Default | Description |
|---------|---------|-------------|
| `provider` | anthropic | LLM provider ("anthropic", "gemini", "openai", or "local") |
| `models.*` | (see above) | Model assignments per provider (fast/reason roles) |
| `llm.timeout_*` | 30-300s | Per-call-type timeouts |
| `llm.tokens_*` | 200-16000 | Per-call-type max output tokens |
| `llm.profile_batch_size` | 200 | Titles per taste profile batch |
| `llm.rate_limit_wait` | 65 | Seconds to wait on rate limit |

### Mood Match
| Setting | Default | Description |
|---------|---------|-------------|
| `wizard.max_questions` | 5 | Hard cap on questions before the wizard must recommend |
| `wizard.min_questions` | 4 | Soft floor (incl. the content-type tap) before it may finish on its own; bounded by `max_questions` |
| `wizard.max_tokens` | 1200 | Output-token ceiling per wizard turn (the finalize turn emits a full intent) |

### Scoring
| Setting | Default | Description |
|---------|---------|-------------|
| `scoring.use_viewing_signals` | false | Off: every watched title counts the same. On: use the completion/rewatch/recency weights below |
| `scoring.weight_completion` | 0.5 | Weight for watch completion rate |
| `scoring.weight_rewatch` | 0.3 | Weight for rewatch bonus |
| `scoring.weight_recency` | 0.2 | Weight for recency (must sum to 1.0) |
| `scoring.default_tv_runtime` | 45 | Fallback TV episode runtime (minutes) |
| `scoring.default_movie_runtime` | 90 | Fallback movie runtime (minutes) |

### Recommendations
| Setting | Default | Description |
|---------|---------|-------------|
| `default_top_n` | 3 | Default results per query |
| `min_vote_count` | 20 | Minimum TMDB votes for discover candidates |
| `min_rating` | 6.5 | Minimum rating, IMDb first, TMDB fallback (0 to disable) |
| `min_year` | 2000 | Minimum release year (0 to disable) |
| `recency_half_life_days` | 90 | Days until recency score halves |
| `watch_region` | US | Region for streaming availability |
| `streaming_platforms` | [] | Your subscribed platforms |

## Environment variables

| Variable | Purpose |
|---|---|
| `TMDB_API_KEY` | Required. TMDB v3 API key |
| `ANTHROPIC_API_KEY`, `GEMINI_API_KEY`, `OPENAI_API_KEY` | The key for whichever provider you use |
| `PLEX_WEBHOOK_TOKEN`, `PLEX_URL`, `PLEX_TOKEN` | [Plex](/guides/plex/) |
| `STREAMLINE_PASSWORD` | Optional password for the web UI |
| `STREAMLINE_API_TOKEN` | Optional bearer token for `GET /api/*` |
| `STREAMLINE_PORT`, `STREAMLINE_HOST` | Where the web UI listens (default port `5051`) |
