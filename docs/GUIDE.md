# User guide

Roundtable runs several LLMs as a team of specialists on one objective. A Python
orchestrator owns the workflow; the models never talk to each other. Each one receives a
purpose-built context, replies with schema-validated JSON, and the orchestrator decides what
happens next. Every call, verdict, file change, and test result is logged to SQLite.

## Install

```bash
git clone https://github.com/bronette/roundtable
cd roundtable
uv sync                       # Python 3.12+, creates .venv
uv run roundtable --help
```

Log in to the CLIs you want to use, once, in your own terminal:

```bash
claude login       # claude.ai subscription
codex login        # ChatGPT
grok login         # xAI
ollama serve       # local models, then: ollama pull qwen3:30b-a3b
```

No API keys are needed for those. API providers need the variable named in the config
(`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `XAI_API_KEY`, `GEMINI_API_KEY`).

## First run

```bash
uv run roundtable providers -c examples/project.yaml
```

prints each configured provider and whether it can be used right now. Then:

```bash
uv run roundtable run examples/project.yaml
```

The example objective is a small pure-Python function with tests. A full run at autonomy 2
makes six to nine model calls and takes ten to twenty minutes, most of it Grok thinking.
The console shows each stage as it happens; the report lands at
`runs/dd-demo/<run_id>/report.md`.

## What a run does

```
INIT        prepare the workspace (worktree, copy, or empty dir), all on a new git branch
PROPOSE     proposer: scope check, plan, assumptions, risks, acceptance criteria
CRITIQUE    critic: attacks the proposal; ACCEPT / REVISE / REJECT / NEEDS_EVIDENCE / NEEDS_EXPERIMENT
REVISE      proposer answers every problem (fixed / rejected / deferred) and reissues the plan
            ... CRITIQUE ↔ REVISE up to budget.max_rounds ...
            on ACCEPT (or REVISE at the round limit) the acceptance criteria are LOCKED
IMPLEMENT   engineer: writes code and tests in the workspace; the orchestrator commits the diff
TEST        orchestrator runs project.test_command; output is recorded verbatim
FIX         engineer sees the failing output and tries again (budget.max_fix_rounds times)
REVIEW      validator: checks every locked criterion against the files and the real test output
SYNTHESIZE  synthesizer: what was built, what the tests say, disagreements, risks, next step
DONE        report written
```

Anything can halt the run: a budget cap, a provider outage, a usage limit, invalid output
twice in a row. A halted run still gets a report; the synthesizer is allowed one call past the
budget for it, and if it cannot run either, the report is built from the database alone.

### What "solved" means

A run is only a success when four observable things are all true:

1. every locked acceptance criterion is marked satisfied by the validator **with a citation**
   (a file and line, a test name that appears in the output, or a quoted output line);
2. the test command was actually executed by the orchestrator and exited 0;
3. no blocker-severity problem is open from the critic or the validator;
4. the synthesizer's overall verdict is ACCEPT, with any disagreements it could not resolve
   listed rather than hidden.

A model saying the work is done is never evidence. The validator deliberately does not see the
proposer's reasoning or the critic's opinions; it judges the artifact and the test output.

### Who is in charge

The orchestrator, which is code. It picks the next stage from a fixed transition table. Within a
stage one role owns the output: the proposer owns the plan, the engineer owns the diff, the
validator owns pass or fail, the synthesizer owns the report. The critic owns nothing; it can
only block. No agent can declare victory or skip a stage.

## Reading the report

`report.md` has, in order:

- **Status and verdict.** `implemented` means tests passed and the validator ran. Other statuses:
  `implemented_with_objections`, `tests_failing`, `plan_accepted` (autonomy 1), `rejected`,
  `needs_input`, `needs_decomposition`, `advised` (autonomy 0), `halted_budget`,
  `halted_usage_limit`, `halted_error`.
- **What was built, test results, disagreements, remaining risks, recommended next experiment.**
  Written by the synthesizer from the run's records. Disagreements list both positions; a
  criticism the proposer rejected or deferred always appears here.
- **Proposal history.** Every proposal and the critique it received, with each problem and its
  severity, and every criticism the proposer rejected or deferred with the reason.
- **Acceptance criteria.** The locked list with its timestamp.
- **Implementation.** Branch, commits, changed files, known gaps, diff artifacts.
- **Test runs.** Command, exit code, counts, duration, per attempt.
- **Validator review.** One row per criterion: satisfied or not, with the cited evidence.
- **Decisions.** Every state transition with its reason.
- **Usage.** Calls, tokens, and cost per provider. Cost shows only for models in the pricing table.

For the full audit trail:

```bash
uv run roundtable calls <run_id> -c examples/project.yaml --full
```

prints every call: role, provider, model, attempt, the exact messages the model received, its
reply, tokens, latency, and cost.

## Working on an existing project

Set `project.repo` to the directory. If it is a git repository, each run gets its own
**worktree** on a new branch `roundtable/<run_id>`; the engineer works there and the
orchestrator commits there. Your checked-out branch and working tree are never touched.

When the run finishes:

```bash
cd ~/your/repo
git log main..roundtable/<run_id>          # what the run committed
git diff main...roundtable/<run_id>        # the full change
git merge roundtable/<run_id>              # if you want it
git worktree remove runs/<project>/<run_id>/workspace   # clean up (path is in the report)
git branch -D roundtable/<run_id>          # if you do not
```

A directory that is not a git repo is copied into the workspace (skipping `.git`, `.venv`,
`node_modules`, caches) and given a local git repo so diffs and commits still work. Omitting
`repo` gives an empty workspace, which is what the example uses.

Set `project.test_command` to whatever proves the work (`python -m pytest tests/ -q`,
`npm test`, `make check`). Set `project.python` to the repo's own interpreter when the tests
need its dependencies.

## Engineer modes

**Agent mode** (default for CLI providers): the CLI is launched inside the worktree with its
own tools, exactly as you would run `codex` or `claude` there by hand. It reads, edits, and runs
tests itself, then returns a short JSON summary. The orchestrator captures the resulting diff.
Containment: Codex runs under its workspace-write sandbox; Claude Code auto-accepts edits inside
the directory and shell is limited to python, pytest, and uv; Grok has no sandbox, only the
worktree boundary and the prompt. Nothing outside the worktree is writable by design, and the
worktree is disposable.

**Answer mode** (default for API providers, or `mode: answer`): the model returns complete
files as JSON; the orchestrator validates paths (no `..`, no absolute paths, nothing under
`.git`) and writes them. Slower to converge on real repos because the model cannot explore,
but it works with any backend including local models.

## Budgets and quota

Caps in `budget` are checked before every call. Two things to know about CLI providers:

- Each call carries the tool's own system prompt, roughly 10k to 15k tokens for Claude and Grok
  and about 14k for Codex, before your content. Budget by calls as much as by tokens.
- Subscriptions have rolling five-hour and weekly limits shared with your interactive use. A
  usage-limit error halts the run with status `halted_usage_limit`; the report is still written.

The reported dollar figures for CLI calls are the vendor's list-price estimates, marked
`billing: subscription` in the database. They tell you what a run would cost on the API; they
are not a bill.

## Choosing roles

Observed so far on the example project:

- **Grok as critic** is the strongest reviewer of the three but slow (two to four minutes and
  10k+ output tokens per critique at medium effort) and occasionally returns nothing on long
  prompts. Give it `fallback: claude_cli` so a stalled critique does not end the run.
- **Claude as proposer, reviser, and synthesizer** is fast (30 to 50 seconds) and writes precise
  criteria and honest reports.
- **Codex as engineer** in agent mode edits and tests inside the worktree.
- **A local model as validator** is free and independent, but weak: qwen3 30B got a simple
  drawdown question wrong twice. It is fine for checking test output against criteria; do not
  expect it to catch subtle logic errors. Any backend can take any role; swap in the config.

Point critic and proposer at different vendors when you can. Independence is the point.

## Troubleshooting

| symptom | cause and fix |
|---|---|
| `agent 'x' cannot run: ... not on PATH` | Install or log in to that CLI, or point the role at another provider. |
| `... API_KEY not set` | Export the variable, or use a `cli` provider instead. |
| Grok call halts with `timed out after 300s` | Raise `timeout_s` on the critic (600 is safe) or set `effort: low`. |
| Grok `model did not produce structured output` | On long prompts (a revised proposal, for example) Grok sometimes spends 10k+ tokens reasoning and then cancels its own turn with no text, at any effort level. The attempt is logged and a repair round runs. Give the critic a `fallback:` provider so the run continues; the report notes who answered. |
| status `halted_error: workspace: ...` | `project.repo` does not exist, or a worktree for that branch already exists. |
| tests "command not found" | `test_command` needs a tool that is not on PATH inside the workspace; set `project.python` or use a full path. |
| cost shows "partly unknown" | Add the model to `pricing.yaml`. Deliberate: unknown prices are never guessed. |
| Claude CLI bills your API key | It will if `ANTHROPIC_API_KEY` is set and `use_api_key: true`. By default keys are stripped from CLI child processes. |

## Layout on disk

```
runs/<project>/
  roundtable.db                    every run for this project
  <run_id>/
    workspace/                     the worktree or copy the engineer worked in
    artifacts/                     diffs and test output, one file per artifact id
    report.md
```
