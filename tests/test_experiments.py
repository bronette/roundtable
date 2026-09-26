import json
import subprocess

import pytest

from roundtable.agents import Agent
from roundtable.config import AgentCfg, Config, ProjectCfg, ProviderCfg
from roundtable.experiments import ExperimentError, interpret, load_prereg, mechanical_decision, preregister, run_experiment
from roundtable.pricing import Pricing
from roundtable.providers.recorded import RecordedProvider, Recording
from roundtable.schemas import Critique, ExperimentPrereg, Interpretation, TradingCritique
from roundtable.store import Store

PREREG = dict(
    hypothesis="The strategy's mean net edge per contract is positive after fees.",
    expected_result="mean_edge > 0.01 with n >= 300",
    method="run analyze on the frozen cohort",
    data="cohort.csv, 2026-06-01..2026-08-31, never used for tuning",
    success_criteria=["mean_edge >= 0.01 with n >= 300"],
    failure_criteria=["ci_lower <= -0.01 with n >= 300"],
    n_trials=1,
    command='python -c "import json,os; m={\\"mean_edge\\":-0.078,\\"ci_lower\\":-0.112,\\"n\\":1402}; open(os.environ[\\"ROUNDTABLE_METRICS\\"],\\"w\\").write(json.dumps(m)); print(\\"done\\")"',
)


def make_cfg(tmp_path, **project):
    cfg = Config(project=ProjectCfg(name="exp", objective="o", domain="trading", **project),
                 providers={"rec": ProviderCfg(type="cli", cli="claude")},
                 agents={r: AgentCfg(provider="rec") for r in ("synthesizer", "critic")}, runs_dir=str(tmp_path / "runs"))
    cfg.source_path = str(tmp_path / "project.yaml")
    return cfg


def test_preregister_locks_and_hashes(tmp_path):
    cfg = make_cfg(tmp_path)
    store = Store(tmp_path / "db")
    eid, h, body = preregister(cfg, store, ExperimentPrereg(**PREREG))
    assert eid == "E1" and h.startswith("sha256:")
    row = store.experiment(eid)
    assert json.loads(row["prereg_json"])["failure_criteria"] == PREREG["failure_criteria"] and row["result_json"] is None


def test_run_records_metrics_and_refuses_reruns(tmp_path):
    cfg = make_cfg(tmp_path)
    store = Store(tmp_path / "db")
    eid, _, _ = preregister(cfg, store, ExperimentPrereg(**PREREG))
    r = run_experiment(cfg, store, eid, runs_dir=tmp_path / "runs")
    assert r.exit_code == 0 and r.metrics == {"mean_edge": -0.078, "ci_lower": -0.112, "n": 1402}
    assert (tmp_path / "runs" / "exp" / "experiments" / eid / "output.txt").read_text().startswith("done")
    with pytest.raises(ExperimentError, match="already has a recorded result"):
        run_experiment(cfg, store, eid, runs_dir=tmp_path / "runs")


def test_tampered_preregistration_is_refused(tmp_path):
    cfg = make_cfg(tmp_path)
    store = Store(tmp_path / "db")
    eid, _, _ = preregister(cfg, store, ExperimentPrereg(**PREREG))
    body = json.loads(store.experiment(eid)["prereg_json"]); body["failure_criteria"] = ["ci_lower <= -0.5"]   # moving the goalposts
    store.db.execute("UPDATE experiments SET prereg_json=? WHERE id=?", (json.dumps(body, sort_keys=True), eid))
    with pytest.raises(ExperimentError, match="altered after locking"):
        run_experiment(cfg, store, eid, runs_dir=tmp_path / "runs")


def test_metrics_from_stdout_json_line_and_pinned_commit(tmp_path):
    src = tmp_path / "src"; src.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=src, check=True)
    (src / "analyze.py").write_text('import json; print(json.dumps({"n": 5}))')
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "add", "-A"], cwd=src, check=True)
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "a"], cwd=src, check=True)
    cfg = make_cfg(tmp_path, repo=str(src))
    store = Store(tmp_path / "db")
    eid, _, body = preregister(cfg, store, ExperimentPrereg(**PREREG | {"command": "python analyze.py"}))
    assert body["repo_commit"] and body["repo"] == str(src)
    (src / "analyze.py").write_text('import json; print(json.dumps({"n": 999}))')     # later edit must not affect the run
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qam", "b"], cwd=src, check=True)
    r = run_experiment(cfg, store, eid, runs_dir=tmp_path / "runs")
    assert r.metrics == {"n": 5} and r.commit == body["repo_commit"]


INTERP_KILL = {"decision": "KILL", "success_reads": [{"criterion": "mean_edge >= 0.01 with n >= 300", "met": False, "evidence": "mean_edge=-0.078, n=1402"}],
               "failure_reads": [{"criterion": "ci_lower <= -0.01 with n >= 300", "met": True, "evidence": "ci_lower=-0.112, n=1402"}],
               "interpretation": "The failure criterion is met at n=1402.", "caveats": [], "confidence": 0.9}
REVIEW_OK = {"verdict": "ACCEPT", "problems": [], "decision_should_be": None, "confidence": 0.9}


def _agents(recs_interp, recs_critic):
    pr = Pricing()
    i = Agent("synthesizer", AgentCfg(provider="rec"), RecordedProvider(recs_interp, name="rec-i"), billing="api", pricing=pr)
    c = Agent("critic", AgentCfg(provider="rec"), RecordedProvider(recs_critic, name="rec-c"), billing="api", pricing=pr)
    return i, c


def test_interpret_records_decision_and_review(tmp_path):
    cfg = make_cfg(tmp_path)
    store = Store(tmp_path / "db")
    eid, h, _ = preregister(cfg, store, ExperimentPrereg(**PREREG))
    run_experiment(cfg, store, eid, runs_dir=tmp_path / "runs")
    i, c = _agents([Recording(INTERP_KILL, expects_prompt_contains=[h[:19], "ci_lower", "-0.112", "locked"])],
                   [Recording(REVIEW_OK, expects_prompt_contains=["INTERPRETATION under review", "KILL"])])
    out = interpret(cfg, store, eid, interpreter=i, critic=c)
    assert out.decision == "KILL" and out.overridden is None
    row = store.experiment(eid)
    assert row["decision"] == "KILL" and json.loads(row["result_json"])["review"]["verdict"] == "ACCEPT"
    assert [r["role"] for r in store.calls(out.run_id)] == ["synthesizer", "critic"]


def test_code_overrides_a_soft_decision(tmp_path):
    cfg = make_cfg(tmp_path)
    store = Store(tmp_path / "db")
    eid, _, _ = preregister(cfg, store, ExperimentPrereg(**PREREG))
    run_experiment(cfg, store, eid, runs_dir=tmp_path / "runs")
    soft = INTERP_KILL | {"decision": "INCONCLUSIVE"}          # reads say a failure criterion is met, but the model flinched
    i, c = _agents([Recording(soft)], [Recording(REVIEW_OK)])
    out = interpret(cfg, store, eid, interpreter=i, critic=c)
    assert out.decision == "KILL" and "the reads imply KILL" in out.overridden


def test_critic_disagreement_makes_it_inconclusive(tmp_path):
    cfg = make_cfg(tmp_path)
    store = Store(tmp_path / "db")
    eid, _, _ = preregister(cfg, store, ExperimentPrereg(**PREREG))
    run_experiment(cfg, store, eid, runs_dir=tmp_path / "runs")
    happy = {"decision": "PASS", "success_reads": [{"criterion": "mean_edge >= 0.01 with n >= 300", "met": True, "evidence": "mean_edge=-0.078"}],
             "failure_reads": [{"criterion": "ci_lower <= -0.01 with n >= 300", "met": False, "evidence": "ci_lower=-0.112"}],
             "interpretation": "Looks good.", "caveats": [], "confidence": 0.7}
    review = {"verdict": "REJECT", "problems": [{"severity": "blocker", "description": "mean_edge is negative; success read is false; failure criterion is met"}],
              "decision_should_be": "KILL", "confidence": 0.95}
    i, c = _agents([Recording(happy)], [Recording(review)])
    out = interpret(cfg, store, eid, interpreter=i, critic=c)
    assert out.decision == "INCONCLUSIVE" and "critic says KILL" in out.overridden
    assert store.open_questions(out.run_id)


def test_mechanical_rule():
    def mk(s, f): return Interpretation(decision="PASS", success_reads=[{"criterion": "s", "met": s, "evidence": "e"}],
                                        failure_reads=[{"criterion": "f", "met": f, "evidence": "e"}], interpretation="x", confidence=0.5)
    assert mechanical_decision(mk(True, False)) == "PASS"
    assert mechanical_decision(mk(True, True)) == "KILL"
    assert mechanical_decision(mk(False, False)) == "INCONCLUSIVE"
    assert mechanical_decision(mk(None, None)) == "INCONCLUSIVE"


def test_trading_critique_requires_full_checklist():
    base = {"verdict": "ACCEPT", "problems": [], "checks_performed": ["x"], "confidence": 0.8}
    with pytest.raises(Exception, match="trading_checks missing"):
        TradingCritique(**base, trading_checks=[{"name": "look_ahead_bias", "status": "ok", "note": "n"}])
    from roundtable.schemas import TRADING_CHECKS
    full = [{"name": n, "status": "not_applicable", "note": "n"} for n in TRADING_CHECKS]
    assert len(TradingCritique(**base, trading_checks=full).trading_checks) == 15
    assert Critique.model_validate({**base, "trading_checks": full})                     # base schema still reads it


def test_domain_addenda_reach_prompts():
    from roundtable.context import system_prompt
    assert "TRADING DOMAIN CHECKLIST" in system_prompt("critic", domain="trading")
    assert "TRADING DOMAIN RULES" in system_prompt("proposer", domain="trading")
    assert "TRADING" not in system_prompt("critic")
    assert "TRADING" not in system_prompt("engineer", domain="trading")      # no addendum file → unchanged


def test_load_prereg_yaml(tmp_path):
    p = tmp_path / "e.yaml"
    p.write_text("hypothesis: h\nexpected_result: r\nmethod: m\ndata: d\nsuccess_criteria: [a]\nfailure_criteria: [b]\ncommand: \"echo hi\"\n")
    assert load_prereg(p).n_trials == 1
