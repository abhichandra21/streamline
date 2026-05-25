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

log = logging.getLogger("recommender.structured_profile")

ALLOWED_CO_VIEWING = {"personal", "family", "mixed", "unknown"}
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
        label = _clean_string(raw.get("label") or raw.get("name") or raw.get("id"))
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
            "weight": _clamp_weight(raw.get("weight")),
            "positive_traits": _clean_string_list(raw.get("positive_traits")),
            "negative_traits": _clean_string_list(raw.get("negative_traits")),
            "co_viewing": co_viewing,
            "mood_states": _clean_string_list(raw.get("mood_states")),
            "languages": _clean_string_list(raw.get("languages")),
            "regions": _clean_string_list(raw.get("regions")),
            "representative_titles": representative_titles,
        })
        if len(clusters) == 16:
            break

    profile["clusters"] = clusters
    profile["mood_states"] = _clean_named_items(data.get("mood_states"))
    profile["creator_affinities"] = _clean_named_items(data.get("creator_affinities"))
    profile["language_region_affinities"] = _clean_named_items(data.get("language_region_affinities"))
    profile["negative_preferences"] = _clean_named_items(data.get("negative_preferences"))
    return profile


def parse_structured_profile_response(text: str) -> dict[str, Any]:
    try:
        data = json.loads(_strip_json_fence(text))
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid structured profile JSON: {exc}") from exc
    return validate_structured_profile(data)


def build_structured_profile(
    events: list[WatchEvent],
    scores: dict[str, float],
    enrichments: dict[str, str],
    client: LLMClient,
    negative_prefs: list[str] | None = None,
) -> dict[str, Any]:
    scored = sorted(
        [(title, score) for title, score in scores.items() if title in enrichments],
        key=lambda item: -item[1],
    )
    if not scored:
        log.warning("No enriched titles found for structured profile build; returning empty profile")
        return validate_structured_profile({})

    lines = [
        f"- {title} (score: {score:.2f}): {enrichments[title]}"
        for title, score in scored[:160]
    ]
    disliked = ", ".join(f'"{title}"' for title in (negative_prefs or [])) or "none"
    prompt = (
        "Create a compact structured JSON taste profile for a personal streaming recommender.\n"
        "Use the engagement scores to separate strong taste signals from incidental watches.\n"
        "Capture specific taste clusters, positive traits, negative traits, co-viewing context, "
        "mood states, creator affinities, language or region affinities, and explicit dislikes.\n"
        "Return ONLY valid JSON with keys: version, clusters, mood_states, creator_affinities, "
        "language_region_affinities, negative_preferences.\n"
        "For each cluster include: id, label, weight, positive_traits, negative_traits, "
        "co_viewing, mood_states, languages, regions, representative_titles.\n"
        "Use ISO-639-1 language codes like hi, en, ko, es, fr when known. "
        "Use ISO-3166 alpha-2 region codes like IN, GB, US, KR when known.\n"
        "Cluster weight should mean strength and confidence of the taste signal, not raw watch frequency. "
        "Do not let high-volume family/co-viewing clusters outrank stronger personal preferences by volume alone.\n"
        "creator_affinities entries must include weight, traits, and clusters. "
        "Traits should explain what the user responds to in that creator's work, not just repeat the name.\n"
        "language_region_affinities entries must include weight, languages, regions, traits, and applies_to. "
        "For example, use languages ['hi'] and regions ['IN'] for a Hindi and Indian cinema affinity.\n"
        "negative_preferences should include explicit dislikes first. "
        "Only infer cautious anti-patterns when repeated low-engagement evidence supports them; otherwise return an empty list.\n"
        "Use co_viewing only as one of: personal, family, mixed, unknown.\n"
        "Use weights from 0.0 to 1.0. Do not invent titles that are not in the history.\n\n"
        f"Explicitly disliked titles: {disliked}\n\n"
        "Watch history sorted by engagement score:\n"
        + "\n".join(lines)
    )
    response_text = client.generate(
        prompt,
        role="reason",
        max_tokens=config.TOKENS_PROFILE_MERGE,
        timeout=config.TIMEOUT_PROFILE_MERGE,
    )
    return parse_structured_profile_response(response_text)


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
