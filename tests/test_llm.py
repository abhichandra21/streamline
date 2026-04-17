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
