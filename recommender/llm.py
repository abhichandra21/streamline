"""LLM provider abstraction.

Supports Anthropic (Claude) and Google (Gemini) with a unified interface.
Call sites use roles ("fast" or "reason") instead of model names.
Model assignments are configured in config.yaml per provider.

Gemini uses the google-genai SDK. API keys starting with 'AQ.' are
automatically routed to Vertex AI; 'AIza' keys go to the Gemini Developer API.
"""

import logging
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

log = logging.getLogger("recommender.llm")

# Pricing per million tokens (USD)
_PRICING: dict[str, dict[str, float]] = {
    "claude-haiku-4-5-20251001":  {"input": 0.80, "output": 4.00},
    "claude-sonnet-4-6":          {"input": 3.00, "output": 15.00},
    "gemini-2.5-flash":           {"input": 0.15, "output": 0.60, "thinking": 0.0375},
    "gemini-2.5-pro":             {"input": 1.25, "output": 10.00, "thinking": 0.625},
}


@dataclass
class UsageStats:
    """Accumulated token usage across multiple LLM calls."""
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    thinking_tokens: int = 0
    cost_usd: float = 0.0
    _by_model: dict = field(default_factory=dict)

    def record(self, model: str, input_t: int, output_t: int, thinking_t: int = 0) -> None:
        self.calls += 1
        self.input_tokens += input_t
        self.output_tokens += output_t
        self.thinking_tokens += thinking_t

        pricing = _PRICING.get(model, {"input": 0, "output": 0, "thinking": 0})
        cost = (
            input_t * pricing.get("input", 0) / 1_000_000
            + output_t * pricing.get("output", 0) / 1_000_000
            + thinking_t * pricing.get("thinking", 0) / 1_000_000
        )
        self.cost_usd += cost

        if model not in self._by_model:
            self._by_model[model] = {"calls": 0, "input": 0, "output": 0, "thinking": 0, "cost": 0.0}
        self._by_model[model]["calls"] += 1
        self._by_model[model]["input"] += input_t
        self._by_model[model]["output"] += output_t
        self._by_model[model]["thinking"] += thinking_t
        self._by_model[model]["cost"] += cost

    def summary(self) -> str:
        parts = [f"{self.calls} calls"]
        parts.append(f"{self.input_tokens:,} in / {self.output_tokens:,} out")
        if self.thinking_tokens:
            parts.append(f"{self.thinking_tokens:,} thinking")
        parts.append(f"~${self.cost_usd:.4f}")
        return " | ".join(parts)

    def reset(self) -> None:
        self.calls = 0
        self.input_tokens = 0
        self.output_tokens = 0
        self.thinking_tokens = 0
        self.cost_usd = 0.0
        self._by_model.clear()


class LLMClient(ABC):
    """Abstract LLM client interface."""

    provider: str
    models: dict[str, str]
    usage: UsageStats

    @abstractmethod
    def generate(
        self,
        prompt: str,
        role: str = "reason",
        max_tokens: int = 1000,
        timeout: float = 30.0,
    ) -> str:
        """Send a prompt and return the response text.

        Args:
            role: "fast" (enrichment, simple tasks) or "reason" (intent, ranking, profile)
        """


class AnthropicClient(LLMClient):
    """Anthropic Claude provider."""

    provider = "anthropic"

    def __init__(self, api_key: str, models: dict[str, str]):
        import anthropic
        self._client = anthropic.Anthropic(api_key=api_key)
        self.models = models
        self.usage = UsageStats()
        log.info("Using Anthropic provider (fast=%s, reason=%s)", models.get("fast"), models.get("reason"))

    def generate(self, prompt: str, role: str = "reason",
                 max_tokens: int = 1000, timeout: float = 30.0) -> str:
        model = self.models.get(role, self.models.get("reason", "claude-sonnet-4-6"))
        message = self._client.messages.create(
            model=model,
            max_tokens=max_tokens,
            timeout=timeout,
            messages=[{"role": "user", "content": prompt}],
        )
        self.usage.record(
            model=model,
            input_t=message.usage.input_tokens,
            output_t=message.usage.output_tokens,
        )
        return message.content[0].text


class GeminiClient(LLMClient):
    """Google Gemini provider using the google-genai SDK."""

    provider = "gemini"

    def __init__(self, api_key: str, models: dict[str, str]):
        from google import genai
        is_vertex = api_key.startswith("AQ.")
        self._client = genai.Client(api_key=api_key, vertexai=is_vertex)
        self.models = models
        self.usage = UsageStats()
        log.info("Using Gemini provider (%s, fast=%s, reason=%s)",
                 "Vertex AI" if is_vertex else "Developer API",
                 models.get("fast"), models.get("reason"))

    def generate(self, prompt: str, role: str = "reason",
                 max_tokens: int = 1000, timeout: float = 30.0) -> str:
        from google.genai import types

        model = self.models.get(role, self.models.get("reason", "gemini-2.5-flash"))

        # Gemini needs more output tokens than Anthropic for equivalent content
        adjusted_tokens = max(max_tokens * 3, 4000)

        config_params: dict = {"max_output_tokens": adjusted_tokens}
        if "return only valid json" in prompt.lower() or "return only a json" in prompt.lower():
            config_params["response_mime_type"] = "application/json"

        # Pro with thinking mode needs longer timeouts
        effective_timeout = max(timeout, 120.0) if "pro" in model else timeout

        config = types.GenerateContentConfig(
            **config_params,
            http_options=types.HttpOptions(timeout=int(effective_timeout * 1000)),
        )

        # Retry on rate limit and timeouts
        for attempt in range(3):
            try:
                response = self._client.models.generate_content(
                    model=model,
                    contents=prompt,
                    config=config,
                )
                break
            except Exception as exc:
                err = str(exc)
                if ("429" in err or "RESOURCE_EXHAUSTED" in err or "504" in err or "DEADLINE_EXCEEDED" in err) and attempt < 2:
                    wait = 30 * (attempt + 1)
                    log.warning("Gemini rate limited, waiting %ds (attempt %d/3)...", wait, attempt + 1)
                    time.sleep(wait)
                    continue
                raise

        # Track usage
        um = getattr(response, "usage_metadata", None)
        if um:
            self.usage.record(
                model=model,
                input_t=getattr(um, "prompt_token_count", 0) or 0,
                output_t=getattr(um, "candidates_token_count", 0) or 0,
                thinking_t=getattr(um, "thoughts_token_count", 0) or 0,
            )

        if response.candidates:
            reason = response.candidates[0].finish_reason
            if str(reason) != "FinishReason.STOP":
                log.warning("Gemini finish_reason=%s (model=%s, max_tokens=%d)",
                            reason, model, adjusted_tokens)

        if hasattr(response, "text") and response.text:
            return response.text
        raise RuntimeError(f"Gemini response did not include text content (model={model})")


def create_client(provider: str | None = None) -> LLMClient:
    """Create an LLM client from config.

    Args:
        provider: Override provider name. If None, uses config.LLM_PROVIDER.
    """
    import config

    provider = provider or config.LLM_PROVIDER
    models = config.LLM_MODELS.get(provider, {})

    if not models:
        raise ValueError(f"No model config for provider '{provider}' in config.yaml")

    if provider == "gemini":
        if not config.GEMINI_API_KEY:
            raise RuntimeError("GEMINI_API_KEY not set. Export it or add to .env.")
        return GeminiClient(api_key=config.GEMINI_API_KEY, models=models)
    elif provider == "anthropic":
        if not config.ANTHROPIC_API_KEY:
            raise RuntimeError("ANTHROPIC_API_KEY not set. Export it or add to .env.")
        return AnthropicClient(api_key=config.ANTHROPIC_API_KEY, models=models)
    else:
        raise ValueError(f"Unknown LLM provider: {provider}. Use 'anthropic' or 'gemini'.")
