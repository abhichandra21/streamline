# LLM-Powered Query-Driven Recommendation Engine

**Date:** 2026-04-01  
**Status:** Approved  

---

## Problem Statement

The current engine has three limitations:

- **(a) Too generic** — TMDB tags (genre, keyword) are shallow and miss tone, pacing, cultural flavor
- **(b) Candidate pool too narrow** — TMDB top-rated/popular lists are US/Western-biased, under-surfacing Bollywood, Indian originals, and niche British dramas
- **(c) Weak explanations** — "because you watched X" is just nearest-neighbor tag matching, not meaningful reasoning

Additionally, the batch "top 10 list" interface doesn't match actual usage. The real use case is conversational and query-driven:

> "Give me a good British crime drama I haven't watched before"  
> "Something like Catastrophe but not a sitcom"  
> "I started Tandav and stopped — is it worth finishing?"

---

## Approach: Two-Stage Query-Driven Pipeline

**Two phases:** offline setup (run once, cached) + online query handling (per request).

**Core insight:** Embedding averaging over a taste profile is lossy for a user with distinct, non-overlapping taste clusters (British prestige drama + Bollywood romance). Claude reasoning holistically over a natural-language taste profile preserves cluster identity. The two-stage recall → rank pattern controls cost while maintaining quality.

**Estimated cost:**
- One-time offline setup: ~$0.90 (Claude Haiku enrichment of ~2,700 titles)
- Per query: ~$0.06 (2 Claude Sonnet calls)

---

## What Gets Removed

The following existing modules become dead weight and are deleted:

- `recommender/taste_profile.py` — tag vector + L2 normalization replaced by Claude natural-language profile
- `recommender/engine.py` — dot product scorer and tag-based `recommend()` replaced by Claude Sonnet ranking

The following are rewritten:

- `recommender/main.py` — batch CLI replaced by query-driven interface

The following are partially modified:

- `recommender/tmdb_client.py` — `get_candidates()` (top-rated/popular pool) replaced by dynamic genre/language/country queries. `get_metadata()`, `_search()`, `_fetch_details()` unchanged.
- `config.py` — new keys added, `prime` path already set

The following are kept as-is:

- `recommender/ingestion/` — all parsers (Netflix, Prime, base)
- `recommender/signals.py` — implicit scoring (completion + rewatch + recency) feeds taste profile builder

---

## Architecture

### Offline Phase

Run once via `python -m recommender.setup`. Results cached to disk.

```
Netflix CSV + Prime CSV
        ↓
    ingestion/              existing parsers → WatchEvent list
        ↓
    signals.py              implicit scores per title (completion + rewatch + recency)
        ↓
    tmdb_client             fetch metadata for all watched titles
        ↓
    enricher.py             Claude Haiku writes 2-3 sentence semantic description
                            per title → cache
        ↓
taste_profile_builder.py    Claude Sonnet reads enriched history + scores
                            → writes natural-language taste profile
        ↓
watch_index.json            flat normalized set of all watched titles (all platforms)
```

### Online Phase

Runs per user query.

```
User natural language query
        ↓
query_engine.py → Claude Sonnet: parse intent
        → {genre, origin, mood, constraint, special_intent}
        ↓
tmdb_client: dynamic search by parsed filters (genre + country + language)
        → ~30-50 candidates with metadata
        ↓
cross-reference watch_index.json → exclude watched titles
        ↓
enricher.py: fetch cached descriptions; generate on-the-fly for any missing
        ↓
Claude Sonnet: taste_profile.txt + query + candidate descriptions
        → ranked recommendations + specific personal explanations
        ↓
Output to terminal
```

---

## Components & Interfaces

### `recommender/enricher.py` (new)

```python
def enrich(title: str, metadata: TmdbMetadata) -> str
    # Claude Haiku: title + TMDB metadata → 2-3 sentence semantic description

def enrich_batch(titles_metadata: dict[str, TmdbMetadata]) -> dict[str, str]
    # Batch version, skips already-cached titles
```

Cache: `recommender/cache/enrichments/{content_type}/{tmdb_id}.txt`  
Fallback cache key (title not found in TMDB): `recommender/cache/enrichments/unknown/{slugified_title}.txt`

### `recommender/taste_profile_builder.py` (new)

```python
def build(events: list[WatchEvent], scores: dict[str, float],
          enrichments: dict[str, str]) -> str
    # Claude Sonnet: enriched watch history weighted by implicit scores
    # → natural language taste profile saved to disk
```

Output: `recommender/cache/taste_profile.txt`  
Refresh: `python -m recommender.setup --refresh-profile`

### `recommender/query_engine.py` (new)

```python
def ask(query: str, taste_profile: str, watch_index: set[str]) -> list[Recommendation]
    # 1. Claude Sonnet: parse query intent
    # 2. TMDB dynamic search by filters
    # 3. Exclude watched titles via watch_index
    # 4. Enrich any missing candidates
    # 5. Claude Sonnet: rank + explain
```

Special intent routing:
- Abandoned titles query → direct watch history lookup, not TMDB search
- Watchlist query → cross-reference Prime watchlist data

### `recommender/watch_index.py` (new)

```python
def build(events: list[WatchEvent]) -> set[str]
    # Normalized set of all watched titles across platforms
    # Normalization: lowercase + strip parentheticals + strip edition suffixes
```

Output: `recommender/cache/watch_index.json`

### `recommender/models.py` (new)

Holds the `Recommendation` dataclass (moved out of the deleted `engine.py`):

```python
@dataclass
class Recommendation:
    title: str
    content_type: str
    score: float
    vote_average: float
    genres: list[str]
    explanation: str        # replaces because_you_watched — Claude-generated
```

### `recommender/setup.py` (new)

Orchestrates the full offline phase:
```bash
python -m recommender.setup              # run all offline steps
python -m recommender.setup --refresh-profile  # rebuild taste profile only
```
Steps: load events → compute scores → fetch TMDB metadata → enrich → build taste profile → build watch index.

### `recommender/main.py` (rewritten)

Two modes:
```bash
# Single-shot
python -m recommender "good British crime drama I haven't watched"

# Interactive loop
python -m recommender
```

### `config.py` additions

```python
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")  # set in ~/.bashrc
ENRICHMENT_CACHE_DIR = "recommender/cache/enrichments"
TASTE_PROFILE_PATH = "recommender/cache/taste_profile.txt"
WATCH_INDEX_PATH = "recommender/cache/watch_index.json"
```

---

## Data Flow Details

### Taste Profile Format (example)

```
You have two strong, distinct taste clusters:

1. British prestige drama: slow-burn, morally complex, high production value.
   Anchored by Downton Abbey, Catastrophe, The Night Manager, Fleabag,
   A Very English Scandal. You prefer character-driven plots over procedural ones.

2. Bollywood romance: emotionally warm films from the 90s-2000s. Strong preference
   for classics (DDLJ, Jab Tak Hai Jaan, Yeh Jawaani Hai Deewani). You revisit
   these — DDLJ has 6 watch sessions.

Secondary interests: Indian crime originals (Mirzapur, Paatal Lok, Made In Heaven),
investigative documentaries (Oppenheimer, She Said, The Report), family-friendly
content (Just Add Magic, Dino Dana).

You consistently finish what you start (>90% completion on most titles).
You rarely engage with pure action or horror.
```

### Query Intent Structure

```python
@dataclass
class QueryIntent:
    genres: list[str]           # ["crime", "drama"]
    origin_countries: list[str] # ["GB"]
    languages: list[str]        # ["en"]
    mood_descriptors: list[str] # ["slow-burn", "gripping"]
    similar_to: list[str]       # ["Night Manager"]
    max_runtime_minutes: int | None
    unwatched_only: bool        # default True
    special_intent: str | None  # "abandoned", "watchlist", "family"
```

### TMDB Fallback for Sparse Results

If dynamic TMDB search returns fewer than 10 candidates after watch exclusion, Claude generates 20 specific title suggestions → each validated through TMDB lookup → failures silently dropped → survivors added to candidate pool.

---

## Error Handling

| Scenario | Handling |
|----------|----------|
| TMDB returns < 10 candidates | Claude generates title suggestions → TMDB validates |
| Claude Haiku enrichment fails | Fall back to TMDB genres + keywords + overview concatenated |
| Title matching ambiguity across platforms | Normalize before indexing; err toward exclusion |
| Stale taste profile | Rebuilt only on `--refresh-profile`; acceptable given deep history |
| Abandoned titles query | Routes to direct watch history lookup, bypasses TMDB search |
| Claude API rate limit | Exponential backoff, max 3 retries |

---

## Example Queries & Expected Behavior

| Query | Intent parsed | Route |
|-------|--------------|-------|
| "good British crime drama I haven't watched" | genre=crime/drama, origin=GB, unwatched=true | TMDB search → rank |
| "something like Catastrophe" | similar_to=Catastrophe, mood=witty/intimate | TMDB search → rank |
| "feel-good Bollywood movie from the 90s" | genre=romance, language=hi, era=1990s | TMDB search → rank |
| "I started Tandav and stopped, worth finishing?" | special_intent=abandoned, title=Tandav | Watch history lookup |
| "something the whole family can watch" | special_intent=family | TMDB search, family filter → rank |
| "what's on my watchlist I should actually watch?" | special_intent=watchlist | Watchlist cross-reference → rank |

---

## Models Used

| Task | Model | Rationale |
|------|-------|-----------|
| Title enrichment (offline) | Claude Haiku | High volume, simple task, cost-sensitive |
| Taste profile build (offline) | Claude Sonnet | Requires nuanced synthesis |
| Query intent parsing (online) | Claude Sonnet | Needs reliable structured extraction |
| Final ranking + explanation (online) | Claude Sonnet | Core quality-determining step |
