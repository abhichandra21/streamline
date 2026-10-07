"""Structured taste profile generation and query-time slicing."""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
from pathlib import Path
from typing import Any

import config
from .ingestion.base import WatchEvent
from .llm import LLMClient
from .signals import STRONG_WEIGHT
from .taste_profile_builder import _EQUAL_WEIGHT_NOTE, history_label, sort_scored

log = logging.getLogger("recommender.structured_profile")

ALLOWED_CO_VIEWING = {"personal", "family", "mixed", "unknown"}
# Every loved or followed title is sent; this many more fill in from the rest.
STRUCTURED_EXTRA_TITLES = 300
# Descriptions are cut to about this many characters to keep the prompt small.
# Clusters past this are dropped, with their titles, so the prompt asks for no more.
MAX_CLUSTERS = 16
STRUCTURED_DESCRIPTION_CHARS = 400
# Weight for a cluster whose member numbers were all unusable.
EMPTY_CLUSTER_WEIGHT = 0.05
FAMILY_WEIGHT_MULTIPLIER = 0.75
FAMILY_TERMS = {
    "animation",
    "animated",
    "cartoon",
    "child",
    "children",
    "family",
    "holiday",
    "kid",
    "kids",
}

DEFAULT_STRUCTURED_PROFILE: dict[str, Any] = {
    "version": 1,
    "clusters": [],
    "mood_states": [],
    "creator_affinities": [],
    "language_region_affinities": [],
    "negative_preferences": [],
}


def _strip_json_fence(text: str) -> str:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\n?", "", cleaned)
        cleaned = re.sub(r"\n?```$", "", cleaned)
    return cleaned.strip()


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _clean_string(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def _clean_string_list(value: Any, limit: int = 12) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in _as_list(value):
        text = _clean_string(item)
        key = text.casefold()
        if text and key not in seen:
            seen.add(key)
            result.append(text)
        if len(result) == limit:
            break
    return result


def _clamp_weight(value: Any, default: float = 0.5) -> float:
    try:
        weight = float(value)
    except (TypeError, ValueError):
        weight = default
    return max(0.0, min(1.0, round(weight, 3)))


def _slug(value: str, fallback: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug or fallback


def _clean_named_items(value: Any, limit: int = 12) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for index, raw in enumerate(_as_list(value)):
        if not isinstance(raw, dict):
            continue
        label = _clean_string(raw.get("label") or raw.get("name") or raw.get("id") or raw.get("description"))
        if not label:
            continue
        item: dict[str, Any] = {
            "id": _clean_string(raw.get("id")) or _slug(label, f"item-{index + 1}"),
            "label": label,
        }
        if "name" in raw:
            item["name"] = _clean_string(raw.get("name"))
        if "weight" in raw:
            item["weight"] = _clamp_weight(raw.get("weight"))
        if "traits" in raw:
            item["traits"] = _clean_string_list(raw.get("traits"))
        if "clusters" in raw:
            item["clusters"] = _clean_string_list(raw.get("clusters"))
        if "languages" in raw:
            item["languages"] = _clean_string_list(raw.get("languages"))
        if "regions" in raw:
            item["regions"] = _clean_string_list(raw.get("regions"))
        if "applies_to" in raw:
            item["applies_to"] = _clean_string_list(raw.get("applies_to"))
        items.append(item)
        if len(items) == limit:
            break
    return items


def validate_structured_profile(data: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise ValueError("structured profile JSON must be an object")

    profile = {key: value.copy() if isinstance(value, list) else value
               for key, value in DEFAULT_STRUCTURED_PROFILE.items()}
    profile["version"] = 1

    clusters: list[dict[str, Any]] = []
    seen_titles: set[str] = set()
    for index, raw in enumerate(_as_list(data.get("clusters"))):
        if not isinstance(raw, dict):
            continue
        label = _clean_string(raw.get("label") or raw.get("id"))
        if not label:
            continue
        co_viewing = _clean_string(raw.get("co_viewing")).lower() or "unknown"
        if co_viewing not in ALLOWED_CO_VIEWING:
            co_viewing = "unknown"

        representative_titles: list[str] = []
        for title in _clean_string_list(raw.get("representative_titles"), limit=20):
            key = title.casefold()
            if key in seen_titles:
                continue
            seen_titles.add(key)
            representative_titles.append(title)

        clusters.append({
            "id": _clean_string(raw.get("id")) or _slug(label, f"cluster-{index + 1}"),
            "label": label,
            "name": _clean_string(raw.get("name")),
            "weight": _clamp_weight(raw.get("weight")),
            "description": _clean_string(raw.get("description")),
            "positive_traits": _clean_string_list(raw.get("positive_traits")),
            "negative_traits": _clean_string_list(raw.get("negative_traits")),
            "co_viewing": co_viewing,
            "mood_states": _clean_string_list(raw.get("mood_states")),
            "languages": _clean_string_list(raw.get("languages")),
            "regions": _clean_string_list(raw.get("regions")),
            "representative_titles": representative_titles,
            "members": _clean_string_list(raw.get("members"), limit=400),
        })
        if len(clusters) == MAX_CLUSTERS:
            break

    profile["clusters"] = clusters
    profile["mood_states"] = _clean_named_items(data.get("mood_states"))
    profile["creator_affinities"] = _clean_named_items(data.get("creator_affinities"))
    profile["language_region_affinities"] = _clean_named_items(data.get("language_region_affinities"))
    profile["negative_preferences"] = _clean_named_items(data.get("negative_preferences"))
    return profile


def _deprioritize_family_cluster_weights(profile: dict[str, Any]) -> dict[str, Any]:
    adjusted = {
        key: value.copy() if isinstance(value, list) else value
        for key, value in profile.items()
    }
    adjusted_clusters: list[dict[str, Any]] = []
    for cluster in profile["clusters"]:
        adjusted_cluster = cluster.copy()
        if adjusted_cluster["co_viewing"] == "family":
            adjusted_cluster["weight"] = _clamp_weight(
                adjusted_cluster["weight"] * FAMILY_WEIGHT_MULTIPLIER
            )
        adjusted_clusters.append(adjusted_cluster)
    adjusted["clusters"] = adjusted_clusters
    return adjusted


def _resolve_members(data: dict[str, Any], scored: list[tuple[str, float]]) -> None:
    """Turn each cluster's member numbers into titles, one cluster per title.

    Numbers that are out of range, repeated, or not numbers are ignored.
    """
    claimed: set[int] = set()
    for raw in _as_list(data.get("clusters")):
        if not isinstance(raw, dict):
            continue
        titles = []
        for number in _as_list(raw.get("members")):
            try:
                index = int(number) - 1
            except (TypeError, ValueError):
                continue
            if 0 <= index < len(scored) and index not in claimed:
                claimed.add(index)
                titles.append(scored[index][0])
        raw["members"] = titles


# A cluster tied to one of these countries joins any other cluster tied to it.
REGION_NAMES = {"GB": "British", "IN": "Indian", "KR": "Korean", "JP": "Japanese",
                "ES": "Spanish", "FR": "French", "DE": "German", "IT": "Italian"}


def merge_region_clusters(profile: dict[str, Any]) -> dict[str, Any]:
    """Merge personal clusters that belong to the same single country.

    The model is asked for one cluster per strong country but can split it
    (British thrillers, British cozy mysteries, British period drama); split, each
    part sorts low although together they are one of the household's biggest tastes.
    """
    merged: list[dict[str, Any]] = []
    by_region: dict[str, dict[str, Any]] = {}
    for cluster in profile["clusters"]:
        regions = cluster.get("regions") or []
        region = regions[0].upper() if len(regions) == 1 else ""
        if region not in REGION_NAMES or cluster["co_viewing"] == "family":
            merged.append(cluster)
            continue
        if region not in by_region:
            by_region[region] = {**cluster, "parts": [cluster["label"]], "name_size": len(cluster["members"])}
            merged.append(by_region[region])
            continue
        into = by_region[region]
        into["parts"].append(cluster["label"])
        # The merged cluster keeps the display name of its biggest part.
        if cluster.get("name") and len(cluster["members"]) > into["name_size"]:
            into["name"], into["name_size"] = cluster["name"], len(cluster["members"])
        for key in ("members", "representative_titles", "positive_traits", "negative_traits",
                    "mood_states", "languages"):
            into[key] = list(dict.fromkeys(into.get(key, []) + cluster.get(key, [])))
        into["description"] = " ".join(d for d in (into.get("description"), cluster.get("description")) if d)
    for cluster in by_region.values():
        parts = cluster.pop("parts")
        cluster.pop("name_size")
        if len(parts) > 1:
            name = REGION_NAMES[cluster["regions"][0].upper()]
            short = [re.sub(rf"^{name}\s+(and \w+\s+)?", "", p, flags=re.IGNORECASE) for p in parts]
            cluster["label"] = f"{name}: " + "; ".join(short)
    return {**profile, "clusters": merged}


def explicit_dislikes(titles: list[str] | None, shown: int = 25) -> list[dict[str, Any]]:
    """The household's own Not for me titles, as the only negative preference."""
    if not titles:
        return []
    listed = ", ".join(titles[:shown]) + (f" and {len(titles) - shown} more" if len(titles) > shown else "")
    return [{"id": "not-for-me", "label": f"Titles marked Not for me: {listed}", "weight": 1.0}]


def apply_member_weights(profile: dict[str, Any], scores: dict[str, float]) -> dict[str, Any]:
    """Set each cluster's weight from its members' summed scores, then order clusters.

    The top cluster gets 1.0 and the rest are relative to it, so the order comes
    from the household's data rather than the model's guess. Family clusters are
    discounted and always sort after personal ones.
    """
    sums = [sum(scores.get(title, 0.0) for title in c["members"]) for c in profile["clusters"]]
    top = max(sums, default=0.0)
    clusters = []
    for cluster, total in zip(profile["clusters"], sums):
        weight = total / top if top and total else EMPTY_CLUSTER_WEIGHT
        clusters.append({**cluster, "weight": _clamp_weight(weight)})
    adjusted = _deprioritize_family_cluster_weights({**profile, "clusters": clusters})
    adjusted["clusters"].sort(key=lambda c: (c["co_viewing"] == "family", -c["weight"]))
    return adjusted


def parse_structured_profile_response(
    text: str,
    scored: list[tuple[str, float]] | None = None,
) -> dict[str, Any]:
    try:
        data = json.loads(_strip_json_fence(text))
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid structured profile JSON: {exc}") from exc
    if scored is None:
        return _deprioritize_family_cluster_weights(validate_structured_profile(data))
    if isinstance(data, dict):
        _resolve_members(data, scored)
    return apply_member_weights(merge_region_clusters(validate_structured_profile(data)), dict(scored))


def _short(text: str) -> str:
    """One line of description, cut at a word boundary near STRUCTURED_DESCRIPTION_CHARS.

    Drops a leading "# Title" heading, which only repeats the title.
    """
    text = re.sub(r"^\s*#[^\n]*\n", "", text)
    text = " ".join(text.split())
    if len(text) <= STRUCTURED_DESCRIPTION_CHARS:
        return text
    return text[:STRUCTURED_DESCRIPTION_CHARS].rsplit(" ", 1)[0] + "..."


def structured_prompt(
    events: list[WatchEvent],
    scores: dict[str, float],
    enrichments: dict[str, str],
    negative_prefs: list[str] | None = None,
    previous_names: list[str] | None = None,
) -> tuple[str, list[tuple[str, float]]]:
    """The structured-profile prompt and the numbered titles it lists.

    Returns ("", []) when no title has an enrichment.
    """
    scored = sort_scored(events, scores, enrichments)
    strong = [item for item in scored if item[1] >= STRONG_WEIGHT]
    scored = strong + [item for item in scored if item[1] < STRONG_WEIGHT][:STRUCTURED_EXTRA_TITLES]
    if not scored:
        return "", []
    lines = [
        f"{number}. {title} (score: {score:.2f}): {_short(enrichments[title])}"
        for number, (title, score) in enumerate(scored, start=1)
    ]
    less_like = ", ".join(f'"{title}"' for title in (negative_prefs or [])) or "none"
    keep_names = (
        "Names used last time: " + "; ".join(f'"{name}"' for name in previous_names) + ". "
        "Reuse a name when its cluster is essentially the same group of titles.\n"
        if previous_names else ""
    )
    prompt = (
        "Create a compact structured JSON taste profile for a personal streaming recommender.\n"
        + (
            "Use the engagement scores to separate strong taste signals from incidental watches.\n"
            if config.USE_VIEWING_SIGNALS else _EQUAL_WEIGHT_NOTE
        ) +
        "When a country or language runs through many titles across genres (for example British "
        "or Hindi series), give it its own cluster instead of spreading it across genre clusters.\n"
        "Capture specific taste clusters, positive traits, negative traits, co-viewing context, "
        "mood states, creator affinities, language or region affinities, and explicit dislikes.\n"
        "Return ONLY valid JSON with keys: version, clusters, mood_states, creator_affinities, "
        "language_region_affinities, negative_preferences.\n"
        "For each cluster include: id, label, name, description, weight, positive_traits, negative_traits, "
        "co_viewing, mood_states, languages, regions, representative_titles, members.\n"
        "label is a plain descriptive label used for search, like \"British cozy and procedural mysteries\".\n"
        "name is the headline the household sees: 2-6 words, warm and plain-spoken, like a friend "
        "describing them. Not a joke or pun, and not a dry genre label. Examples of the right voice: "
        "\"Cosy British mysteries\", \"Hindi stories that feel like home\", \"Sci-fi that bends your brain\", "
        "\"Eating your way around the world\", \"History that leaves a mark\", \"Life behind palace walls\".\n"
        + keep_names +
        "description is 2-3 sentences in second person, written like a friend who has just figured "
        "this household out: playful, specific and affectionate, naming several of their titles. "
        "Not a report and not a personality test. Example: \"Your ideal crime has a village, a vicar, "
        "and someone pouring tea before the second body turns up. Grantchester and Father Brown handle "
        "the cosy end; Happy Valley and Line of Duty remind you the British can be properly terrifying.\"\n"
        "members lists the numbers of every history title in that cluster. Put each title "
        f"in exactly one cluster, using at most {MAX_CLUSTERS} clusters. Every number from 1 to {len(scored)} "
        "must appear; if a group of titles fits none of your clusters, give it a cluster of its own.\n"
        "Use ISO-639-1 language codes like hi, en, ko, es, fr when known. "
        "Use ISO-3166 alpha-2 region codes like IN, GB, US, KR when known.\n"
        "Cluster weight and order are computed from members and the scores, so any cluster weight you give is ignored. "
        "Use co_viewing family only for children's and kids' titles; a family story for adults is personal.\n"
        "creator_affinities entries must include weight, traits, and clusters. "
        "Traits should explain what the user responds to in that creator's work, not just repeat the name.\n"
        "language_region_affinities entries must include weight, languages, regions, traits, and applies_to. "
        "For example, use languages ['hi'] and regions ['IN'] for a Hindi and Indian cinema affinity.\n"
        "negative_preferences entries must include label, weight and applies_to, and should include explicit dislikes first. "
        + (
            "Only infer cautious anti-patterns when repeated low-engagement evidence supports them; otherwise return an empty list.\n"
            if config.USE_VIEWING_SIGNALS else
            "Do not infer anti-patterns beyond explicit dislikes and \"Less like this\" feedback; otherwise return an empty list.\n"
        ) +
        "Use co_viewing only as one of: personal, family, mixed, unknown.\n"
        "Use weights from 0.0 to 1.0. Do not invent titles that are not in the history.\n\n"
        # Watched-to-the-end titles the user wants less of, which is a weaker
        # signal than dislike and should not be read as one.
        f"Titles the user asked to see less like: {less_like}\n\n"
        f"Watch history sorted by {history_label()}:\n"
        + "\n".join(lines)
    )
    return prompt, scored


def build_structured_profile(
    events: list[WatchEvent],
    scores: dict[str, float],
    enrichments: dict[str, str],
    client: LLMClient,
    negative_prefs: list[str] | None = None,
    previous_names: list[str] | None = None,
) -> dict[str, Any]:
    prompt, scored = structured_prompt(events, scores, enrichments, negative_prefs, previous_names)
    if not scored:
        log.warning("No enriched titles found for structured profile build; returning empty profile")
        return validate_structured_profile({})

    response_text = client.generate(
        prompt,
        role="reason",
        max_tokens=config.TOKENS_PROFILE_MERGE,
        timeout=config.TIMEOUT_PROFILE_MERGE,
    )
    # Kept for diagnosis: a bad answer is otherwise invisible.
    try:
        Path(config.STRUCTURED_TASTE_PROFILE_PATH).with_suffix(".response.txt").write_text(response_text)
    except OSError as exc:
        log.warning("Could not save the structured profile response: %s", exc)
    response_text = _place_unplaced_titles(response_text, scored, enrichments, client)
    profile = parse_structured_profile_response(response_text, scored)
    # Inferred dislikes contradicted the household's loves, so only their own count.
    profile["negative_preferences"] = explicit_dislikes(negative_prefs)
    if not profile["clusters"]:
        raise ValueError("the model returned no taste clusters")
    return profile


def _place_unplaced_titles(
    response_text: str,
    scored: list[tuple[str, float]],
    enrichments: dict[str, str],
    client: LLMClient,
) -> str:
    """Ask the fast model to file the titles the profile answer left out.

    The reasoning model drops hundreds of titles from its member lists, and cluster
    order comes from members, so leaving them out skews the order. Returns the
    answer unchanged when nothing is missing or the follow-up fails.
    """
    try:
        data = json.loads(_strip_json_fence(response_text))
    except json.JSONDecodeError:
        return response_text
    clusters = [c for c in _as_list(data.get("clusters") if isinstance(data, dict) else None)
                if isinstance(c, dict)][:MAX_CLUSTERS]
    if not clusters:
        return response_text
    placed: set[int] = set()
    for cluster in clusters:
        for number in _as_list(cluster.get("members")):
            try:
                placed.add(int(number))
            except (TypeError, ValueError):
                continue
    unplaced = [n for n in range(1, len(scored) + 1) if n not in placed]
    if not unplaced:
        return response_text
    log.info("Structured profile left %d of %d titles unplaced; filing them", len(unplaced), len(scored))
    prompt = (
        "These are a household's taste clusters:\n"
        + "\n".join(f"C{i}: {_clean_string(c.get('label'))}" for i, c in enumerate(clusters, start=1))
        + "\n\nPut each title below in the cluster it fits best. Leave out a title only if it "
        "fits none of them. Return ONLY JSON mapping cluster to title numbers, like "
        "{\"C1\": [12, 40], \"C2\": [7]}.\n\n"
        + "\n".join(f"{n}. {scored[n - 1][0]}: {_short(enrichments.get(scored[n - 1][0], ''))[:200]}"
                    for n in unplaced)
    )
    try:
        answer = json.loads(_strip_json_fence(client.generate(
            prompt, role="fast", max_tokens=8000, timeout=config.TIMEOUT_PROFILE_MERGE)))
    except Exception as exc:
        log.warning("Could not file the unplaced profile titles: %s", exc)
        return response_text
    if not isinstance(answer, dict):
        return response_text
    wanted = set(unplaced)
    for key, numbers in answer.items():
        index = str(key).lstrip("Cc")
        if not index.isdigit() or not 1 <= int(index) <= len(clusters):
            continue
        cluster = clusters[int(index) - 1]
        for number in _as_list(numbers):
            if isinstance(number, int) and number in wanted:
                wanted.discard(number)
                cluster["members"] = _as_list(cluster.get("members")) + [number]
    return json.dumps(data)


def save_structured_profile(profile: dict[str, Any], path: str | Path) -> None:
    normalized = validate_structured_profile(profile)
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", dir=destination.parent, delete=False, suffix=".tmp") as f:
        f.write(json.dumps(normalized, indent=2, sort_keys=True))
        tmp = f.name
    os.replace(tmp, destination)


def load_structured_profile(path: str | Path) -> dict[str, Any] | None:
    source = Path(path)
    if not source.exists():
        return None
    try:
        return validate_structured_profile(json.loads(source.read_text()))
    except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
        log.warning("Ignoring invalid structured taste profile at %s: %s", source, exc)
        return None


def _intent_terms(intent: Any) -> set[str]:
    values: list[str] = []
    for attr in ("genres", "mood_descriptors", "similar_to", "platforms"):
        values.extend(_clean_string_list(getattr(intent, attr, []), limit=40))
    content_type = _clean_string(getattr(intent, "content_type", ""))
    if content_type and content_type != "both":
        values.append(content_type)
    special_intent = _clean_string(getattr(intent, "special_intent", ""))
    if special_intent:
        values.append(special_intent)
    terms: set[str] = set()
    for value in values:
        terms.add(value.casefold())
        for part in re.split(r"[^a-zA-Z0-9]+", value):
            if part:
                terms.add(part.casefold())
    return terms


def _intent_locale_terms(intent: Any) -> tuple[set[str], set[str]]:
    languages = {
        value.casefold()
        for value in _clean_string_list(getattr(intent, "languages", []), limit=40)
    }
    regions = {
        value.casefold()
        for value in _clean_string_list(getattr(intent, "origin_countries", []), limit=40)
    }
    return languages, regions


def _item_refs(item: dict[str, Any], key: str) -> set[str]:
    return {value.casefold() for value in item.get(key, [])}


def _item_label_text(item: dict[str, Any]) -> str:
    return " ".join(
        _clean_string(item.get(key))
        for key in ("id", "label", "name")
        if item.get(key)
    ).casefold()


def _cluster_text(cluster: dict[str, Any]) -> str:
    fields = [
        cluster.get("id"),
        cluster.get("label"),
        " ".join(cluster.get("positive_traits", [])),
        " ".join(cluster.get("negative_traits", [])),
        " ".join(cluster.get("mood_states", [])),
        " ".join(cluster.get("languages", [])),
        " ".join(cluster.get("regions", [])),
        " ".join(cluster.get("representative_titles", [])),
    ]
    return " ".join(_clean_string(field) for field in fields).casefold()


def _cluster_locale_match_count(
    cluster: dict[str, Any],
    language_terms: set[str],
    region_terms: set[str],
) -> int:
    cluster_languages = {value.casefold() for value in cluster.get("languages", [])}
    cluster_regions = {value.casefold() for value in cluster.get("regions", [])}
    return len(language_terms & cluster_languages) + len(region_terms & cluster_regions)


def _text_tokens(text: str) -> set[str]:
    return {
        part.casefold()
        for part in re.split(r"[^a-zA-Z0-9]+", text)
        if part
    }


def _term_matches_text(term: str, text: str, tokens: set[str]) -> bool:
    if not term:
        return False
    if len(term) <= 2 and re.fullmatch(r"[a-z0-9]+", term):
        return term in tokens
    return term in text


def _matching_term_count(terms: set[str], text: str) -> int:
    tokens = _text_tokens(text)
    return sum(1 for term in terms if _term_matches_text(term, text, tokens))


def _text_matches_any_term(text: str, terms: set[str]) -> bool:
    return _matching_term_count(terms, text.casefold()) > 0


def _is_family_request(intent: Any, terms: set[str]) -> bool:
    if _clean_string(getattr(intent, "special_intent", "")).casefold() == "family":
        return True
    return bool(terms & FAMILY_TERMS)


def _format_cluster(cluster: dict[str, Any]) -> str:
    positives = "; ".join(cluster.get("positive_traits", [])) or "unspecified positive traits"
    negatives = "; ".join(cluster.get("negative_traits", [])) or "no explicit negatives"
    titles = ", ".join(cluster.get("representative_titles", [])) or "no representative titles"
    return (
        f"- {cluster['label']} (weight {cluster['weight']:.2f}, {cluster['co_viewing']}): "
        f"likes {positives}; avoid {negatives}; titles: {titles}"
    )


def structured_profile_text(profile: dict[str, Any] | None) -> str:
    """The whole structured profile as prompt text, strongest cluster first."""
    if not profile or not profile.get("clusters"):
        return ""
    normalized = validate_structured_profile(profile)
    lines = ["Taste profile, strongest first:"]
    lines.extend(_format_cluster(cluster) for cluster in normalized["clusters"])
    lines.extend(_format_named_item("negative preference", item)
                 for item in normalized["negative_preferences"][:6])
    return "\n".join(lines)


def _format_named_item(prefix: str, item: dict[str, Any]) -> str:
    label = item.get("label") or item.get("name") or item.get("id")
    weight = item.get("weight")
    weight_text = f" (weight {weight:.2f})" if isinstance(weight, float) else ""
    traits = "; ".join(item.get("traits", []))
    applies_to = ", ".join(item.get("applies_to", []))
    suffix = traits or applies_to
    return f"- {prefix}: {label}{weight_text}" + (f": {suffix}" if suffix else "")


def select_profile_slice(intent: Any, profile: dict[str, Any] | None, max_clusters: int = 5) -> str:
    if not profile:
        return ""
    normalized = validate_structured_profile(profile)
    terms = _intent_terms(intent)
    language_terms, region_terms = _intent_locale_terms(intent)
    query_has_terms = bool(terms or language_terms or region_terms)
    family_request = _is_family_request(intent, terms)

    eligible_clusters = [
        cluster for cluster in normalized["clusters"]
        if family_request or cluster["co_viewing"] != "family"
    ]
    locale_requested = bool(language_terms or region_terms)
    locale_restricted = (
        locale_requested
        and any(_cluster_locale_match_count(cluster, language_terms, region_terms) for cluster in eligible_clusters)
    )

    scored_clusters: list[tuple[float, dict[str, Any]]] = []
    for cluster in eligible_clusters:
        locale_match_count = _cluster_locale_match_count(cluster, language_terms, region_terms)
        if locale_restricted and not locale_match_count:
            continue
        haystack = _cluster_text(cluster)
        match_count = _matching_term_count(terms, haystack)
        match_count += locale_match_count
        score = match_count + (cluster["weight"] * 0.25)
        if match_count or not query_has_terms:
            scored_clusters.append((score, cluster))

    if not scored_clusters:
        scored_clusters = [
            (cluster["weight"] * 0.25, cluster)
            for cluster in eligible_clusters
        ]

    selected = [
        cluster
        for _, cluster in sorted(scored_clusters, key=lambda item: item[0], reverse=True)[:max_clusters]
    ]
    if not selected:
        return ""

    selected_cluster_refs = {
        value.casefold()
        for cluster in selected
        for value in (cluster.get("id"), cluster.get("label"))
        if value
    }
    lines = ["Relevant taste profile slice:"]
    lines.extend(_format_cluster(cluster) for cluster in selected)

    for item in normalized["creator_affinities"][:6]:
        item_clusters = _item_refs(item, "clusters")
        label_text = _item_label_text(item)
        if query_has_terms and not (selected_cluster_refs & item_clusters) and not _text_matches_any_term(label_text, terms):
            continue
        lines.append(_format_named_item("creator affinity", item))

    for item in normalized["language_region_affinities"][:6]:
        item_languages = _item_refs(item, "languages")
        item_regions = _item_refs(item, "regions")
        applies_to = _item_refs(item, "applies_to")
        label_text = _item_label_text(item)
        if locale_requested:
            is_relevant = bool((language_terms & item_languages) or (region_terms & item_regions))
        else:
            is_relevant = bool((selected_cluster_refs & applies_to) or _text_matches_any_term(label_text, terms))
        if query_has_terms and not is_relevant:
            continue
        lines.append(_format_named_item("language/region affinity", item))

    for item in normalized["negative_preferences"][:6]:
        applies_to = _item_refs(item, "applies_to")
        item_clusters = _item_refs(item, "clusters")
        if query_has_terms and not (terms & applies_to) and not (selected_cluster_refs & item_clusters):
            continue
        lines.append(_format_named_item("negative preference", item))

    return "\n".join(lines)
