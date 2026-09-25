from roundtable.providers.base import Completion, Message, Usage


def _completion(**kw):
    base = dict(text='{"a":1}', parsed={"a": 1}, usage=Usage(100, 20, 5), provider="p", model="m",
                latency_ms=10, request_id="req", stop_reason="end", reported_cost_usd=0.01, argv=["x"])
    base.update(kw)
    return Completion(**base)


def test_record_call_and_usage_aggregate(store, run):
    msgs = [Message("system", "s"), Message("user", "u")]
    cid = store.record_call(run_id=run, stage="PROPOSE", role="proposer", provider="p", model="m", attempt=1,
                            messages=msgs, context_refs=["P1"], schema_name="Answer", completion=_completion(),
                            valid=True, error=None, cost_usd=0.002, billing="subscription", started_at="t0")
    store.record_call(run_id=run, stage="CRITIQUE", role="critic", provider="p", model="m", attempt=1,
                      messages=msgs, context_refs=[], schema_name=None, completion=_completion(),
                      valid=True, error=None, cost_usd=None, billing="api", started_at="t1")
    rows = store.calls(run)
    assert [r["id"] for r in rows][0] == cid
    assert rows[0]["prompt_hash"].startswith("sha256:")
    assert rows[0]["billing"] == "subscription"
    u = store.usage(run)
    assert u["total"]["calls"] == 2
    assert u["total"]["tokens"] == 240
    assert u["per_provider"]["p"]["input_tokens"] == 200


def test_unknown_cost_stays_null(store, run):
    msgs = [Message("user", "u")]
    store.record_call(run_id=run, stage="S", role="r", provider="p", model="m", attempt=1, messages=msgs,
                      context_refs=[], schema_name=None, completion=_completion(), valid=True, error=None,
                      cost_usd=None, billing="api", started_at="t")
    assert store.usage(run)["per_provider"]["p"]["cost_usd"] is None
    store.record_call(run_id=run, stage="S", role="r", provider="p", model="m", attempt=1, messages=msgs,
                      context_refs=[], schema_name=None, completion=_completion(), valid=True, error=None,
                      cost_usd=0.5, billing="api", started_at="t")
    assert store.usage(run)["per_provider"]["p"]["cost_usd"] == 0.5
    assert store.usage(run)["total"]["cost_known"] is True


def test_failed_call_without_completion(store, run):
    store.record_call(run_id=run, stage="S", role="r", provider="p", model="m", attempt=1,
                      messages=[Message("user", "u")], context_refs=[], schema_name=None, completion=None,
                      valid=False, error="provider error", cost_usd=None, billing="api", started_at="t")
    assert store.usage(run)["total"]["calls"] == 0
    assert store.calls(run)[0]["valid"] == 0


def test_stage_and_decisions(store, run):
    store.set_stage(run, "CRITIQUE", round=1)
    store.record_decision(run, "PROPOSE", "CRITIQUE", "proposal valid", ["P1"])
    r = store.get_run(run)
    assert r["stage"] == "CRITIQUE" and r["round"] == 1
    store.finish_run(run, "done")
    assert store.get_run(run)["status"] == "done"
