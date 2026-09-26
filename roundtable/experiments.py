"""Pre-registered experiments: lock the criteria, run the thing, read the numbers, have the
reading checked. The decision rule is applied by code; the models only read and review."""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from roundtable import actions, context
from roundtable.agents import Agent
from roundtable.config import Config
from roundtable.providers.cli.common import scrubbed_env
from roundtable.schemas import CriterionRead, Decision, ExperimentPrereg, ExperimentResult, Interpretation, InterpretationReview, Verdict
from roundtable.store import Store
from roundtable.util import sha256_text


class ExperimentError(Exception):
    pass


def load_prereg(path: str | Path) -> ExperimentPrereg:
    with open(path) as f:
        data = yaml.safe_load(f) or {}
    return ExperimentPrereg.model_validate(data)


def preregister(cfg: Config, store: Store, prereg: ExperimentPrereg) -> tuple[str, str, dict[str, Any]]:
    """Pin the code and lock the criteria. Returns (experiment id, hash, the locked body)."""
    body = prereg.model_dump(mode="json")
    repo = prereg.repo or cfg.project.repo
    if repo:
        repo_path = cfg.resolve_path(repo)
        if not repo_path.is_dir():
            raise ExperimentError(f"repo path does not exist: {repo_path}")
        body["repo"] = str(repo_path)
        r = subprocess.run(["git", "-C", str(repo_path), "rev-parse", "HEAD"], capture_output=True, text=True)
        body["repo_commit"] = r.stdout.strip() if r.returncode == 0 else None
    project = store.latest_project(cfg.project.name)
    project_id = project["id"] if project else store.create_project(
        name=cfg.project.name, objective=cfg.project.objective, requirements=cfg.project.requirements, config=cfg.model_dump(mode="json"))
    eid, h = store.preregister(project_id=project_id, prereg=body)
    return eid, h, body


def _intact(row) -> bool:
    return sha256_text(row["prereg_json"]) == row["prereg_hash"]


def run_experiment(cfg: Config, store: Store, eid: str, *, runs_dir: Path, timeout_s: float = 3600.0) -> ExperimentResult:
    row = store.experiment(eid)
    if row is None:
        raise ExperimentError(f"no experiment {eid}")
    if not _intact(row):
        raise ExperimentError(f"{eid}: pre-registration was altered after locking; refusing to run")
    if row["result_json"]:
        raise ExperimentError(f"{eid} already has a recorded result; pre-register a new experiment instead of re-running")
    prereg = json.loads(row["prereg_json"])
    exp_dir = runs_dir / cfg.project.name / "experiments" / eid
    exp_dir.mkdir(parents=True, exist_ok=True)
    cwd = exp_dir / "workspace"
    commit = prereg.get("repo_commit")
    if prereg.get("repo"):
        ws = actions.prepare_workspace(exp_dir, prereg["repo"], f"exp-{eid}", commit=commit)
        cwd = ws.path
    else:
        cwd.mkdir(exist_ok=True)
    env = scrubbed_env(keep_api_keys=False)
    metrics_path = exp_dir / "metrics.json"
    env["ROUNDTABLE_METRICS"] = str(metrics_path)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    command = prereg["command"]
    argv = shlex.split(command)
    if argv and argv[0] in ("python", "python3"):
        argv[0] = str(cfg.resolve_path(cfg.project.python)) if cfg.project.python else sys.executable
        command_run = " ".join(shlex.quote(a) for a in argv)
    else:
        command_run = command
    t0 = time.monotonic()
    try:
        p = subprocess.run(command_run, shell=True, cwd=cwd, env=env, capture_output=True, text=True,
                           timeout=timeout_s, stdin=subprocess.DEVNULL)
        out, rc, timed_out = p.stdout + p.stderr, p.returncode, False
    except subprocess.TimeoutExpired as e:
        out = ((e.stdout or b"") if isinstance(e.stdout, str) else "") + "\n(timed out)"
        rc, timed_out = 124, True
    duration = round(time.monotonic() - t0, 2)
    metrics: dict[str, Any] = {}
    if metrics_path.exists():
        try:
            metrics = json.loads(metrics_path.read_text())
        except json.JSONDecodeError:
            metrics = {"_error": "metrics file is not valid JSON"}
    else:
        metrics = _last_json_object(out)
    (exp_dir / "output.txt").write_text(out)
    result = ExperimentResult(command=command_run, exit_code=rc, metrics={k: v for k, v in metrics.items() if isinstance(v, (int, float, str, bool)) or v is None},
                              stdout_tail=out[-4000:], duration_s=duration, commit=commit, timed_out=timed_out)
    intact = store.record_result(eid, result.model_dump(mode="json"))
    if not intact:
        raise ExperimentError(f"{eid}: pre-registration hash mismatch at result time")
    return result


def _last_json_object(text: str) -> dict[str, Any]:
    """The last JSON object printed to stdout, single-line or pretty-printed, ignoring trailing text."""
    dec = json.JSONDecoder()
    best: tuple[int, int, dict[str, Any]] | None = None     # (span, start, obj): the outermost object wins
    pos = text.rfind("{")
    tries = 0
    while pos != -1 and tries < 400:
        try:
            obj, end = dec.raw_decode(text[pos:])
            if isinstance(obj, dict) and (best is None or (end, pos) > (best[0], best[1])):
                best = (end, pos, obj)
        except json.JSONDecodeError:
            pass
        pos = text.rfind("{", 0, pos)
        tries += 1
    return best[2] if best else {}


def mechanical_decision(interp: Interpretation) -> Decision:
    if any(r.met is True for r in interp.failure_reads):
        return Decision.KILL
    if any(r.met is True for r in interp.success_reads) and not any(r.met is True for r in interp.failure_reads):
        return Decision.PASS
    return Decision.INCONCLUSIVE


@dataclass
class InterpretOutcome:
    decision: Decision
    interpretation: Interpretation
    review: InterpretationReview | None
    overridden: str | None      # note when code changed the model's decision
    run_id: str


def interpret(cfg: Config, store: Store, eid: str, *, interpreter: Agent, critic: Agent | None, on_event=None) -> InterpretOutcome:
    row = store.experiment(eid)
    if row is None or not row["result_json"]:
        raise ExperimentError(f"{eid} has no recorded result to interpret")
    if not _intact(row):
        raise ExperimentError(f"{eid}: pre-registration was altered after locking")
    prereg = json.loads(row["prereg_json"])
    result = json.loads(row["result_json"])
    result = {k: v for k, v in result.items() if k not in ("interpretation", "review")}
    project_id = row["project_id"]
    run_id = store.create_run(project_id=project_id, workspace_path="", stage="EXPERIMENT")
    emit = on_event or (lambda s, t: None)
    res = interpreter.call(store=store, run_id=run_id, stage="EXPERIMENT", system=context.system_prompt("interpreter", domain=cfg.project.domain),
                           prompt=context.interpreter_pack(prereg, row["prereg_hash"], result), schema=Interpretation, context_refs=[eid])
    interp: Interpretation = res.output  # type: ignore[assignment]
    emit("EXPERIMENT", f"interpreter → {res.completion.model}: {interp.decision} (confidence {interp.confidence:.2f})")
    decision = interp.decision
    overridden = None
    mech = mechanical_decision(interp)
    if mech != decision:
        overridden = f"interpreter said {decision}; the reads imply {mech}; {mech} recorded"
        decision = mech
        emit("EXPERIMENT", overridden)
    review = None
    if critic is not None:
        rres = critic.call(store=store, run_id=run_id, stage="EXPERIMENT", system=context.system_prompt("experiment_critic", domain=cfg.project.domain),
                           prompt=context.experiment_critic_pack(prereg, row["prereg_hash"], result, interp.model_dump(mode="json")),
                           schema=InterpretationReview, context_refs=[eid])
        review = rres.output  # type: ignore[assignment]
        emit("EXPERIMENT", f"critic → {rres.completion.model}: {review.verdict} ({len(review.problems)} problems)")
        if review.verdict != Verdict.ACCEPT:
            store.add_open_question(run_id, "critic", f"{eid}: interpretation {review.verdict}: " + "; ".join(p.description for p in review.problems)[:400])
            if review.decision_should_be and review.decision_should_be != decision:
                # two readers disagree on the decision: record neither as final
                overridden = (overridden + "; " if overridden else "") + f"critic says {review.decision_should_be}; recorded INCONCLUSIVE pending operator"
                decision = Decision.INCONCLUSIVE
    store.record_interpretation(eid, decision, interp.model_dump(mode="json"), review.model_dump(mode="json") if review else None)
    store.finish_run(run_id, "done", overridden)
    return InterpretOutcome(decision, interp, review, overridden, run_id)
