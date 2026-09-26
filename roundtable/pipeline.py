"""The orchestration state machine. One handler per stage; each builds a context pack from the
store, calls one agent, writes rows, and returns the next stage. Budget is checked before
every call. Every transition is recorded in `decisions`."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Callable

import json

from roundtable import actions, context
from roundtable.agents import Agent
from roundtable.budget import Budget, BudgetExceeded
from roundtable.config import Config
from roundtable.providers.base import ProviderError, SchemaError, UsageLimitError
from roundtable.report import build_report
from roundtable.schemas import Critique, Implementation, Proposal, Review, Scope, Synthesis, TradingCritique, Verdict
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
    ws: actions.Workspace | None = None
    impl_id: str | None = None
    test_id: str | None = None
    last_test: dict | None = None
    last_diff: str = ""
    changed_paths: list[str] = field(default_factory=list)
    review_id: str | None = None

    @property
    def run_dir(self) -> Path:
        return self.workspace.parent


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
        return self._loop(st, Stage.INIT)

    def resume(self, run_id: str, *, from_stage: "Stage | None" = None) -> RunState:
        """Restart a halted run at the stage that failed. Earlier calls are not repeated or re-billed;
        budget counters continue from the run's recorded usage, so raise the caps if the halt was a budget."""
        run = self.store.get_run(run_id)
        if run is None:
            raise ValueError(f"no run {run_id}")
        if from_stage is None and (run["status"] == "done" or run["status"] in ("implemented", "implemented_with_objections")):
            raise ValueError(f"run {run_id} finished with status {run['status']}; nothing to resume (use --from to force a stage)")
        failed_stage = from_stage
        for d in reversed(self.store.decisions(run_id)) if failed_stage is None else []:
            if d["reason"].startswith("halted:"):
                failed_stage = Stage(d["from_stage"])
                break
        if failed_stage is None:
            failed_stage = Stage(run["stage"]) if run["stage"] in Stage.__members__ else Stage.SYNTHESIZE
        if failed_stage in (Stage.SYNTHESIZE, Stage.DONE, Stage.HALTED):
            failed_stage = Stage.SYNTHESIZE
        st = self._rebuild_state(run)
        self.store.reopen_run(run_id, failed_stage)
        self.store.record_decision(run_id, Stage.HALTED, failed_stage, "resumed by operator" + (f" from {failed_stage}" if from_stage else ""), [])
        if from_stage in (Stage.TEST, Stage.FIX):
            st.status = "running"          # tests will run again and set it; REVIEW keeps the rebuilt test status
        self.on_event(Stage.HALTED, f"resuming {run_id} at {failed_stage} (round {st.round}, fix {st.fix_round})")
        return self._loop(st, failed_stage)

    def _rebuild_state(self, run) -> RunState:
        run_id = run["id"]
        st = RunState(run_id=run_id, workspace=Path(run["workspace_path"]), round=run["round"], fix_round=run["fix_round"])
        if run["ws_json"]:
            w = json.loads(run["ws_json"])
            st.ws = actions.Workspace(Path(w["path"]), w["branch"], w["base_commit"],
                                      Path(w["source_repo"]) if w.get("source_repo") else None, w["mode"])
        elif run["workspace_path"] and (Path(run["workspace_path"]) / ".git").exists():
            # runs recorded before ws_json existed: recover what git knows
            ws = actions.Workspace(Path(run["workspace_path"]), f"roundtable/{run_id}", "", None, "greenfield")
            try:
                ws.base_commit = ws.git("rev-list", "--max-parents=0", "HEAD").strip().splitlines()[0]
                ws.branch = ws.git("rev-parse", "--abbrev-ref", "HEAD").strip()
            except actions.WorkspaceError:
                pass
            st.ws = ws
        props = self.store.proposals(run_id)
        if props:
            st.proposal_id = props[-1]["id"]
            crits = [c for c in self.store.critiques(run_id) if c["target_kind"] == "proposal" and c["target_id"] == st.proposal_id]
            if crits:
                st.critique_id = crits[-1]["id"]
        impls = self.store.implementations(run_id)
        if impls:
            st.impl_id = impls[-1]["id"]
            if st.ws is not None:
                st.last_diff, st.changed_paths = actions.cumulative_changes(st.ws)
            else:
                body = json.loads(impls[-1]["body_json"])
                st.changed_paths = body.get("changed", [])
        tests = self.store.test_runs(run_id)
        if tests:
            t = tests[-1]
            st.test_id = t["id"]
            st.last_test = {"command": t["command"], "exit_code": t["exit_code"], "passed": t["passed"], "failed": t["failed"],
                            "errors": t["errors"], "duration_s": t["duration_s"], "timed_out": bool(t["timed_out"]),
                            "stdout_tail": self.store.artifact_text(run_id, t["output_artifact_id"], st.run_dir) if t["output_artifact_id"] else ""}
            if t["exit_code"] == 0:
                st.status = "implemented"
        return st

    def _loop(self, st: RunState, stage: Stage) -> RunState:
        run_id = st.run_id
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

    def _call(self, st: RunState, role: str, stage: Stage, prompt: str, schema, refs: list[str],
              *, prompt_file: str | None = None, workspace: str | None = None):
        agent = self.agents[role]
        readonly = False
        if workspace is None and agent.cfg.mode == "read" and st.ws is not None:
            workspace, readonly = str(st.ws.path), True     # read mode: inspect the worktree, change nothing
        self.budget.check(self.store, st.run_id, provider=agent.provider.name)
        self.on_event(stage, f"{role} ← {agent.provider.name}" + (f"/{agent.cfg.model}" if agent.cfg.model else "")
                             + (" [read mode in worktree]" if readonly else (" [agent mode in worktree]" if workspace else "")))
        if readonly:
            prompt = (f"You are inside a read-only checkout of the project (branch {st.ws.branch}). Read whatever files you need "
                      f"to ground your answer; cite paths. You cannot change anything.\n\nFILE TREE:\n{actions.file_tree(st.ws)}\n\n" + prompt)
        if self.cfg.project.context_files and role != "engineer":
            prompt += "\n\nCONTEXT DOCUMENTS supplied by the operator:\n" + "\n".join(
                context.evidence_block(name, "document", text, trust="operator") for name, text in self._context_docs())
        res = agent.call(store=self.store, run_id=st.run_id, stage=stage,
                         system=context.system_prompt(prompt_file or role, agent_mode=workspace is not None,
                                                      domain=self.cfg.project.domain),
                         prompt=prompt, schema=schema, context_refs=refs, workspace=workspace, readonly=readonly)
        c = res.completion
        self.on_event(stage, f"{role} → {c.model}  {c.latency_ms} ms  in={c.usage.input_tokens} out={c.usage.output_tokens}"
                             + (" (repaired)" if res.attempts > 1 else ""))
        return res

    # ---- handlers

    def h_init(self, st: RunState) -> Transition:
        repo = str(self.cfg.resolve_path(self.cfg.project.repo)) if self.cfg.project.repo else None
        try:
            st.ws = actions.prepare_workspace(st.run_dir, repo, st.run_id, commit=self.cfg.project.base_ref)
        except actions.WorkspaceError as e:
            raise ProviderError(f"workspace: {e}") from e
        self.store.set_ws(st.run_id, {"path": str(st.ws.path), "branch": st.ws.branch, "base_commit": st.ws.base_commit,
                                      "source_repo": str(st.ws.source_repo) if st.ws.source_repo else None, "mode": st.ws.mode})
        return Stage.PROPOSE, f"workspace ready ({st.ws.mode}, branch {st.ws.branch})", []

    # ---- engineering stages

    def _context_docs(self) -> list[tuple[str, str]]:
        out = []
        for f in self.cfg.project.context_files:
            path = self.cfg.resolve_path(f)
            try:
                out.append((path.name, path.read_text(errors="replace")))
            except OSError as e:
                out.append((path.name, f"(unreadable: {e})"))
        return out

    def _engineer_mode(self) -> bool:
        """True = agent mode (CLI runs inside the worktree)."""
        a = self.cfg.agents["engineer"]
        if a.mode == "read":
            raise ProviderError("engineer role cannot be in read mode")
        if a.mode:
            return a.mode == "agent"
        return self.cfg.providers[a.provider].type == "cli"

    def _criteria(self, st: RunState) -> list[dict]:
        run = self.store.get_run(st.run_id)
        return json.loads(run["acceptance_json"] or "[]")

    def _engineer(self, st: RunState, stage: Stage, prior: dict | None) -> Transition:
        p, ws = self.cfg.project, st.ws
        assert ws and st.proposal_id
        perm = self.cfg.permissions.get("engineer")
        if perm is not None and perm.workspace != "write":
            raise ProviderError("engineer role lacks workspace: write permission")
        agent_mode = self._engineer_mode()
        criteria = self._criteria(st)
        prompt = context.engineer_pack(p.objective, p.requirements, st.proposal_id, self.store.proposal(st.run_id, st.proposal_id),
                                       criteria, tree=actions.file_tree(ws), test_command=p.test_command, branch=ws.branch,
                                       agent_mode=agent_mode, prior=prior)
        a = self.cfg.agents["engineer"]
        try:
            res = self._call(st, "engineer", stage, prompt, Implementation, [st.proposal_id],
                             prompt_file="engineer" if agent_mode else "engineer_answer",
                             workspace=str(ws.path) if agent_mode else None)
            impl: Implementation = res.output  # type: ignore[assignment]
            call_id = res.call_id
        except ProviderError as e:
            # An agent-mode engineer that timed out (or crashed) after editing files is partial work, not
            # nothing. Keep what is on disk, say so, and let the tests judge it.
            if not (agent_mode and ws.git("status", "--porcelain", check=False).strip()):
                raise
            self.on_event(stage, f"engineer failed mid-work ({str(e)[:80]}); committing partial changes for testing")
            impl = Implementation(summary=f"PARTIAL: engineer session ended before reporting ({type(e).__name__}: {str(e)[:160]}). "
                                          "Changes on disk were committed as-is; the test stage decides.",
                                  known_gaps=["engineer did not finish; summary and gaps unknown"])
            call_id = self.store.calls(st.run_id)[-1]["id"]
        if not agent_mode:
            try:
                actions.write_files(ws, impl.files)
            except actions.WorkspaceError as e:
                raise ProviderError(f"engineer wrote outside workspace: {e}") from e
        commit, diff, changed = actions.commit_changes(ws, f"roundtable {st.run_id}: {stage.lower()} round {st.fix_round}")
        # reviewers and the report see the whole run's change, not just this round's
        st.last_diff, st.changed_paths = actions.cumulative_changes(ws)
        art = self.store.add_artifact(st.run_id, call_id=call_id, kind="diff", name=f"{stage.lower()}{st.fix_round}.diff",
                                      content=diff or "(no changes)", artifacts_dir=st.run_dir / "artifacts")
        st.impl_id = self.store.add_implementation(st.run_id, call_id=call_id, proposal_id=st.proposal_id, fix_round=st.fix_round,
                                                   artifact_ids=[art], body=impl.model_dump(mode="json") | {"commit": commit, "changed": changed})
        self.on_event(stage, f"{st.impl_id}: {len(changed)} file(s) changed, commit {commit[:10]} on {ws.branch}"
                             + (f"; known gaps: {len(impl.known_gaps)}" if impl.known_gaps else ""))
        if not changed:
            self.on_event(stage, "engineer changed nothing")
        return Stage.TEST, f"{st.impl_id} committed ({len(changed)} files)", [st.impl_id, art]

    def h_implement(self, st: RunState) -> Transition:
        if self.cfg.autonomy <= 1:
            st.status, st.halt_reason = "plan_accepted", "autonomy level 1: proposals only"
            return Stage.SYNTHESIZE, st.halt_reason, [st.proposal_id or ""]
        return self._engineer(st, Stage.IMPLEMENT, None)

    def h_test(self, st: RunState) -> Transition:
        p, ws = self.cfg.project, st.ws
        assert ws and st.impl_id
        python = str(self.cfg.resolve_path(p.python)) if p.python else None
        result = actions.run_tests(ws, p.test_command, python=python, timeout_s=p.test_timeout_s, extra_env=p.env)
        art = self.store.add_artifact(st.run_id, call_id=None, kind="test_output", name=f"test{st.fix_round}.txt",
                                      content=result.stdout_tail, artifacts_dir=st.run_dir / "artifacts")
        st.test_id = self.store.add_test_run(st.run_id, implementation_id=st.impl_id, result=result.model_dump(), output_artifact_id=art)
        st.last_test = result.model_dump()
        self.on_event(Stage.TEST, f"{st.test_id}: `{result.command}` exit={result.exit_code} passed={result.passed} "
                                  f"failed={result.failed} errors={result.errors} {result.duration_s}s"
                                  + (" TIMED OUT" if result.timed_out else ""))
        if result.exit_code == 0:
            st.status = "implemented"
            return Stage.REVIEW, f"{st.test_id} passed", [st.test_id]
        if st.fix_round < self.cfg.budget.max_fix_rounds:
            st.fix_round += 1
            return Stage.FIX, f"{st.test_id} failed; fix round {st.fix_round}/{self.cfg.budget.max_fix_rounds}", [st.test_id]
        st.status = "tests_failing"
        return Stage.REVIEW, f"{st.test_id} failed; fix rounds exhausted", [st.test_id]

    def h_fix(self, st: RunState) -> Transition:
        assert st.ws and st.last_test
        prior = {"test_id": st.test_id, "test_output": st.last_test.get("stdout_tail", ""), "impl_id": st.impl_id}
        if self._engineer_mode():
            prior["diff"] = st.last_diff
        else:
            prior["files"] = actions.read_files(st.ws, st.changed_paths)
        return self._engineer(st, Stage.FIX, prior)

    def h_review(self, st: RunState) -> Transition:
        p, ws = self.cfg.project, st.ws
        assert ws and st.last_test and st.impl_id
        files = actions.read_files(ws, st.changed_paths)
        prompt = context.validator_pack(p.objective, p.requirements, self._criteria(st), files=files,
                                        test_result=st.last_test, diff=st.last_diff)
        res = self._call(st, "validator", Stage.REVIEW, prompt, Review, [st.impl_id, st.test_id or ""])
        rev: Review = res.output  # type: ignore[assignment]
        st.review_id = self.store.add_critique(st.run_id, call_id=res.call_id, target_kind="implementation", target_id=st.impl_id,
                                               verdict=rev.verdict, body=rev.model_dump(mode="json"))
        ok = sum(1 for c in rev.checks if c.satisfied)
        self.on_event(Stage.REVIEW, f"{st.review_id} on {st.impl_id} → {rev.verdict}  ({ok}/{len(rev.checks)} criteria satisfied, "
                                    f"{len(rev.problems)} problems)")
        if st.status == "implemented" and rev.verdict != Verdict.ACCEPT:
            st.status = "implemented_with_objections"
        return Stage.SYNTHESIZE, f"{st.review_id} {rev.verdict}", [st.review_id]

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
        existing = [c for c in self.store.critiques(st.run_id) if c["target_kind"] == "proposal" and c["target_id"] == pid]
        if existing:   # resumed after the critique was already recorded: apply its verdict instead of paying again
            crit = Critique.model_validate(json.loads(existing[-1]["body_json"]))   # base fields suffice for the verdict
            st.critique_id = existing[-1]["id"]
            return self._apply_verdict(st, pid, st.critique_id, crit)
        prompt = context.critic_pack(p.objective, p.requirements, pid, body)
        schema = TradingCritique if p.domain == "trading" else Critique
        res = self._call(st, "critic", Stage.CRITIQUE, prompt, schema, [pid])
        crit: Critique = res.output  # type: ignore[assignment]
        cid = self.store.add_critique(st.run_id, call_id=res.call_id, target_kind="proposal", target_id=pid,
                                      verdict=crit.verdict, body=crit.model_dump(mode="json"))
        st.critique_id = cid
        self.store.set_proposal_status(st.run_id, pid, "criticized")
        n_block = sum(1 for x in crit.problems if x.severity == "blocker")
        self.on_event(Stage.CRITIQUE, f"{cid} on {pid} → {crit.verdict}  ({len(crit.problems)} problems, {n_block} blockers)")
        return self._apply_verdict(st, pid, cid, crit)

    def _apply_verdict(self, st: RunState, pid: str, cid: str, crit: Critique) -> Transition:
        refs = [pid, cid]
        body = self.store.proposal(st.run_id, pid)
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
            blockers = [x for x in crit.problems if x.severity == "blocker"]
            if blockers:
                # a blocker against the plan at the round limit means the definition of done is wrong;
                # locking it would make the engineer build to a known-bad target. Stop and ask.
                for x in blockers:
                    self.store.add_open_question(st.run_id, "critic", f"blocker at round limit: {x.description}")
                st.status, st.halt_reason = "needs_input", f"{cid} REVISE with {len(blockers)} blocker(s) at max_rounds; not locking criteria"
                return Stage.SYNTHESIZE, st.halt_reason, refs
            st.unresolved = True
            self._lock(st, body)
            return Stage.IMPLEMENT, f"{cid} REVISE at max_rounds (no blockers); implementing with open criticisms", refs
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

    def h_synthesize(self, st: RunState) -> Transition:
        p = self.cfg.project
        import json as _json
        proposals = [(r["id"], _json.loads(r["body_json"])) for r in self.store.proposals(st.run_id)]
        critiques = [(r["id"], r["target_id"], _json.loads(r["body_json"])) for r in self.store.critiques(st.run_id)]
        decisions = [f"{d['from_stage']} → {d['to_stage']}: {d['reason']}" for d in self.store.decisions(st.run_id)]
        status = st.status if st.status != "running" else "done"
        extra = []
        for r in self.store.implementations(st.run_id):
            body = _json.loads(r["body_json"])
            extra.append(context.evidence_block(r["id"], "implementation", {k: body.get(k) for k in ("summary", "assumptions", "known_gaps", "changed", "commit")}))
        for r in self.store.test_runs(st.run_id):
            extra.append(context.evidence_block(r["id"], "test-run", {k: r[k] for k in ("command", "exit_code", "passed", "failed", "errors", "timed_out")}, trust="test-runner"))
        if st.ws:
            extra.append(f"WORKSPACE: branch {st.ws.branch} ({st.ws.mode}); changed files: {', '.join(st.changed_paths) or 'none'}")
        if p.context_files:
            extra.append("CONTEXT DOCUMENTS supplied by the operator:\n" + "\n".join(
                context.evidence_block(name, "document", text, trust="operator") for name, text in self._context_docs()))
        prompt = context.synthesizer_pack(p.objective, p.requirements, status=status, halt_reason=st.halt_reason,
                                          proposals=proposals, critiques=critiques, decisions=decisions,
                                          usage=self.budget.summary(self.store, st.run_id), extra_blocks=extra)
        synth_error: str | None = None
        synth = self.agents.get("synthesizer")
        if synth is None:
            synth_error = "no synthesizer configured"
        elif not self.store.calls(st.run_id):
            synth_error = "halted before any model call; nothing to synthesize"
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
        report = build_report(self.store, st.run_id, st.synthesis, budget_summary=self.budget.summary(self.store, st.run_id),
                              workspace=st.ws)
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
        Stage.TEST: h_test,
        Stage.FIX: h_fix,
        Stage.REVIEW: h_review,
        Stage.SYNTHESIZE: h_synthesize,
    }
