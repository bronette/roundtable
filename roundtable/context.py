"""Context packs: what each role sees, built from store rows. Never the transcript.
Untrusted text enters a prompt only inside <evidence> blocks."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from roundtable.util import dumps

PROMPTS = Path(__file__).parent / "prompts"
MAX_EVIDENCE_CHARS = 20_000


def system_prompt(role_file: str) -> str:
    return (PROMPTS / "_common.md").read_text().strip() + "\n\n" + (PROMPTS / f"{role_file}.md").read_text().strip()


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
