"""Context packs: what each role sees, built from store rows. Never the transcript.
Untrusted text enters a prompt only inside <evidence> blocks."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from roundtable.util import dumps

PROMPTS = Path(__file__).parent / "prompts"
MAX_EVIDENCE_CHARS = 20_000


def system_prompt(role_file: str, *, agent_mode: bool = False, domain: str | None = None) -> str:
    common = (PROMPTS / "_common.md").read_text().strip()
    if agent_mode:
        # the scratch-directory rule does not apply to an agent working inside the worktree
        common = "\n".join(l for l in common.splitlines() if "scratch directory" not in l)
    text = common + "\n\n" + (PROMPTS / f"{role_file}.md").read_text().strip()
    addendum = PROMPTS / f"{role_file}_{domain}.md" if domain else None
    if addendum and addendum.exists():
        text += "\n" + addendum.read_text().rstrip()
    return text


def evidence_block(id: str, kind: str, body: Any, *, trust: str = "agent-output") -> str:
    text = body if isinstance(body, str) else json.dumps(body, indent=1, sort_keys=True, default=str, ensure_ascii=False)
    truncated = ""
    if len(text) > MAX_EVIDENCE_CHARS:
        text = text[:MAX_EVIDENCE_CHARS]
        truncated = ' truncated="true"'
    return f'<evidence id="{id}" kind="{kind}" trust="{trust}"{truncated}>\n{text}\n</evidence>'


def objective_block(objective: str, requirements: list[str]) -> str:
    reqs = "\n".join(f"{i}. {r}" for i, r in enumerate(requirements, 1)) or "(none listed; derive them from the objective)"
    return f"OBJECTIVE:\n{objective.strip()}\n\nREQUIREMENTS:\n{reqs}"


def proposer_pack(objective: str, requirements: list[str], *, round_limit: int) -> str:
    return (objective_block(objective, requirements)
            + f"\n\nThe critique loop allows at most {round_limit} revision round(s). Get the criteria right the first time.")


def critic_pack(objective: str, requirements: list[str], proposal_id: str, proposal: dict[str, Any]) -> str:
    body = {k: v for k, v in proposal.items() if k != "responses_to_criticism"} | (
        {"responses_to_criticism": proposal["responses_to_criticism"]} if proposal.get("responses_to_criticism") else {})
    return (objective_block(objective, requirements)
            + f"\n\nThe proposal under review is {proposal_id}.\n\n" + evidence_block(proposal_id, "proposal", body))


def reviser_pack(objective: str, requirements: list[str], proposal_id: str, proposal: dict[str, Any],
                 critique_id: str, critique: dict[str, Any]) -> str:
    return (objective_block(objective, requirements)
            + f"\n\nYour proposal {proposal_id}:\n" + evidence_block(proposal_id, "proposal", proposal)
            + f"\n\nThe critique {critique_id} of it:\n" + evidence_block(critique_id, "critique", critique)
            + "\n\nProduce the revised proposal.")


def synthesizer_pack(objective: str, requirements: list[str], *, status: str, halt_reason: str | None,
                     proposals: list[tuple[str, dict[str, Any]]], critiques: list[tuple[str, str, dict[str, Any]]],
                     decisions: list[str], usage: dict[str, Any], extra_blocks: list[str] | None = None) -> str:
    parts = [objective_block(objective, requirements),
             f"\nRUN STATUS: {status}" + (f" (reason: {halt_reason})" if halt_reason else ""),
             "\nDECISIONS (orchestrator, in order):\n" + "\n".join(f"- {d}" for d in decisions)]
    for pid, body in proposals:
        parts.append(evidence_block(pid, "proposal", body))
    for cid, target, body in critiques:
        parts.append(evidence_block(cid, f"critique-of-{target}", body))
    parts.extend(extra_blocks or [])
    parts.append("USAGE: " + dumps(usage))
    return "\n\n".join(parts)


# ---- M2 packs


def _criteria_text(criteria: list[dict[str, Any]]) -> str:
    return "\n".join(f"- {c['id']}: {c['requirement']} — check: {c['check']}" for c in criteria) or "(none)"


def engineer_pack(objective: str, requirements: list[str], proposal_id: str, proposal: dict[str, Any],
                  criteria: list[dict[str, Any]], *, tree: str, test_command: str, branch: str,
                  agent_mode: bool, prior: dict[str, Any] | None = None) -> str:
    plan = {k: proposal.get(k) for k in ("claim", "approach", "assumptions", "risks")}
    parts = [objective_block(objective, requirements),
             f"\nACCEPTED PROPOSAL {proposal_id}:\n" + evidence_block(proposal_id, "proposal", plan),
             "\nLOCKED ACCEPTANCE CRITERIA (the definition of done):\n" + _criteria_text(criteria),
             f"\nTEST COMMAND (the orchestrator runs this after you finish): {test_command}",
             f"\nWORKSPACE ({'you are in it, on branch ' + branch if agent_mode else 'branch ' + branch + '; files are shown; return complete files'}):\n"
             + evidence_block("tree", "file-tree", tree, trust="filesystem")]
    if prior:
        parts.append("\nPREVIOUS ATTEMPT FAILED. Fix it.")
        if prior.get("test_output"):
            parts.append(evidence_block(prior["test_id"], "test-output", prior["test_output"], trust="test-runner"))
        if prior.get("diff"):
            parts.append(evidence_block(prior["impl_id"], "diff-of-previous-attempt", prior["diff"], trust="filesystem"))
        if prior.get("files"):
            for path, content in prior["files"].items():
                parts.append(evidence_block(path, "file", content, trust="filesystem"))
    return "\n".join(parts)


def validator_pack(objective: str, requirements: list[str], criteria: list[dict[str, Any]], *,
                   files: dict[str, str], test_result: dict[str, Any], diff: str) -> str:
    parts = [objective_block(objective, requirements),
             "\nLOCKED ACCEPTANCE CRITERIA:\n" + _criteria_text(criteria),
             "\nTEST RESULT (real output from the orchestrator):\n"
             + evidence_block("test", "test-result", test_result, trust="test-runner")]
    for path, content in files.items():
        parts.append(evidence_block(path, "file", content, trust="filesystem"))
    if diff and not files:
        parts.append(evidence_block("diff", "diff", diff, trust="filesystem"))
    return "\n".join(parts)


# ---- M5 packs


def interpreter_pack(prereg: dict[str, Any], prereg_hash: str, result: dict[str, Any]) -> str:
    return ("PRE-REGISTRATION (locked " + prereg_hash[:19] + "…; fixed before the result existed):\n"
            + evidence_block("prereg", "experiment-preregistration", prereg, trust="operator")
            + "\n\nRECORDED RESULT (from the orchestrator's execution, not from any model):\n"
            + evidence_block("result", "experiment-result", result, trust="test-runner"))


def experiment_critic_pack(prereg: dict[str, Any], prereg_hash: str, result: dict[str, Any], interpretation: dict[str, Any]) -> str:
    return (interpreter_pack(prereg, prereg_hash, result)
            + "\n\nINTERPRETATION under review:\n" + evidence_block("interp", "interpretation", interpretation))
