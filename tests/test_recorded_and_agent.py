import pytest

from roundtable.agents import Agent
from roundtable.config import AgentCfg
from roundtable.pricing import Pricing
from roundtable.providers.base import Message, SchemaError
from roundtable.providers.recorded import PromptGuardViolation, RecordedProvider, Recording
from roundtable.schemas import Answer


def test_guards_enforced():
    p = RecordedProvider([Recording({"ok": True}, expects_prompt_contains=["hello"], forbids_prompt_contains=["claude"])])
    with pytest.raises(PromptGuardViolation):
        p.run([Message("user", "written by claude, hello")])
    p = RecordedProvider([Recording({"ok": True}, expects_prompt_contains=["hello"])])
    with pytest.raises(PromptGuardViolation):
        p.run([Message("user", "goodbye")])


def test_recordings_consumed_in_order():
    p = RecordedProvider([Recording({"n": 1}), Recording({"n": 2})])
    assert p.run([Message("user", "x")]).parsed == {"n": 1}
    assert p.run([Message("user", "x")]).parsed == {"n": 2}


def _agent(recordings):
    return Agent("proposer", AgentCfg(provider="rec", model="m"), RecordedProvider(recordings),
                 billing="api", pricing=Pricing({"recorded": {"input": 1.0, "output": 2.0}}))


def test_agent_repairs_once_and_logs_both_attempts(store, run):
    a = _agent([
        Recording({"answer": "x"}),                                   # missing fields → invalid
        Recording({"answer": "x", "reasoning_summary": "y", "confidence": 0.5}),
    ])
    res = a.call(store=store, run_id=run, stage="PROPOSE", system="S", prompt="P", schema=Answer)
    assert res.attempts == 2
    assert isinstance(res.output, Answer) and res.output.confidence == 0.5
    rows = store.calls(run)
    assert [r["valid"] for r in rows] == [0, 1]
    assert "schema:" in rows[0]["error"]
    # the repair prompt carried the bad output and the error
    repair_msgs = a.provider.calls[1]["messages"]
    assert repair_msgs[2].role == "assistant" and "did not validate" in repair_msgs[3].content
    assert rows[1]["cost_usd"] == pytest.approx((100 * 1.0 + 50 * 2.0) / 1e6)


def test_agent_gives_up_after_two_failures(store, run):
    a = _agent([Recording({"answer": "x"}), Recording({"answer": "x"})])
    with pytest.raises(SchemaError):
        a.call(store=store, run_id=run, stage="PROPOSE", system="S", prompt="P", schema=Answer)
    assert len(store.calls(run)) == 2


def test_agent_raw_mode_no_schema(store, run):
    a = _agent([Recording("free text")])
    res = a.call(store=store, run_id=run, stage="X", system="S", prompt="P", schema=None)
    assert res.output is None and res.completion.text == "free text"


def test_fallback_answers_after_primary_fails_twice(store, run):
    primary = RecordedProvider([Recording({"answer": "x"}), Recording({"answer": "x"})], name="grok")
    backup = RecordedProvider([Recording({"answer": "b", "reasoning_summary": "y", "confidence": 0.7})], name="claude")
    fb = Agent("critic", AgentCfg(provider="claude"), backup, billing="api", pricing=Pricing())
    a = Agent("critic", AgentCfg(provider="grok"), primary, billing="api", pricing=Pricing(), fallback=fb)
    res = a.call(store=store, run_id=run, stage="CRITIQUE", system="S", prompt="P", schema=Answer)
    assert res.output.answer == "b" and res.completion.provider == "claude"
    rows = store.calls(run)
    assert [(r["provider"], r["valid"]) for r in rows] == [("grok", 0), ("grok", 0), ("claude", 1)]
    q = store.open_questions(run)
    assert len(q) == 1 and "grok failed at CRITIQUE" in q[0]["question"] and "claude answered instead" in q[0]["question"]


def test_no_fallback_still_raises(store, run):
    a = Agent("critic", AgentCfg(provider="grok"), RecordedProvider([Recording({"answer": "x"})] * 2, name="grok"),
              billing="api", pricing=Pricing())
    with pytest.raises(SchemaError):
        a.call(store=store, run_id=run, stage="CRITIQUE", system="S", prompt="P", schema=Answer)


def _raising_provider(errors, then=None, name="p"):
    """A provider that raises the given exceptions in order, then answers."""
    from roundtable.providers.base import Completion, Usage
    class P:
        def __init__(self):
            self.name = name; self.calls = 0
        def run(self, messages, **kw):
            self.calls += 1
            if self.calls <= len(errors):
                raise errors[self.calls - 1]
            return Completion(text='{"answer":"a","reasoning_summary":"r","confidence":0.5}',
                              parsed={"answer": "a", "reasoning_summary": "r", "confidence": 0.5},
                              usage=Usage(10, 5), provider=name, model="m", latency_ms=1)
    return P()


def test_transient_errors_are_retried_and_logged(store, run):
    from roundtable.providers.base import ProviderError
    p = _raising_provider([ProviderError("503 UNAVAILABLE"), ProviderError("timed out")])
    a = Agent("critic", AgentCfg(provider="p", retries=2), p, billing="api", pricing=Pricing())
    res = a.call(store=store, run_id=run, stage="CRITIQUE", system="S", prompt="P", schema=Answer)
    assert res.output.answer == "a" and res.attempts == 3 and p.calls == 3
    rows = store.calls(run)
    assert [r["valid"] for r in rows] == [0, 0, 1]
    assert "503" in rows[0]["error"] and rows[1]["attempt"] == 2 and rows[2]["attempt"] == 3


def test_retries_exhausted_raises(store, run):
    from roundtable.providers.base import ProviderError
    p = _raising_provider([ProviderError("x")] * 3)
    a = Agent("critic", AgentCfg(provider="p", retries=1), p, billing="api", pricing=Pricing())
    with pytest.raises(ProviderError):
        a.call(store=store, run_id=run, stage="CRITIQUE", system="S", prompt="P", schema=Answer)
    assert p.calls == 2


def test_usage_limit_and_unavailable_are_not_retried(store, run):
    from roundtable.providers.base import ProviderUnavailable, UsageLimitError
    for exc in (UsageLimitError("quota"), ProviderUnavailable("no key")):
        p = _raising_provider([exc])
        a = Agent("critic", AgentCfg(provider="p", retries=3), p, billing="api", pricing=Pricing())
        with pytest.raises(type(exc)):
            a.call(store=store, run_id=run, stage="CRITIQUE", system="S", prompt="P", schema=Answer)
        assert p.calls == 1


def test_cost_falls_back_to_reported_estimate(store, run):
    from roundtable.providers.base import Completion, Usage
    rec = RecordedProvider([Recording({"answer": "a", "reasoning_summary": "r", "confidence": 0.5}, model="grok-x")])
    orig = rec.run
    def run_with_estimate(messages, **kw):
        c = orig(messages, **kw); c.reported_cost_usd = 0.0135; return c
    rec.run = run_with_estimate
    a = Agent("critic", AgentCfg(provider="rec"), rec, billing="subscription", pricing=Pricing())
    a.call(store=store, run_id=run, stage="CRITIQUE", system="S", prompt="P", schema=Answer)
    row = store.calls(run)[0]
    assert row["cost_usd"] == pytest.approx(0.0135) and row["cost_source"] == "reported"
    assert store.usage(run)["total"]["cost_known"] is True
