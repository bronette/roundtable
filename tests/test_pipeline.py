"""Offline pipeline tests on RecordedProvider. Prompt guards make the anti-sycophancy rules
executable: the critic must never see who wrote the proposal; the reviser must see the critique."""

import json
import pathlib

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


def make_cfg(roles, *, autonomy, engineer_mode=None, **budget):
    agents = {r: AgentCfg(provider="rec", model="claude-x") for r in roles}
    if "engineer" in agents:
        agents["engineer"] = AgentCfg(provider="rec", model="claude-x", mode=engineer_mode)
    return Config(
        project=ProjectCfg(name="t", objective="Implement max_drawdown", requirements=["0.0 for empty"]),
        providers={"rec": ProviderCfg(type="cli", cli="claude")},
        agents=agents, autonomy=autonomy, budget=BudgetCfg(**budget),
    )


def make_pipeline(tmp_path, recordings: dict[str, list[Recording]], *, autonomy=1, engineer_mode="answer", **budget):
    cfg = make_cfg(list(recordings), autonomy=autonomy, engineer_mode=engineer_mode, **budget)
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
    assert (tmp_path / "runs" / "t" / st.run_id / "workspace" / ".git").exists()   # greenfield workspace is a git repo
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


# ---------------------------------------------------------------- M2: implement / test / review

DD_PY = "def max_drawdown(equity):\n    peak, worst = None, 0.0\n    for x in equity:\n        if peak is None or x > peak:\n            peak = x\n        if peak and peak > 0:\n            worst = max(worst, (peak - x) / peak)\n    return float(worst)\n"
TEST_PY = "from dd import max_drawdown\n\ndef test_empty():\n    assert max_drawdown([]) == 0.0\n\ndef test_trough_end():\n    assert abs(max_drawdown([100, 120, 90]) - 0.25) < 1e-9\n"
BAD_DD_PY = "def max_drawdown(equity):\n    return 1.0\n"

IMPL_ANSWER = {"summary": "running-peak scan", "files": [{"path": "dd.py", "content": DD_PY}, {"path": "test_dd.py", "content": TEST_PY}],
               "test_command": "python -m pytest -q", "assumptions": [], "known_gaps": []}
IMPL_AGENT = {"summary": "edited in worktree", "files": [], "test_command": "python -m pytest -q", "assumptions": [], "known_gaps": []}
REVIEW_OK = {"verdict": "ACCEPT", "checks": [{"criterion_id": "AC1", "satisfied": True, "evidence": "test_dd.py::test_empty passed"}],
             "problems": [], "confidence": 0.8}
REVIEW_BAD = {"verdict": "REJECT", "checks": [{"criterion_id": "AC1", "satisfied": False, "evidence": "1 failed in output"}],
              "problems": [{"severity": "blocker", "description": "returns constant"}], "confidence": 0.9}
SYNTH_DONE = SYNTH | {"what_was_built": "dd.py and tests", "test_summary": "2 passed", "overall_verdict": "ACCEPT"}


def test_answer_mode_engineer_writes_files_tests_pass_validator_accepts(tmp_path):
    cfg, store, pipe, events = make_pipeline(tmp_path, {
        "proposer": [Recording(PROPOSAL)], "critic": [Recording(CRIT_ACCEPT)], "reviser": [],
        "engineer": [Recording(IMPL_ANSWER, expects_prompt_contains=["AC1", "return complete files", "LOCKED ACCEPTANCE"],
                               forbids_prompt_contains=["counterarguments"])],
        "validator": [Recording(REVIEW_OK, expects_prompt_contains=["2 passed", "def max_drawdown", "AC1"],
                                forbids_prompt_contains=["approach", "Running-peak scan computes"])],   # sees artifact, not the argument
        "synthesizer": [Recording(SYNTH_DONE, expects_prompt_contains=["I1", "T1", "exit_code", "RUN STATUS: implemented"])],
    }, autonomy=2)
    st = pipe.run(runs_dir=tmp_path / "runs")
    assert st.status == "implemented"
    stages = [d["to_stage"] for d in store.decisions(st.run_id)]
    assert stages == ["PROPOSE", "CRITIQUE", "IMPLEMENT", "TEST", "REVIEW", "SYNTHESIZE", "DONE"]
    ws = st.ws
    assert (ws.path / "dd.py").read_text() == DD_PY
    t = store.test_runs(st.run_id)[0]
    assert t["exit_code"] == 0 and t["passed"] == 2 and t["failed"] == 0
    impl = json.loads(store.implementations(st.run_id)[0]["body_json"])
    assert sorted(impl["changed"]) == ["dd.py", "test_dd.py"]
    assert ws.git("log", "--oneline").count("\n") == 2            # base + implementation commit
    assert (tmp_path / "runs" / "t" / st.run_id / "artifacts").exists()
    rep = st.report_path.read_text()
    assert "## Implementation" in rep and "## Test runs" in rep and "## Validator review" in rep and "roundtable/" in rep
    assert store.critiques(st.run_id)[-1]["target_kind"] == "implementation"


def test_agent_mode_engineer_edits_worktree_directly(tmp_path):
    def act(workspace):
        (pathlib.Path(workspace) / "dd.py").write_text(DD_PY)
        (pathlib.Path(workspace) / "test_dd.py").write_text(TEST_PY)
        (pathlib.Path(workspace) / "__pycache__").mkdir()
        (pathlib.Path(workspace) / "__pycache__" / "dd.pyc").write_bytes(b"x")     # agents leave caches behind
    cfg, store, pipe, _ = make_pipeline(tmp_path, {
        "proposer": [Recording(PROPOSAL)], "critic": [Recording(CRIT_ACCEPT)], "reviser": [],
        "engineer": [Recording(IMPL_AGENT, side_effect=act, expects_prompt_contains=["you are in it, on branch roundtable/"])],
        "validator": [Recording(REVIEW_OK)], "synthesizer": [Recording(SYNTH_DONE)],
    }, autonomy=2, engineer_mode="agent")
    st = pipe.run(runs_dir=tmp_path / "runs")
    assert st.status == "implemented"
    eng = pipe.agents["engineer"].provider.calls[0]
    assert eng["workspace"] == str(st.ws.path)                     # provider was told to run inside the worktree
    assert "scratch directory" not in eng["messages"][0].content    # agent-mode system prompt drops the scratch rule
    assert sorted(st.changed_paths) == ["dd.py", "test_dd.py"]           # caches are never committed


def test_failing_tests_get_one_fix_round_then_review(tmp_path):
    bad = IMPL_ANSWER | {"files": [{"path": "dd.py", "content": BAD_DD_PY}, {"path": "test_dd.py", "content": TEST_PY}]}
    cfg, store, pipe, _ = make_pipeline(tmp_path, {
        "proposer": [Recording(PROPOSAL)], "critic": [Recording(CRIT_ACCEPT)], "reviser": [],
        "engineer": [Recording(bad), Recording(IMPL_ANSWER, expects_prompt_contains=["PREVIOUS ATTEMPT FAILED", "test_trough_end", "return 1.0"])],
        "validator": [Recording(REVIEW_OK)], "synthesizer": [Recording(SYNTH_DONE)],
    }, autonomy=2, max_fix_rounds=1)
    st = pipe.run(runs_dir=tmp_path / "runs")
    stages = [d["to_stage"] for d in store.decisions(st.run_id)]
    assert stages == ["PROPOSE", "CRITIQUE", "IMPLEMENT", "TEST", "FIX", "TEST", "REVIEW", "SYNTHESIZE", "DONE"]
    tests = store.test_runs(st.run_id)
    assert [t["exit_code"] == 0 for t in tests] == [False, True]
    assert st.status == "implemented" and st.fix_round == 1


def test_fix_rounds_exhausted_still_reviews_and_reports_failure(tmp_path):
    bad = IMPL_ANSWER | {"files": [{"path": "dd.py", "content": BAD_DD_PY}, {"path": "test_dd.py", "content": TEST_PY}]}
    cfg, store, pipe, _ = make_pipeline(tmp_path, {
        "proposer": [Recording(PROPOSAL)], "critic": [Recording(CRIT_ACCEPT)], "reviser": [],
        "engineer": [Recording(bad)],
        "validator": [Recording(REVIEW_BAD, expects_prompt_contains=["2 failed", "\"exit_code\": 1"])],
        "synthesizer": [Recording(SYNTH | {"overall_verdict": "REJECT"}, expects_prompt_contains=["RUN STATUS: tests_failing"])],
    }, autonomy=2, max_fix_rounds=0)
    st = pipe.run(runs_dir=tmp_path / "runs")
    assert st.status == "tests_failing"
    assert "| AC1 | NO |" in st.report_path.read_text()


def test_engineer_cannot_write_outside_workspace(tmp_path):
    evil = IMPL_ANSWER | {"files": [{"path": "../escape.py", "content": "x"}]}
    cfg, store, pipe, _ = make_pipeline(tmp_path, {
        "proposer": [Recording(PROPOSAL)], "critic": [Recording(CRIT_ACCEPT)], "reviser": [],
        "engineer": [Recording(evil)], "validator": [], "synthesizer": [Recording(SYNTH)],
    }, autonomy=2)
    st = pipe.run(runs_dir=tmp_path / "runs")
    assert st.status == "halted_error" and "outside workspace" in st.halt_reason
    assert not (tmp_path / "runs" / "t" / "escape.py").exists()


def test_existing_git_repo_gets_a_worktree_and_branch(tmp_path):
    import subprocess
    src = tmp_path / "src"; src.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=src, check=True)
    (src / "README.md").write_text("hello")
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "add", "-A"], cwd=src, check=True)
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "init"], cwd=src, check=True)
    cfg, store, pipe, _ = make_pipeline(tmp_path, {
        "proposer": [Recording(PROPOSAL)], "critic": [Recording(CRIT_ACCEPT)], "reviser": [],
        "engineer": [Recording(IMPL_ANSWER, expects_prompt_contains=["README.md"])],   # sees the repo's tree
        "validator": [Recording(REVIEW_OK)], "synthesizer": [Recording(SYNTH_DONE)],
    }, autonomy=2)
    cfg.project.repo = str(src)
    st = pipe.run(runs_dir=tmp_path / "runs")
    assert st.ws.mode == "worktree" and st.ws.source_repo == src.resolve()
    branches = subprocess.run(["git", "branch", "--list"], cwd=src, capture_output=True, text=True).stdout
    assert f"roundtable/{st.run_id}" in branches
    assert (st.ws.path / "README.md").read_text() == "hello" and (st.ws.path / "dd.py").exists()
    assert not (src / "dd.py").exists()                              # the user's checkout is untouched
    head = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=src, capture_output=True, text=True).stdout.strip()
    assert head == "main"


# ---------------------------------------------------------------- M3: resume

def test_resume_continues_from_failed_stage_without_repeating_calls(tmp_path):
    # first run: budget of 2 calls halts before REVISE
    cfg, store, pipe, _ = make_pipeline(tmp_path, {
        "proposer": [Recording(PROPOSAL)], "critic": [Recording(CRIT_REVISE)], "reviser": [], "synthesizer": [Recording(SYNTH)],
    }, max_calls=2)
    st = pipe.run(runs_dir=tmp_path / "runs")
    assert st.status == "halted_budget"
    assert [r["role"] for r in store.calls(st.run_id)] == ["proposer", "critic", "synthesizer"]

    # resume with a bigger budget and fresh agents holding only the remaining recordings
    cfg2 = make_cfg(["proposer", "critic", "reviser", "synthesizer"], autonomy=1, max_calls=10)
    pricing = Pricing({"claude-x": {"input": 1.0, "output": 2.0}})
    agents = {
        "proposer": Agent("proposer", cfg2.agents["proposer"], RecordedProvider([], name="rec-proposer"), billing="api", pricing=pricing),
        "critic": Agent("critic", cfg2.agents["critic"], RecordedProvider([Recording(CRIT_ACCEPT, expects_prompt_contains=["P2"])], name="rec-critic"), billing="api", pricing=pricing),
        "reviser": Agent("reviser", cfg2.agents["reviser"], RecordedProvider([Recording(REVISED, expects_prompt_contains=["C1", "P1"])], name="rec-reviser"), billing="api", pricing=pricing),
        "synthesizer": Agent("synthesizer", cfg2.agents["synthesizer"], RecordedProvider([Recording(SYNTH, expects_prompt_contains=["RUN STATUS: plan_accepted"])], name="rec-synthesizer"), billing="api", pricing=pricing),
    }
    events = []
    st2 = Pipeline(cfg2, store, agents, on_event=lambda s, t: events.append((s, t))).resume(st.run_id)
    assert st2.run_id == st.run_id and st2.status == "plan_accepted"
    stages = [d["to_stage"] for d in store.decisions(st.run_id)]
    assert stages[-6:] == ["REVISE", "CRITIQUE", "REVISE", "CRITIQUE", "IMPLEMENT", "SYNTHESIZE"][-6:] or "REVISE" in stages
    roles = [r["role"] for r in store.calls(st.run_id)]
    assert roles.count("proposer") == 1                       # never re-run
    assert roles == ["proposer", "critic", "synthesizer", "reviser", "critic", "synthesizer"]
    assert any("resumed by operator" in d["reason"] for d in store.decisions(st.run_id))
    assert store.get_run(st.run_id)["status"] == "plan_accepted"


def test_resume_after_engineer_failure_keeps_locked_criteria_and_workspace(tmp_path):
    cfg, store, pipe, _ = make_pipeline(tmp_path, {
        "proposer": [Recording(PROPOSAL)], "critic": [Recording(CRIT_ACCEPT)], "reviser": [],
        "engineer": [], "validator": [], "synthesizer": [Recording(SYNTH)],   # engineer has no recording → halts at IMPLEMENT
    }, autonomy=2)
    st = pipe.run(runs_dir=tmp_path / "runs")
    assert st.status == "halted_error" and store.get_run(st.run_id)["acceptance_hash"]
    cfg2 = make_cfg(["proposer", "critic", "reviser", "engineer", "validator", "synthesizer"], autonomy=2, engineer_mode="answer")
    pricing = Pricing()
    agents = {r: Agent(r, cfg2.agents[r], RecordedProvider(recs, name=f"rec-{r}"), billing="api", pricing=pricing) for r, recs in {
        "proposer": [], "critic": [], "reviser": [],
        "engineer": [Recording(IMPL_ANSWER, expects_prompt_contains=["AC1", "roundtable/"])],
        "validator": [Recording(REVIEW_OK)], "synthesizer": [Recording(SYNTH_DONE)],
    }.items()}
    st2 = Pipeline(cfg2, store, agents, on_event=lambda s, t: None).resume(st.run_id)
    assert st2.status == "implemented" and st2.ws is not None and st2.ws.path == st.ws.path
    assert [r["role"] for r in store.calls(st.run_id)][-3:] == ["engineer", "validator", "synthesizer"]
    assert (st2.ws.path / "dd.py").exists()


def test_resume_refuses_finished_run(tmp_path):
    cfg, store, pipe, _ = make_pipeline(tmp_path, {
        "proposer": [Recording(PROPOSAL)], "critic": [Recording(CRIT_ACCEPT)], "reviser": [], "synthesizer": [Recording(SYNTH)],
    })
    st = pipe.run(runs_dir=tmp_path / "runs")
    store.finish_run(st.run_id, "implemented")
    with pytest.raises(ValueError, match="nothing to resume"):
        pipe.resume(st.run_id)
