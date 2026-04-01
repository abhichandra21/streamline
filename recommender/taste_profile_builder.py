import anthropic

from .ingestion.base import WatchEvent


def build(
    events: list[WatchEvent],
    scores: dict[str, float],
    enrichments: dict[str, str],
    client: anthropic.Anthropic,
) -> str:
    """Build a natural-language taste profile using Claude Sonnet."""
    scored = sorted(
        [(title, score) for title, score in scores.items() if title in enrichments],
        key=lambda x: -x[1],
    )[:50]

    lines = [
        f"- {title} (score: {score:.2f}): {enrichments[title]}"
        for title, score in scored
    ]
    history_str = '\n'.join(lines)

    message = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=800,
        timeout=30.0,
        messages=[{
            "role": "user",
            "content": (
                "Analyze this person's streaming watch history and write a detailed taste profile.\n"
                "Identify distinct taste clusters, preferences for tone/pacing/culture, "
                "what they consistently finish, and notable patterns.\n"
                "Write in second person (\"You gravitate toward...\").\n\n"
                f"Watch history (sorted by engagement score):\n{history_str}"
            ),
        }],
    )
    return message.content[0].text.strip()
