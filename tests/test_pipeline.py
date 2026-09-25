"""Offline pipeline tests on RecordedProvider. Prompt guards make the anti-sycophancy rules
executable: the critic must never see who wrote the proposal; the reviser must see the critique."""

import json

import pytest

from roundtable.agents import Agent
from roundtable.config import AgentCfg, BudgetCfg, Config, ProjectCfg, ProviderCfg
from roundtable.pipeline import Pipeline, Stage
from roundtable.pricing import Pricing
from roundtable.providers.recorded import RecordedProvider, Recording
from roundtable.store import Store

PROPOSAL = {
    "scope": "single_task", "claim": "Running-peak scan computes max drawdown", "approach": "Track peak; dd=(peak-x)/peak.",
    "assumptions": ["equity is non-negative"], "evidence": [], "risks": ["division by zero at peak 0"],
    "acceptance_criteria": [{"id": "AC1", "requirement": "0.0 for empty", "check": "test_empty"}],
    "confidence": 0.8,
}
REVISED = PROPOSAL | {
    "claim": "Running-peak scan with zero-peak guard",
    "responses_to_criticism": [{"problem": "division by zero", "action": "fixed", "reason": "guard peak<=0"},
                               {"problem": "no single-element test", "action": "deferred", "reason": "out of scope"}],
    "confidence": 0.9,
}
CRIT_REVISE = {"verdict": "REVISE", "problems": [
    {"severity": "major", "description": "division by zero when peak == 0", "location": "assumptions[0]"},
    {"severity": "minor", "description": "no single-element test", "location": "AC1"}],
    "counterarguments": [], "checks_performed": ["edge cases"], "tests_required": ["test_zero_peak"], "confidence": 0.8}
CRIT_ACCEPT = {"verdict": "ACCEPT", "problems": [], "counterarguments": [], "checks_performed": ["zero peak", "empty"],
               "tests_required": [], "confidence": 0.85}
CRIT_REJECT = {"verdict": "REJECT", "problems": [{"severity": "blocker", "description": "approach cannot meet objective"}],
               "counterarguments": ["use a different metric"], "checks_performed": [], "tests_required": [], "confidence": 0.9}
SYNTH = {"what_was_built": "Nothing yet; plan accepted", "test_summary": "no tests were run",
         "disagreements": [{"topic": "single-element test", "positions": [{"role": "critic", "position": "needed"},
                                                                         {"role": "proposer", "position": "deferred"}], "resolved": False}],
         "remaining_risks": ["negative equity undefined"], "recommended_next_experiment": "property test", "overall_verdict": "REVISE",
         "confidence": 0.7}


def make_cfg(**budget):
    return Config(
        project=ProjectCfg(name="t", objective="Implement max_drawdown", requirements=["0.0 for empty"]),
        providers={"rec": ProviderCfg(type="cli", cli="claude")},
        agents={r: AgentCfg(provider="rec", model="claude-x") for r in ("proposer", "critic", "reviser", "synthesizer")},
        budget=BudgetCfg(**budget),
    )


def make_pipeline(tmp_path, recordings: dict[str, list[Recording]], **budget):
    cfg = make_cfg(**budget)
    store = Store(tmp_path / "db.sqlite")
    pricing = Pricing({"claude-x": {"input": 1.0, "output": 2.0}})
    agents = {role: Agent(role, cfg.agents[role], RecordedProvider(recs, name=f"rec-{role}"), billing="api", pricing=pricing)
              for role, recs in recordings.items()}
    events = []
    pipe = Pipeline(cfg, store, agents, on_event=lambda s, t: events.append((s, t)))
    return cfg, store, pipe, events


def test_happy_path_revise_then_accept(tmp_path):
    cfg, store, pipe, events = make_pipeline(tmp_path, {
        "proposer": [Recording(PROPOSAL, expects_prompt_contains=["OBJECTIVE:", "0.0 for empty", "at most 3 revision"])],
        "critic": [
            Recording(CRIT_REVISE, expects_prompt_contains=["P1", "<evidence id=\"P1\""],
                      forbids_prompt_contains=["claude", "rec-proposer", "proposer"]),
            Recording(CRIT_ACCEPT, expects_prompt_contains=["P2", "responses_to_criticism"],
                      forbids_prompt_contains=["claude", "rec-proposer"]),
        ],
        "reviser": [Recording(REVISED, expects_prompt_contains=["division by zero when peak == 0", "C1", "P1"])],
        "synthesizer": [Recording(SYNTH, expects_prompt_contains=["C1", "C2", "P1", "P2", "RUN STATUS: plan_accepted", "DECISIONS"])],
    })
    st = pipe.run(runs_dir=tmp_path / "runs")
    assert st.status == "plan_accepted"
    assert st.synthesis.overall_verdict == "REVISE"
    stages = [d["to_stage"] for d in store.decisions(st.run_id)]
    assert stages == ["PROPOSE", "CRITIQUE", "REVISE", "CRITIQUE", "IMPLEMENT", "SYNTHESIZE", "DONE"]
    run = store.get_run(st.run_id)
    assert run["status"] == "plan_accepted" and run["acceptance_hash"].startswith("sha256:")
    assert json.loads(run["acceptance_json"])[0]["id"] == "AC1"
    props = {p["id"]: p for p in store.proposals(st.run_id)}
    assert props["P1"]["status"] == "superseded" and props["P2"]["status"] == "accepted" and props["P2"]["revision_of"] == "P1"
    assert [c["verdict"] for c in store.critiques(st.run_id)] == ["REVISE", "ACCEPT"]
    report = st.report_path.read_text()
    assert "UNRESOLVED" in report and "deferred" in report and "AC1" in report
    assert store.usage(st.run_id)["total"]["calls"] == 5


def test_reject_ends_run_without_reviser(tmp_path):
    cfg, store, pipe, _ = make_pipeline(tmp_path, {
        "proposer": [Recording(PROPOSAL)], "critic": [Recording(CRIT_REJECT)], "reviser": [],
        "synthesizer": [Recording(SYNTH | {"overall_verdict": "REJECT"}, expects_prompt_contains=["RUN STATUS: rejected"])],
    })
    st = pipe.run(runs_dir=tmp_path / "runs")
    assert st.status == "rejected"
    assert store.proposals(st.run_id)[0]["status"] == "rejected"
    assert store.get_run(st.run_id)["acceptance_json"] is None


def test_max_rounds_implements_with_open_criticisms(tmp_path):
    cfg, store, pipe, _ = make_pipeline(tmp_path, {
        "proposer": [Recording(PROPOSAL)],
        "critic": [Recording(CRIT_REVISE), Recording(CRIT_REVISE)],
        "reviser": [Recording(REVISED)],
        "synthesizer": [Recording(SYNTH)],
    }, max_rounds=1)
    st = pipe.run(runs_dir=tmp_path / "runs")
    assert st.unresolved is True and st.status == "plan_accepted"
    reasons = [d["reason"] for d in store.decisions(st.run_id)]
    assert any("max_rounds" in r for r in reasons)
    assert store.get_run(st.run_id)["acceptance_hash"] is not None


def test_budget_halts_and_fact_only_report_is_written(tmp_path):
    cfg, store, pipe, events = make_pipeline(tmp_path, {
        "proposer": [Recording(PROPOSAL)],
        "critic": [Recording(CRIT_REVISE)],
        "reviser": [Recording(REVISED)],
        "synthesizer": [Recording(SYNTH, expects_prompt_contains=["RUN STATUS: halted_budget"])],
    }, max_calls=2)
    st = pipe.run(runs_dir=tmp_path / "runs")
    assert st.status == "halted_budget" and "max_calls" in st.halt_reason
    # proposer + critic ran, reviser was blocked, synthesizer still got its one call
    assert [r["role"] for r in store.calls(st.run_id)] == ["proposer", "critic", "synthesizer"]
    assert st.report_path.exists() and "halted_budget" in st.report_path.read_text()


def test_needs_decomposition_stops_before_critique(tmp_path):
    big = {"scope": "needs_decomposition", "suggested_split": ["data layer", "api", "ui"], "claim": "too big",
           "approach": "", "acceptance_criteria": [], "confidence": 0.9}
    cfg, store, pipe, _ = make_pipeline(tmp_path, {
        "proposer": [Recording(big)], "critic": [], "reviser": [],
        "synthesizer": [Recording(SYNTH, expects_prompt_contains=["needs_decomposition"])],
    })
    st = pipe.run(runs_dir=tmp_path / "runs")
    assert st.status == "needs_decomposition"
    assert [q["question"] for q in store.open_questions(st.run_id)] == ["Sub-task 1: data layer", "Sub-task 2: api", "Sub-task 3: ui"]
    assert "Sub-task 2: api" in st.report_path.read_text()


def test_needs_evidence_records_open_questions(tmp_path):
    crit = {"verdict": "NEEDS_EVIDENCE", "problems": [{"severity": "major", "description": "claim unverified"}],
            "counterarguments": [], "checks_performed": [], "tests_required": ["benchmark against reference impl"], "confidence": 0.6}
    cfg, store, pipe, _ = make_pipeline(tmp_path, {
        "proposer": [Recording(PROPOSAL)], "critic": [Recording(crit)], "reviser": [], "synthesizer": [Recording(SYNTH)],
    })
    st = pipe.run(runs_dir=tmp_path / "runs")
    assert st.status == "needs_input"
    assert store.open_questions(st.run_id)[0]["question"] == "NEEDS_EVIDENCE: benchmark against reference impl"


def test_autonomy_zero_stops_after_first_critique(tmp_path):
    cfg, store, pipe, _ = make_pipeline(tmp_path, {
        "proposer": [Recording(PROPOSAL)], "critic": [Recording(CRIT_REVISE)], "reviser": [], "synthesizer": [Recording(SYNTH)],
    })
    cfg.autonomy = 0
    st = pipe.run(runs_dir=tmp_path / "runs")
    assert st.status == "advised"
    assert [r["role"] for r in store.calls(st.run_id)] == ["proposer", "critic", "synthesizer"]


def test_dishonest_critique_is_repaired(tmp_path):
    lazy_accept = {"verdict": "ACCEPT", "problems": [], "counterarguments": [], "checks_performed": [], "tests_required": [], "confidence": 0.9}
    cfg, store, pipe, _ = make_pipeline(tmp_path, {
        "proposer": [Recording(PROPOSAL)],
        "critic": [Recording(lazy_accept), Recording(CRIT_ACCEPT, expects_prompt_contains=["did not validate", "checks_performed"])],
        "reviser": [], "synthesizer": [Recording(SYNTH)],
    })
    st = pipe.run(runs_dir=tmp_path / "runs")
    crit_calls = [r for r in store.calls(st.run_id) if r["role"] == "critic"]
    assert [c["valid"] for c in crit_calls] == [0, 1]
    assert st.status == "plan_accepted"


def test_provider_failure_halts_with_report(tmp_path):
    cfg, store, pipe, _ = make_pipeline(tmp_path, {
        "proposer": [Recording(PROPOSAL)], "critic": [], "reviser": [],   # critic has no recording → ProviderError
        "synthesizer": [Recording(SYNTH, expects_prompt_contains=["RUN STATUS: halted_error"])],
    })
    st = pipe.run(runs_dir=tmp_path / "runs")
    assert st.status == "halted_error" and "ProviderError" in st.halt_reason
    assert st.report_path.exists()
