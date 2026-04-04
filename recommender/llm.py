"""LLM provider abstraction.

Supports Anthropic (Claude), Google (Gemini), and any OpenAI-compatible API
with a unified interface. Call sites use roles ("fast" or "reason") instead
of model names. Model assignments are configured in config.yaml per provider.

The OpenAI provider works with any service implementing the OpenAI chat
completions API: OpenAI, Ollama, Groq, Together, LM Studio, vLLM, etc.
Configure base_url and api_key_env in config.yaml models.openai section.
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
    "gpt-4.1":                    {"input": 2.00, "output": 8.00},
    "gpt-4.1-mini":               {"input": 0.40, "output": 1.60},
    "gpt-4.1-nano":               {"input": 0.10, "output": 0.40},
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
    was_truncated: bool = False  # set after each generate() call

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
        self.was_truncated = message.stop_reason == "max_tokens"
        if self.was_truncated:
            log.warning("Anthropic response truncated (max_tokens=%d, model=%s). "
                        "Increase the token limit in config.yaml.", max_tokens, model)
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
        # Enable JSON mode when prompt asks for structured JSON output
        prompt_lower = prompt.lower()
        if any(phrase in prompt_lower for phrase in [
            "return only valid json", "return only a json",
            "return only a json array", "json array of",
        ]):
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

        self.was_truncated = False
        if response.candidates:
            reason = response.candidates[0].finish_reason
            if str(reason) != "FinishReason.STOP":
                self.was_truncated = "MAX_TOKENS" in str(reason)
                log.warning("Gemini finish_reason=%s (model=%s, max_tokens=%d)",
                            reason, model, adjusted_tokens)

        if hasattr(response, "text") and response.text:
            return response.text
        raise RuntimeError(f"Gemini response did not include text content (model={model})")


class OpenAIClient(LLMClient):
    """OpenAI-compatible provider. Works with OpenAI, Ollama, Groq, Together, LM Studio, vLLM, etc."""

    provider = "openai"

    def __init__(self, api_key: str, models: dict[str, str], base_url: str | None = None):
        from openai import OpenAI
        kwargs: dict = {"api_key": api_key}
        if base_url:
            kwargs["base_url"] = base_url
        self._client = OpenAI(**kwargs)
        self.models = models
        self.usage = UsageStats()
        endpoint = base_url or "api.openai.com"
        log.info("Using OpenAI-compatible provider (endpoint=%s, fast=%s, reason=%s)",
                 endpoint, models.get("fast"), models.get("reason"))

    def generate(self, prompt: str, role: str = "reason",
                 max_tokens: int = 1000, timeout: float = 30.0) -> str:
        model = self.models.get(role, self.models.get("reason", "gpt-4.1-mini"))

        for attempt in range(3):
            try:
                response = self._client.chat.completions.create(
                    model=model,
                    max_tokens=max_tokens,
                    timeout=timeout,
                    messages=[{"role": "user", "content": prompt}],
                )
                break
            except Exception as exc:
                err = str(exc)
                if ("429" in err or "rate" in err.lower() or "timeout" in err.lower()) and attempt < 2:
                    wait = 30 * (attempt + 1)
                    log.warning("OpenAI rate limited/timeout, waiting %ds (attempt %d/3)...", wait, attempt + 1)
                    time.sleep(wait)
                    continue
                raise

        choice = response.choices[0]
        text = choice.message.content or ""

        # Track usage
        if response.usage:
            self.usage.record(
                model=model,
                input_t=response.usage.prompt_tokens or 0,
                output_t=response.usage.completion_tokens or 0,
            )

        self.was_truncated = choice.finish_reason == "length"
        if self.was_truncated:
            log.warning("OpenAI response truncated (max_tokens=%d, model=%s). "
                        "Increase the token limit in config.yaml.", max_tokens, model)

        return text


def create_client(provider: str | None = None) -> LLMClient:
    """Create an LLM client from config.

    Args:
        provider: Override provider name. If None, uses config.LLM_PROVIDER.
    """
    import config

    import os

    provider = provider or config.LLM_PROVIDER
    models = config.LLM_MODELS.get(provider, {})

    if not models:
        raise ValueError(f"No model config for provider '{provider}' in config.yaml")

    # All providers read their API key from a configurable env var
    default_key_envs = {"anthropic": "ANTHROPIC_API_KEY", "gemini": "GEMINI_API_KEY", "openai": "OPENAI_API_KEY"}
    api_key_env = models.get("api_key_env", default_key_envs.get(provider, f"{provider.upper()}_API_KEY"))
    api_key = os.environ.get(api_key_env, "")
    if not api_key:
        raise RuntimeError(f"{api_key_env} not set. Export it or add to .env.")

    if provider == "gemini":
        return GeminiClient(api_key=api_key, models=models)
    elif provider == "anthropic":
        return AnthropicClient(api_key=api_key, models=models)
    elif provider == "openai":
        base_url = models.get("base_url") or None
        return OpenAIClient(api_key=api_key, models=models, base_url=base_url)
    else:
        raise ValueError(f"Unknown LLM provider: {provider}. Use 'anthropic', 'gemini', or 'openai'.")
