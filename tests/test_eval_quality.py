import json
from types import SimpleNamespace

import pytest

from tools import eval_quality


def _fake_claude_cli(monkeypatch, stdout, returncode=0):
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        return SimpleNamespace(stdout=stdout, stderr="", returncode=returncode)

    monkeypatch.setattr(eval_quality.subprocess, "run", fake_run)
    return calls


def test_claude_code_client_sends_a_bare_call_on_the_subscription(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    calls = _fake_claude_cli(monkeypatch, json.dumps(
        {"is_error": False, "result": "ok", "usage": {"input_tokens": 10, "output_tokens": 20}}))
    client = eval_quality.ClaudeCodeClient(models={"fast": "claude-haiku-4-5-20251001"})

    assert client.generate("hello", role="fast") == "ok"
    cmd, kwargs = calls[0]
    assert cmd[cmd.index("--model") + 1] == "claude-haiku-4-5-20251001"
    assert cmd[cmd.index("--system-prompt") + 1] == ""
    assert cmd[cmd.index("--tools") + 1] == ""
    assert kwargs["input"] == "hello"
    assert "ANTHROPIC_API_KEY" not in kwargs["env"]
    assert kwargs["env"]["MAX_THINKING_TOKENS"] == "0"
    assert client.usage.output_tokens == 20


def test_claude_code_client_raises_when_the_cli_reports_an_error(monkeypatch):
    _fake_claude_cli(monkeypatch, json.dumps({"is_error": True, "result": "Not logged in"}))
    client = eval_quality.ClaudeCodeClient(models={"reason": "claude-sonnet-5-5"})

    with pytest.raises(RuntimeError, match="Not logged in"):
        client.generate("hello")


def test_claude_code_client_raises_when_the_cli_prints_no_json(monkeypatch):
    _fake_claude_cli(monkeypatch, "Not logged in · Please run /login", returncode=1)
    client = eval_quality.ClaudeCodeClient(models={"reason": "claude-sonnet-5-5"})

    with pytest.raises(RuntimeError, match="exit=1"):
        client.generate("hello")


def test_eval_env_replaces_the_api_key_so_stray_api_calls_fail(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "real-key")

    assert eval_quality._env()["ANTHROPIC_API_KEY"] != "real-key"


def test_set_changes_only_the_scratch_copy_config(tmp_path):
    (tmp_path / "config.local.yaml").write_text("min_year: 1990\n")

    applied = eval_quality._apply_settings(tmp_path, ["profile_note=No horror, like It", "min_rating=7"])

    assert applied == {"profile_note": "No horror, like It", "min_rating": 7}
    text = (tmp_path / "config.local.yaml").read_text()
    assert "min_year: 1990" in text and "No horror, like It" in text
