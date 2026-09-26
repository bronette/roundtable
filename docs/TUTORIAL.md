# Tutorial: your first run in ten minutes

This walks through one complete run on the bundled example, then points Roundtable at a
project of your own. Every command is copy-pasteable. Nothing here needs an API key.

## 1. Install

```bash
git clone https://github.com/bronette/roundtable
cd roundtable
uv sync
```

You need Python 3.12+ and [uv](https://docs.astral.sh/uv/). `uv sync` creates the virtual
environment and installs everything.

## 2. Sign in to the model CLIs you have

Roundtable drives the same command-line tools you already use, with the logins they already
hold. Sign in once, interactively, to whichever of these you have:

| Tool | Sign in with | Command |
|---|---|---|
| Claude Code | claude.ai subscription | `claude login` |
| Codex | ChatGPT | `codex login` |
| Grok | xAI account | `grok login` |
| Antigravity | Google AI Pro | `agy` (then sign in when prompted) |
| Ollama | nothing | `ollama serve` and `ollama pull qwen3:30b-a3b` |

You do not need all of them. One is enough to start; the example config below can be pointed
at whatever you have.

## 3. Check what can run

```bash
uv run roundtable providers -c examples/project.yaml
```

This prints every provider in the config and whether it is usable right now. A red entry
names what is missing (a CLI not on your PATH, a key not set). Every seat in `examples/project.yaml`
must map to a green provider before a run will start.

If you only have Claude, edit the `agents:` block so every seat says `provider: claude_cli`.
Roles are configuration; any seat can use any provider.

## 4. Run the example

```bash
uv run roundtable run examples/project.yaml
```

The example objective is small: a max-drawdown function with tests. You will see each stage as
it happens:

```
[PROPOSE   ] proposer → claude-...  P1: Add max_drawdown(...) ...
[CRITIQUE  ] critic → grok-...      C1 on P1 → REVISE  (3 problems, 1 blockers)
[REVISE    ] reviser → claude-...   P2 revises P1: 3 fixed, 0 rejected, 0 deferred
[CRITIQUE  ] critic → grok-...      C2 on P2 → ACCEPT
[CRITIQUE  ] acceptance criteria locked (sha256:...)
[IMPLEMENT ] engineer → claude-...  I1: 2 file(s) changed, commit ... on roundtable/run_...
[TEST      ] T1: `python -m pytest -q` exit=0 passed=20 failed=0
[REVIEW    ] validator → qwen3...   C3 on I1 → ACCEPT  (7/7 criteria satisfied)
[SYNTHESIZE] synthesizer → claude-... verdict ACCEPT
```

A full run takes ten to twenty minutes. Most of that is the critic thinking. At the end you
get a path to `report.md`.

## 5. Read the report

Open `runs/dd-demo/<run_id>/report.md`. Read it top to bottom once:

1. **Status and verdict.** `implemented` with `ACCEPT` is the good case. Anything else says
   why in one line.
2. **What was built and the test results.** Written from the records, not from a model's
   opinion of its own work.
3. **Disagreements.** Every criticism the proposer rejected or deferred, with both sides. This
   section is the reason to use several models instead of one.
4. **Remaining risks and the recommended next experiment.**
5. **Acceptance criteria**, locked before the code existed, and the **validator review** that
   checked each one against real output.

Then look at the code:

```bash
cd runs/dd-demo/<run_id>/workspace
git log --oneline          # the base commit and the engineer's commit
cat dd.py test_dd.py
```

To see exactly what any model was told and what it replied:

```bash
uv run roundtable calls <run_id> -c examples/project.yaml
uv run roundtable call-show <call_id> -c examples/project.yaml
```

## 6. Point it at your own project

Copy the example config and change the project block:

```yaml
project:
  name: my-project
  repo: ~/code/my-project              # any git repo; your checkout is never touched
  python: ~/code/my-project/.venv/bin/python
  test_command: python -m pytest -q
  objective: >
    Add input validation to the CSV importer so malformed rows are reported
    with line numbers instead of crashing the import.
  requirements:
    - "Malformed rows are collected and reported; the import continues"
    - "Existing tests pass unchanged"
    - "New tests cover a malformed row, an empty file, and a header-only file"
```

Three things make a good first objective: your test suite runs in under a minute, the change is
small and you already want it, and you would not mind throwing the branch away.

```bash
uv run roundtable run my-project.yaml
```

The engineer works in a git worktree on a new branch `roundtable/<run_id>`. When the report
says the tests passed and the validator accepted:

```bash
cd ~/code/my-project
git diff main...roundtable/<run_id>
git merge roundtable/<run_id>              # only if you like it
git worktree remove <path from the report>
```

## 7. If something stops

Runs halt on budgets, provider outages, or invalid output. The report is still written and says
why. Fix the cause and continue from where it stopped, without paying again for what completed:

```bash
uv run roundtable resume <run_id> -c my-project.yaml
uv run roundtable resume <run_id> -c my-project.yaml --max-calls 24    # if the halt was a budget
```

## 8. Research questions: pre-register, then run

For anything where the temptation is to keep tweaking until the numbers look good, use an
experiment. Write the criteria first:

```yaml
# prereg.yaml
hypothesis: The signal has positive expectancy after costs on the held-out window.
expected_result: mean_return_bps >= 3 with n_trades >= 200
method: run scripts/backtest.py --window holdout --costs realistic
data: data/holdout.parquet, 2025-01..2025-06, never used for tuning
success_criteria: ["n_trades >= 200 AND mean_return_bps >= 3 AND ci_lower_bps > 0"]
failure_criteria: ["n_trades >= 200 AND ci_lower_bps <= 0"]
n_trials: 1
command: python scripts/backtest.py --window holdout --costs realistic --json
repo: ~/code/my-strategy
```

```bash
uv run roundtable experiment preregister prereg.yaml -c my-project.yaml   # locks it: E1
uv run roundtable experiment run E1 -c my-project.yaml                     # runs it once
uv run roundtable experiment interpret E1 -c my-project.yaml               # reads it: PASS / KILL / INCONCLUSIVE
```

Your command must print its metrics as JSON, or write them to the file named by the
`ROUNDTABLE_METRICS` environment variable. The criteria cannot be edited after locking, the
experiment cannot be run twice, and the decision is applied by code from the model's reading of
each criterion, with a second model checking that reading.

## Where to go next

- [User guide](GUIDE.md) for how each stage works, what "solved" means, and troubleshooting.
- [Configuration](CONFIG.md) for every key in the project file.
- [Providers](PROVIDERS.md) for each backend's details and how to add one.
