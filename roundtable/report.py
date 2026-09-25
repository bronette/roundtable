"""The human-facing report. Facts come from the store; the synthesizer's narrative is added
when it ran. If it did not (budget, provider down), the fact section still stands alone."""

from __future__ import annotations

import json
from typing import Any

from roundtable.schemas import Synthesis
from roundtable.store import Store


def build_report(store: Store, run_id: str, synthesis: Synthesis | None, *, budget_summary: dict[str, Any] | None = None,
                 workspace: Any = None) -> str:
    run = store.get_run(run_id)
    proposals = store.proposals(run_id)
    critiques = store.critiques(run_id)
    decisions = store.decisions(run_id)
    questions = store.open_questions(run_id)
    usage = store.usage(run_id)
    project = store.db.execute("SELECT * FROM projects WHERE id=?", (run["project_id"],)).fetchone()

    out: list[str] = [f"# {project['name']} — run {run_id}", ""]
    out.append(f"**Status:** {run['status']}" + (f" — {run['halt_reason']}" if run["halt_reason"] else ""))
    if synthesis:
        out.append(f"**Overall verdict:** {synthesis.overall_verdict} (confidence {synthesis.confidence:.2f})")
    else:
        out.append("**Overall verdict:** not synthesized (the synthesizer did not run; see status)")
    out += ["", "## Objective", "", project["objective"].strip(), ""]
    reqs = json.loads(project["requirements_json"])
    if reqs:
        out += ["Requirements:", *[f"{i}. {r}" for i, r in enumerate(reqs, 1)], ""]

    if synthesis:
        out += ["## What was built", "", synthesis.what_was_built, "", "## Test results", "", synthesis.test_summary, ""]
        out.append("## Disagreements")
        out.append("")
        if synthesis.disagreements:
            for i, d in enumerate(synthesis.disagreements, 1):
                out.append(f"{i}. **{d.topic}** — {'resolved' if d.resolved else 'UNRESOLVED'}"
                           + (f": {d.resolution}" if d.resolution else ""))
                for p in d.positions:
                    out.append(f"   - {p.role}: {p.position}")
        else:
            out.append("None recorded.")
        out += ["", "## Remaining risks", ""]
        out += [f"- {r}" for r in synthesis.remaining_risks] or ["- none listed"]
        out += ["", "## Recommended next experiment", "", synthesis.recommended_next_experiment or "None; objective complete.", ""]

    out += ["## Proposal history", ""]
    if not proposals:
        out.append("No proposal was produced.")
    for p in proposals:
        body = json.loads(p["body_json"])
        crits = [c for c in critiques if c["target_id"] == p["id"]]
        line = f"- **{p['id']}** (round {p['round']}, {p['status']}, confidence {body.get('confidence', 0):.2f}): {body.get('claim', '')}"
        out.append(line)
        for c in crits:
            cb = json.loads(c["body_json"])
            probs = cb.get("problems", [])
            sev = {s: sum(1 for x in probs if x["severity"] == s) for s in ("blocker", "major", "minor")}
            out.append(f"  - {c['id']} → **{c['verdict']}** ({sev['blocker']} blocker, {sev['major']} major, {sev['minor']} minor)")
            for x in probs:
                out.append(f"    - [{x['severity']}] {x['description']}" + (f" ({x['location']})" if x.get("location") else ""))
        for r in body.get("responses_to_criticism", []):
            if r["action"] != "fixed":
                out.append(f"  - proposer **{r['action']}**: \"{r['problem']}\" — {r['reason']}")
    if run["acceptance_json"]:
        out += ["", "## Acceptance criteria (locked " + run["acceptance_locked_at"] + ")", ""]
        for ac in json.loads(run["acceptance_json"]):
            out.append(f"- {ac['id']}: {ac['requirement']} — check: {ac['check']}")

    impls = store.implementations(run_id)
    tests = store.test_runs(run_id)
    reviews = [c for c in critiques if c["target_kind"] == "implementation"]
    if impls:
        out += ["", "## Implementation", ""]
        if workspace is not None:
            out.append(f"Branch `{workspace.branch}` in `{workspace.path}` ({workspace.mode}"
                       + (f" of `{workspace.source_repo}`" if workspace.source_repo else "") + "). "
                       "Nothing has been merged; review the branch and merge it yourself.")
            out.append("")
        for r in impls:
            body = json.loads(r["body_json"])
            out.append(f"- **{r['id']}** (fix round {r['fix_round']}, commit `{(body.get('commit') or '')[:10]}`): {body.get('summary', '')}")
            if body.get("changed"):
                out.append("  - files: " + ", ".join(f"`{c}`" for c in body["changed"]))
            for g in body.get("known_gaps", []):
                out.append(f"  - known gap: {g}")
            for a in json.loads(r["artifact_ids_json"]):
                out.append(f"  - diff: `{a}` under artifacts/")
        out += ["", "## Test runs", "", "| id | command | exit | passed | failed | errors | time |", "|---|---|---|---|---|---|---|"]
        for t in tests:
            out.append(f"| {t['id']} | `{t['command']}` | {t['exit_code']}{' (timeout)' if t['timed_out'] else ''} | "
                       f"{t['passed']} | {t['failed']} | {t['errors']} | {t['duration_s']}s |")
    if reviews:
        out += ["", "## Validator review", ""]
        for c in reviews:
            cb = json.loads(c["body_json"])
            out.append(f"**{c['id']}** on {c['target_id']} → **{c['verdict']}** (confidence {cb.get('confidence', 0):.2f})")
            out.append("")
            out += ["| criterion | satisfied | evidence |", "|---|---|---|"]
            for ch in cb.get("checks", []):
                out.append(f"| {ch['criterion_id']} | {'yes' if ch['satisfied'] else 'NO'} | {ch['evidence'].replace('|', '/')} |")
            for x in cb.get("problems", []):
                out.append(f"- [{x['severity']}] {x['description']}" + (f" ({x['location']})" if x.get("location") else ""))
    if questions:
        out += ["", "## Open questions", ""]
        out += [f"- ({q['raised_by_role']}) {q['question']}" for q in questions]

    out += ["", "## Decisions", ""]
    out += [f"- {d['from_stage']} → {d['to_stage']}: {d['reason']}" for d in decisions]

    t = usage["total"]
    out += ["", "## Usage", "", "| provider | calls | input | output | cost |", "|---|---|---|---|---|"]
    for name, u in usage["per_provider"].items():
        cost = f"${u['cost_usd']:.4f}" if u["cost_usd"] is not None else "n/a"
        out.append(f"| {name} | {u['calls']} | {u['input_tokens']} | {u['output_tokens']} | {cost} |")
    cost_t = f"${t['cost_usd']:.4f}" if t["cost_known"] else "partly unknown"
    out.append(f"| **total** | {t['calls']} | {t['input_tokens']} | {t['output_tokens']} | {cost_t} |")
    if budget_summary:
        out.append("")
        out.append("Budget: " + ", ".join(f"{k} {v}" for k, v in budget_summary.items()))
    out += ["", f"Audit: `roundtable calls {run_id} --full` shows every prompt, response, and token count.", ""]
    return "\n".join(out)
