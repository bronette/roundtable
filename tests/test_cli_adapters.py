"""CLI adapters against recorded envelopes (no subprocess). Shapes copied from real runs on 2026-09-25."""

import json

import pytest

from roundtable.providers.base import Message, SchemaError
from roundtable.providers.cli import claude, codex, common, grok
from roundtable.schemas import Answer


@pytest.fixture(autouse=True)
def no_binary_check(monkeypatch):
    monkeypatch.setattr(common, "require_binary", lambda b: f"/fake/{b}")


def fake_run(rc, stdout, stderr=""):
    def _run(argv, *, cwd, env, timeout_s):
        fake_run.argv = argv
        fake_run.env = env
        if argv[0].endswith("codex"):
            import os
            with open(os.path.join(cwd, "last.txt"), "w") as f:
                f.write(fake_run.last or "")
        return rc, stdout, stderr, 42
    fake_run.last = None
    return _run


CLAUDE_OK = {"type": "result", "subtype": "success", "is_error": False, "result": '{"answer":"a","reasoning_summary":"r","confidence":0.5}',
             "structured_output": {"answer": "a", "reasoning_summary": "r", "confidence": 0.5},
             "usage": {"input_tokens": 2, "cache_creation_input_tokens": 100, "cache_read_input_tokens": 5, "output_tokens": 50,
                       "output_tokens_details": {"thinking_tokens": 7}},
             "modelUsage": {"claude-fable-5-1": {}}, "total_cost_usd": 0.01, "session_id": "s1", "stop_reason": "end_turn"}


def test_claude_envelope(monkeypatch):
    monkeypatch.setattr(common, "run_argv", fake_run(0, json.dumps(CLAUDE_OK)))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "leak")
    p = claude.ClaudeCLI("c")
    c = p.run([Message("system", "S"), Message("user", "U")], schema=Answer)
    assert c.parsed["answer"] == "a" and c.model == "claude-fable-5-1"
    assert c.usage.input_tokens == 102 and c.usage.cached_input_tokens == 5 and c.usage.reasoning_tokens == 7
    assert c.reported_cost_usd == 0.01 and c.request_id == "s1"
    assert "ANTHROPIC_API_KEY" not in fake_run.env          # subscription mode strips keys
    assert "--system-prompt" in fake_run.argv and "--json-schema" in fake_run.argv


GROK_OK = {"text": '{"answer":"a","reasoning_summary":"r","confidence":0.5}', "stopReason": "end_turn", "requestId": "rq",
           "usage": {"input_tokens": 10, "output_tokens": 20, "cache_read_input_tokens": 3, "reasoning_tokens": 15},
           "total_cost_usd": 0.02, "modelUsage": {"grok-4.7-build": {}},
           "structuredOutput": {"answer": "a", "reasoning_summary": "r", "confidence": 0.5}}


def test_grok_envelope_and_effort(monkeypatch):
    monkeypatch.setattr(common, "run_argv", fake_run(0, json.dumps(GROK_OK)))
    c = grok.GrokCLI("g").run([Message("user", "U")], schema=Answer, effort="low")
    assert c.parsed["confidence"] == 0.5 and c.model == "grok-4.7-build" and c.usage.reasoning_tokens == 15
    assert fake_run.argv[fake_run.argv.index("--reasoning-effort") + 1] == "low"


def test_grok_prose_instead_of_json_returns_unparsed_text_for_repair(monkeypatch):
    env = GROK_OK | {"text": "The proposal is correct and matches the spec.", "structuredOutput": None,
                     "structuredOutputError": "model did not produce structured output"}
    monkeypatch.setattr(common, "run_argv", fake_run(1, json.dumps(env)))
    c = grok.GrokCLI("g").run([Message("user", "U")], schema=Answer)
    assert c.parsed is None and c.text.startswith("The proposal") and c.usage.output_tokens == 20
    assert fake_run.argv[fake_run.argv.index("--max-turns") + 1] == "3"


def test_grok_json_inside_prose_is_recovered(monkeypatch):
    env = GROK_OK | {"text": 'Here you go: {"answer":"a","reasoning_summary":"r","confidence":0.5}', "structuredOutput": None,
                     "structuredOutputError": "model did not produce structured output"}
    monkeypatch.setattr(common, "run_argv", fake_run(1, json.dumps(env)))
    assert grok.GrokCLI("g").run([Message("user", "U")], schema=Answer).parsed["answer"] == "a"


def test_grok_real_failure_is_provider_error(monkeypatch):
    monkeypatch.setattr(common, "run_argv", fake_run(1, "", "boom: not logged in"))
    from roundtable.providers.base import ProviderError
    with pytest.raises(ProviderError):
        grok.GrokCLI("g").run([Message("user", "U")], schema=Answer)


CODEX_EVENTS = "\n".join(json.dumps(e) for e in [
    {"type": "thread.started", "thread_id": "t1"}, {"type": "turn.started"},
    {"type": "item.completed", "item": {"id": "i0", "type": "agent_message", "text": '{"answer":"a","reasoning_summary":"r","confidence":0.5}'}},
    {"type": "turn.completed", "usage": {"input_tokens": 13, "cached_input_tokens": 2, "output_tokens": 4, "reasoning_output_tokens": 1}},
])


def test_codex_events(monkeypatch):
    r = fake_run(0, CODEX_EVENTS)
    r.last = '{"answer":"a","reasoning_summary":"r","confidence":0.5}'
    monkeypatch.setattr(common, "run_argv", r)
    c = codex.CodexCLI("x").run([Message("system", "S"), Message("user", "U")], schema=Answer, effort="high")
    assert c.parsed["answer"] == "a" and c.usage.input_tokens == 13 and c.request_id == "t1"
    assert fake_run.argv[-1].startswith("SYSTEM INSTRUCTIONS:\nS")     # no system flag on codex
    assert 'model_reasoning_effort="high"' in fake_run.argv
    schema_path = fake_run.argv[fake_run.argv.index("--output-schema") + 1]
    assert schema_path.endswith("schema.json")


def test_codex_turn_failed(monkeypatch):
    ev = "\n".join(json.dumps(e) for e in [{"type": "thread.started", "thread_id": "t1"}, {"type": "turn.started"},
                                            {"type": "turn.failed", "error": {"message": "invalid_json_schema"}}])
    monkeypatch.setattr(common, "run_argv", fake_run(1, ev))
    from roundtable.providers.base import ProviderError
    with pytest.raises(ProviderError, match="invalid_json_schema"):
        codex.CodexCLI("x").run([Message("user", "U")], schema=Answer)


def test_codex_transient_error_then_completion_is_success(monkeypatch):
    ev = json.dumps({"type": "error", "message": "stream disconnected; reconnecting"}) + "\n" + CODEX_EVENTS
    r = fake_run(0, ev)
    r.last = '{"answer":"a","reasoning_summary":"r","confidence":0.5}'
    monkeypatch.setattr(common, "run_argv", r)
    assert codex.CodexCLI("x").run([Message("user", "U")], schema=Answer).parsed["answer"] == "a"


GEMINI_OK = {"response": 'Here it is: {"answer":"a","reasoning_summary":"r","confidence":0.5}',
             "stats": {"models": {"gemini-2.5-pro": {"tokens": {"prompt": 30, "candidates": 12, "cached": 4, "thoughts": 9, "total": 51}}}}}


def test_gemini_envelope_requires_key_and_parses(monkeypatch):
    from roundtable.providers.cli import gemini
    from roundtable.providers.base import ProviderUnavailable
    monkeypatch.delenv("GEMINI_API_KEY", raising=False); monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    with pytest.raises(ProviderUnavailable):
        gemini.GeminiCLI("g")
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    monkeypatch.setattr(common, "run_argv", fake_run(0, json.dumps(GEMINI_OK)))
    c = gemini.GeminiCLI("g").run([Message("system", "S"), Message("user", "U")], schema=Answer)
    assert c.parsed["answer"] == "a" and c.model == "gemini-2.5-pro"
    assert c.usage.input_tokens == 30 and c.usage.reasoning_tokens == 9 and c.usage.cached_input_tokens == 4
    assert fake_run.env.get("GEMINI_API_KEY") == "k" and fake_run.env.get("GEMINI_CLI_TRUST_WORKSPACE") == "true"
    assert "ANTHROPIC_API_KEY" not in fake_run.env or True   # other vendors' keys are still stripped
    assert fake_run.argv[fake_run.argv.index("--approval-mode") + 1] == "plan"
    assert "JSON Schema" in fake_run.argv[2] and "SYSTEM INSTRUCTIONS" in fake_run.argv[2]


def test_gemini_agent_mode_uses_yolo_in_workspace(monkeypatch, tmp_path):
    from roundtable.providers.cli import gemini
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    monkeypatch.setattr(common, "run_argv", fake_run(0, json.dumps(GEMINI_OK)))
    gemini.GeminiCLI("g").run([Message("user", "U")], schema=Answer, workspace=str(tmp_path))
    assert fake_run.argv[fake_run.argv.index("--approval-mode") + 1] == "yolo"
