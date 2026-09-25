"""The orchestration state machine. One handler per stage; each builds a context pack from the
store, calls one agent, writes rows, and returns the next stage. Budget is checked before
every call. Every transition is recorded in `decisions`."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Callable

from roundtable import context
from roundtable.agents import Agent
from roundtable.budget import Budget, BudgetExceeded
from roundtable.config import Config
from roundtable.providers.base import ProviderError, SchemaError, UsageLimitError
from roundtable.report import build_report
from roundtable.schemas import Critique, Proposal, Scope, Synthesis, Verdict
from roundtable.store import Store


class Stage(StrEnum):
    INIT = "INIT"
    PROPOSE = "PROPOSE"
    CRITIQUE = "CRITIQUE"
    REVISE = "REVISE"
    IMPLEMENT = "IMPLEMENT"
    TEST = "TEST"
    FIX = "FIX"
    REVIEW = "REVIEW"
    SYNTHESIZE = "SYNTHESIZE"
    DONE = "DONE"
    HALTED = "HALTED"


Transition = tuple[Stage, str, list[str]]  # next stage, reason, row ids involved
EventFn = Callable[[str, str], None]


@dataclass
class RunState:
    run_id: str
    workspace: Path
    round: int = 0
    fix_round: int = 0
    proposal_id: str | None = None      # the proposal currently under discussion
    critique_id: str | None = None      # the latest critique of it
    unresolved: bool = False            # went to IMPLEMENT with open criticisms
    status: str = "running"             # final status, set on the way to SYNTHESIZE
    halt_reason: str | None = None
    synthesis: Synthesis | None = None
    report_path: Path | None = None
    notes: list[str] = field(default_factory=list)


class Pipeline:
    def __init__(self, cfg: Config, store: Store, agents: dict[str, Agent], *, on_event: EventFn | None = None):
        self.cfg, self.store, self.agents = cfg, store, agents
        self.on_event = on_event or (lambda stage, text: None)
        self.budget = Budget(cfg.budget)

    # ---- entry

    def run(self, *, runs_dir: Path) -> RunState:
        p = self.cfg.project
        project_id = self.store.create_project(name=p.name, objective=p.objective, requirements=p.requirements,
                                               config=self.cfg.model_dump(mode="json"))
        run_id = self.store.create_run(project_id=project_id, workspace_path="", stage=Stage.INIT)
        run_dir = runs_dir / p.name / run_id
        st = RunState(run_id=run_id, workspace=run_dir / "workspace")
        self.store.db.execute("UPDATE runs SET workspace_path=? WHERE id=?", (str(st.workspace), run_id))
        stage = Stage.INIT
        while stage != Stage.DONE:
            handler = self.HANDLERS[stage]
            try:
                nxt, reason, refs = handler(self, st)
            except BudgetExceeded as e:
                nxt, reason, refs = self._halt(st, "halted_budget", str(e))
            except UsageLimitError as e:
                nxt, reason, refs = self._halt(st, "halted_usage_limit", str(e))
            except (ProviderError, SchemaError) as e:
                nxt, reason, refs = self._halt(st, "halted_error", f"{type(e).__name__}: {e}")
            self.store.record_decision(run_id, stage, nxt, reason, refs)
            self.store.set_stage(run_id, nxt, round=st.round, fix_round=st.fix_round)
            self.on_event(stage, f"→ {nxt}: {reason}")
            stage = nxt
        return st

    def _halt(self, st: RunState, status: str, reason: str) -> Transition:
        st.status, st.halt_reason = status, reason
        self.on_event(Stage.HALTED, reason)
        return Stage.SYNTHESIZE, f"halted: {reason}", []

    def _call(self, st: RunState, role: str, stage: Stage, prompt: str, schema, refs: list[str]):
        agent = self.agents[role]
        self.budget.check(self.store, st.run_id, provider=agent.provider.name)
        self.on_event(stage, f"{role} ← {agent.provider.name}" + (f"/{agent.cfg.model}" if agent.cfg.model else ""))
        res = agent.call(store=self.store, run_id=st.run_id, stage=stage, system=context.system_prompt(role),
                         prompt=prompt, schema=schema, context_refs=refs)
        c = res.completion
        self.on_event(stage, f"{role} → {c.model}  {c.latency_ms} ms  in={c.usage.input_tokens} out={c.usage.output_tokens}"
                             + (" (repaired)" if res.attempts > 1 else ""))
        return res

    # ---- handlers

    def h_init(self, st: RunState) -> Transition:
        st.workspace.mkdir(parents=True, exist_ok=True)
        return Stage.PROPOSE, "workspace ready", []

    def h_propose(self, st: RunState) -> Transition:
        p = self.cfg.project
        prompt = context.proposer_pack(p.objective, p.requirements, round_limit=self.cfg.budget.max_rounds)
        res = self._call(st, "proposer", Stage.PROPOSE, prompt, Proposal, [])
        prop: Proposal = res.output  # type: ignore[assignment]
        pid = self.store.add_proposal(st.run_id, call_id=res.call_id, revision_of=None, round=0, body=prop.model_dump(mode="json"))
        st.proposal_id = pid
        self.on_event(Stage.PROPOSE, f"{pid}: {prop.claim}  (confidence {prop.confidence:.2f})")
        if prop.scope == Scope.NEEDS_DECOMPOSITION:
            self.store.set_proposal_status(st.run_id, pid, "needs_decomposition")
            for i, task in enumerate(prop.suggested_split, 1):
                self.store.add_open_question(st.run_id, "proposer", f"Sub-task {i}: {task}")
            st.status, st.halt_reason = "needs_decomposition", f"{pid}: objective too large for one task; {len(prop.suggested_split)} sub-tasks suggested"
            return Stage.SYNTHESIZE, st.halt_reason, [pid]
        return Stage.CRITIQUE, f"{pid} proposed", [pid]

    def h_critique(self, st: RunState) -> Transition:
        p = self.cfg.project
        pid = st.proposal_id
        assert pid
        body = self.store.proposal(st.run_id, pid)
        prompt = context.critic_pack(p.objective, p.requirements, pid, body)
        res = self._call(st, "critic", Stage.CRITIQUE, prompt, Critique, [pid])
        crit: Critique = res.output  # type: ignore[assignment]
        cid = self.store.add_critique(st.run_id, call_id=res.call_id, target_kind="proposal", target_id=pid,
                                      verdict=crit.verdict, body=crit.model_dump(mode="json"))
        st.critique_id = cid
        self.store.set_proposal_status(st.run_id, pid, "criticized")
        n_block = sum(1 for x in crit.problems if x.severity == "blocker")
        self.on_event(Stage.CRITIQUE, f"{cid} on {pid} → {crit.verdict}  ({len(crit.problems)} problems, {n_block} blockers)")
        refs = [pid, cid]
        if self.cfg.autonomy == 0:
            st.status, st.halt_reason = "advised", "autonomy level 0: advise only"
            return Stage.SYNTHESIZE, f"{cid} {crit.verdict}; stopping at autonomy 0", refs

        v = crit.verdict
        if v == Verdict.ACCEPT:
            self._lock(st, body)
            return Stage.IMPLEMENT, f"{cid} ACCEPT; acceptance criteria locked", refs
        if v == Verdict.REVISE:
            if st.round < self.cfg.budget.max_rounds:
                st.round += 1
                return Stage.REVISE, f"{cid} REVISE; round {st.round}/{self.cfg.budget.max_rounds}", refs
            st.unresolved = True
            self._lock(st, body)
            return Stage.IMPLEMENT, f"{cid} REVISE at max_rounds; implementing with open criticisms", refs
        if v == Verdict.REJECT:
            self.store.set_proposal_status(st.run_id, pid, "rejected")
            st.status, st.halt_reason = "rejected", f"{cid} rejected {pid}"
            return Stage.SYNTHESIZE, st.halt_reason, refs
        # NEEDS_EVIDENCE / NEEDS_EXPERIMENT: no researcher yet, record and stop
        for t in crit.tests_required or [x.description for x in crit.problems]:
            self.store.add_open_question(st.run_id, "critic", f"{v}: {t}")
        st.status, st.halt_reason = "needs_input", f"{cid} {v}"
        return Stage.SYNTHESIZE, st.halt_reason, refs

    def _lock(self, st: RunState, proposal_body: dict) -> None:
        h = self.store.lock_acceptance(st.run_id, proposal_body.get("acceptance_criteria", []))
        self.store.set_proposal_status(st.run_id, st.proposal_id or "", "accepted")
        self.on_event(Stage.CRITIQUE, f"acceptance criteria locked ({h[:19]}…)")

    def h_revise(self, st: RunState) -> Transition:
        p = self.cfg.project
        pid, cid = st.proposal_id, st.critique_id
        assert pid and cid
        prop = self.store.proposal(st.run_id, pid)
        crit = next(dict(c) for c in self.store.critiques(st.run_id) if c["id"] == cid)
        import json as _json
        prompt = context.reviser_pack(p.objective, p.requirements, pid, prop, cid, _json.loads(crit["body_json"]))
        res = self._call(st, "reviser", Stage.REVISE, prompt, Proposal, [pid, cid])
        rev: Proposal = res.output  # type: ignore[assignment]
        new_pid = self.store.add_proposal(st.run_id, call_id=res.call_id, revision_of=pid, round=st.round, body=rev.model_dump(mode="json"))
        st.proposal_id = new_pid
        acts = {a: sum(1 for r in rev.responses_to_criticism if r.action == a) for a in ("fixed", "rejected", "deferred")}
        self.on_event(Stage.REVISE, f"{new_pid} revises {pid}: {acts['fixed']} fixed, {acts['rejected']} rejected, {acts['deferred']} deferred")
        if rev.scope == Scope.NEEDS_DECOMPOSITION:
            st.status, st.halt_reason = "needs_decomposition", f"{new_pid}: reviser concluded the objective needs decomposition"
            return Stage.SYNTHESIZE, st.halt_reason, [new_pid]
        return Stage.CRITIQUE, f"{new_pid} revised", [pid, cid, new_pid]

    def h_implement(self, st: RunState) -> Transition:
        # M2 adds the engineer. Until then the accepted plan is the deliverable.
        if self.cfg.autonomy <= 1:
            st.status, st.halt_reason = "plan_accepted", "autonomy level 1: proposals only"
        else:
            st.status, st.halt_reason = "plan_accepted", "implementation stage not available yet (M2)"
        return Stage.SYNTHESIZE, st.halt_reason, [st.proposal_id or ""]

    def h_synthesize(self, st: RunState) -> Transition:
        p = self.cfg.project
        import json as _json
        proposals = [(r["id"], _json.loads(r["body_json"])) for r in self.store.proposals(st.run_id)]
        critiques = [(r["id"], r["target_id"], _json.loads(r["body_json"])) for r in self.store.critiques(st.run_id)]
        decisions = [f"{d['from_stage']} → {d['to_stage']}: {d['reason']}" for d in self.store.decisions(st.run_id)]
        status = st.status if st.status != "running" else "done"
        prompt = context.synthesizer_pack(p.objective, p.requirements, status=status, halt_reason=st.halt_reason,
                                          proposals=proposals, critiques=critiques, decisions=decisions,
                                          usage=self.budget.summary(self.store, st.run_id))
        synth_error: str | None = None
        synth = self.agents.get("synthesizer")
        if synth is None:
            synth_error = "no synthesizer configured"
        elif st.status == "halted_usage_limit" and synth.provider.name == self._last_failed_provider(st):
            synth_error = f"provider {synth.provider.name} is at its usage limit"
        if synth_error is None:
            try:
                # The synthesizer is allowed exactly one call even if the budget tripped: a halted run
                # without a report is worse than one call over budget.
                res = synth.call(
                    store=self.store, run_id=st.run_id, stage=Stage.SYNTHESIZE, system=context.system_prompt("synthesizer"),
                    prompt=prompt, schema=Synthesis, context_refs=[pid for pid, _ in proposals] + [cid for cid, _, _ in critiques])
                st.synthesis = res.output  # type: ignore[assignment]
                c = res.completion
                self.on_event(Stage.SYNTHESIZE, f"synthesizer → {c.model}  {c.latency_ms} ms  verdict {st.synthesis.overall_verdict}")
            except (ProviderError, SchemaError) as e:  # fall back to the fact-only report
                synth_error = f"{type(e).__name__}: {e}"
                self.on_event(Stage.SYNTHESIZE, f"synthesizer failed ({synth_error[:120]}); writing fact-only report")
        final_status = status
        self.store.finish_run(st.run_id, final_status, st.halt_reason)
        report = build_report(self.store, st.run_id, st.synthesis, budget_summary=self.budget.summary(self.store, st.run_id))
        if synth_error:
            report += f"\n\n> Synthesizer unavailable: {synth_error}\n"
        st.report_path = st.workspace.parent / "report.md"
        st.report_path.parent.mkdir(parents=True, exist_ok=True)
        st.report_path.write_text(report)
        return Stage.DONE, f"report written: {st.report_path}", []

    def _last_failed_provider(self, st: RunState) -> str | None:
        rows = self.store.calls(st.run_id)
        for r in reversed(rows):
            if not r["valid"]:
                return r["provider"]
        return None

    HANDLERS: dict[Stage, Callable[["Pipeline", RunState], Transition]] = {
        Stage.INIT: h_init,
        Stage.PROPOSE: h_propose,
        Stage.CRITIQUE: h_critique,
        Stage.REVISE: h_revise,
        Stage.IMPLEMENT: h_implement,
        Stage.SYNTHESIZE: h_synthesize,
    }
