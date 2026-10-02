import sys
from types import SimpleNamespace


def _fake_openai_module():
    class FakeCompletions:
        def __init__(self, calls, response):
            self._calls = calls
            self._response = response

        def create(self, **kwargs):
            self._calls.append(kwargs)
            return self._response

    class FakeOpenAI:
        instances = []

        def __init__(self, **kwargs):
            self.init_kwargs = kwargs
            self.calls = []
            self.response = SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(
                            content="ok",
                            reasoning=None,
                            reasoning_content=None,
                        ),
                        finish_reason="stop",
                    )
                ],
                usage=SimpleNamespace(prompt_tokens=12, completion_tokens=34),
            )
            self.chat = SimpleNamespace(completions=FakeCompletions(self.calls, self.response))
            type(self).instances.append(self)

    return SimpleNamespace(OpenAI=FakeOpenAI), FakeOpenAI


def test_openai_client_uses_configured_thinking_scale_and_floor(monkeypatch):
    from recommender import llm as llm_module

    fake_module, fake_cls = _fake_openai_module()
    monkeypatch.setitem(sys.modules, "openai", fake_module)

    client = llm_module.OpenAIClient(
        api_key="test-key",
        models={
            "reason": "gpt-test",
            "thinking": True,
            "thinking_token_scale": 2,
            "thinking_token_floor": 100,
        },
        base_url="http://localhost:11434/v1",
    )

    assert client.generate("hello", max_tokens=40) == "ok"
    assert fake_cls.instances[-1].calls[0]["max_tokens"] == 100


def test_openai_client_can_disable_local_thinking_token_floor(monkeypatch):
    from recommender import llm as llm_module

    fake_module, fake_cls = _fake_openai_module()
    monkeypatch.setitem(sys.modules, "openai", fake_module)

    client = llm_module.OpenAIClient(
        api_key="test-key",
        models={
            "reason": "gpt-oss:120b",
            "thinking": True,
            "thinking_token_scale": 1,
            "thinking_token_floor": 0,
        },
        base_url="http://localhost:11434/v1",
    )

    assert client.generate("profile merge", max_tokens=300) == "ok"
    assert fake_cls.instances[-1].calls[0]["max_tokens"] == 300


def test_openai_client_non_thinking_models_leave_max_tokens_unchanged(monkeypatch):
    from recommender import llm as llm_module

    fake_module, fake_cls = _fake_openai_module()
    monkeypatch.setitem(sys.modules, "openai", fake_module)

    client = llm_module.OpenAIClient(
        api_key="test-key",
        models={"reason": "gpt-4.1"},
    )

    assert client.generate("rank these", max_tokens=750) == "ok"
    assert fake_cls.instances[-1].calls[0]["max_tokens"] == 750


def _fake_anthropic_module(content, stop_reason="end_turn", stop_details=None):
    class FakeMessages:
        def __init__(self, calls):
            self._calls = calls

        def create(self, **kwargs):
            self._calls.append(kwargs)
            return SimpleNamespace(
                content=content,
                stop_reason=stop_reason,
                stop_details=stop_details,
                usage=SimpleNamespace(input_tokens=10, output_tokens=20),
            )

    class FakeAnthropic:
        instances = []

        def __init__(self, **kwargs):
            self.calls = []
            self.beta_calls = []
            self.messages = FakeMessages(self.calls)
            self.beta = SimpleNamespace(messages=FakeMessages(self.beta_calls))
            type(self).instances.append(self)

    return SimpleNamespace(Anthropic=FakeAnthropic), FakeAnthropic


def _text(text):
    return SimpleNamespace(type="text", text=text)


def test_anthropic_client_sends_sonnet_5_5_options_through_beta(monkeypatch):
    from recommender import llm as llm_module

    fake_module, fake_cls = _fake_anthropic_module([_text("ok")])
    monkeypatch.setitem(sys.modules, "anthropic", fake_module)

    client = llm_module.AnthropicClient(api_key="test-key", models={"reason": "claude-sonnet-5-5"})

    assert client.generate("hello", max_tokens=40) == "ok"
    instance = fake_cls.instances[-1]
    assert instance.calls == []
    request = instance.beta_calls[0]
    assert request["thinking"] == {"type": "between_tools"}
    assert request["output_config"] == {"effort": "high"}
    assert request["betas"] == ["server-side-fallback-2026-07-01"]
    assert request["extra_body"] == {"fallbacks": "default"}


def test_anthropic_client_sends_plain_request_for_other_models(monkeypatch):
    from recommender import llm as llm_module

    fake_module, fake_cls = _fake_anthropic_module([_text("ok")])
    monkeypatch.setitem(sys.modules, "anthropic", fake_module)

    client = llm_module.AnthropicClient(api_key="test-key", models={"fast": "claude-haiku-4-5-20251001"})

    assert client.generate("hello", role="fast", max_tokens=40) == "ok"
    instance = fake_cls.instances[-1]
    assert instance.beta_calls == []
    assert set(instance.calls[0]) == {"model", "max_tokens", "timeout", "messages"}


def test_anthropic_client_reads_text_blocks_after_other_blocks(monkeypatch):
    from recommender import llm as llm_module

    content = [SimpleNamespace(type="thinking", thinking=""), _text("first "), _text("second")]
    fake_module, _ = _fake_anthropic_module(content)
    monkeypatch.setitem(sys.modules, "anthropic", fake_module)

    client = llm_module.AnthropicClient(api_key="test-key", models={"reason": "claude-sonnet-5-5"})

    assert client.generate("hello") == "first second"


def test_anthropic_client_raises_on_refusal(monkeypatch):
    import pytest
    from recommender import llm as llm_module

    fake_module, _ = _fake_anthropic_module(
        [], stop_reason="refusal", stop_details=SimpleNamespace(category="cyber"),
    )
    monkeypatch.setitem(sys.modules, "anthropic", fake_module)

    client = llm_module.AnthropicClient(api_key="test-key", models={"reason": "claude-sonnet-5-5"})

    with pytest.raises(RuntimeError, match="category=cyber"):
        client.generate("hello")
