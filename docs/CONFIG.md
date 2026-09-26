# Configuration reference

A project is one YAML file. `roundtable run project.yaml` reads it, expands `${VAR}` and
`${VAR:-default}` from the environment, validates it, and refuses to start if any agent's
provider cannot be used (missing key, binary not on PATH).

Relative paths (`runs_dir`, `pricing`) resolve against the config file's directory.

```yaml
project:
  name: dd-demo                  # database and run directory name: runs/<name>/
  objective: >                   # what to achieve, in plain language
    Implement max_drawdown(...) with pytest tests.
  requirements:                  # numbered in prompts; every acceptance criterion must map to one
    - "Returns 0.0 for empty series"
  repo: ~/code/some-project      # optional: any directory or git repo to work on (see below)
  test_command: python -m pytest -q   # run by the orchestrator after the engineer finishes
  python: /path/to/python        # optional: interpreter substituted for `python` in test_command
  test_timeout_s: 300
  domain: trading                # optional; trading adds proposer rules and the critic checklist

autonomy: 2                      # see "Autonomy levels"
runs_dir: ../runs                # where runs/<project>/<run_id>/ and roundtable.db live
pricing: pricing.yaml            # optional $/Mtok table; unknown models cost NULL

budget:                          # hard caps; checked before every model call
  max_rounds: 3                  # critique → revise cycles
  max_fix_rounds: 1              # engineer retries after failing tests
  max_calls: 12                  # model calls per run (repair attempts count)
  max_tokens: 400000             # input + output across all providers
  max_cost_usd: 5.00             # only enforced when every model used has a price
  max_seconds: 1800
  per_provider:                  # optional per-provider caps
    grok_cli: { max_calls: 4, max_tokens: 150000 }

providers:                       # name → how to reach a backend
  claude_cli: { type: cli, cli: claude }
  codex_cli:  { type: cli, cli: codex, max_turns: 3 }
  grok_cli:   { type: cli, cli: grok }
  agy_cli:    { type: cli, cli: agy }             # Google Antigravity, Google AI Pro login
  gemini_cli: { type: cli, cli: gemini }          # needs GEMINI_API_KEY
  ollama:     { type: api, sdk: ollama, host: http://localhost:11434 }
  anthropic:  { type: api, sdk: anthropic,     api_key_env: ANTHROPIC_API_KEY }
  openai:     { type: api, sdk: openai_compat, api_key_env: OPENAI_API_KEY }
  xai:        { type: api, sdk: openai_compat, api_key_env: XAI_API_KEY, base_url: https://api.x.ai/v1 }
  gemini:     { type: api, sdk: gemini,        api_key_env: GEMINI_API_KEY }

agents:                          # role → provider + model + knobs
  proposer:    { provider: claude_cli }
  critic:      { provider: grok_cli, effort: medium, timeout_s: 600, fallback: claude_cli }
  reviser:     { provider: claude_cli }
  engineer:    { provider: codex_cli, timeout_s: 900, max_turns: 40 }
  validator:   { provider: ollama, model: qwen3:30b-a3b, timeout_s: 600 }
  synthesizer: { provider: claude_cli }

permissions:                     # what the orchestrator will do with a role's output
  engineer:  { workspace: write, run_tests: false }
  validator: { workspace: read,  run_tests: true }
```

## `project`

| key | default | meaning |
|---|---|---|
| `name` | required | Names the SQLite file `runs/<name>/roundtable.db` and the run directories. |
| `objective` | required | The task. Sent verbatim to every role. |
| `requirements` | `[]` | Numbered list. The proposer must cover each with an acceptance criterion; the validator checks each. |
| `repo` | none | Directory to work on. A git repo gets a **worktree** on a new branch `roundtable/<run_id>`; a plain directory is **copied** and given a local git repo; omitted means a **greenfield** empty workspace. The original is never modified except for the new branch ref. |
| `base_ref` | checkout HEAD | Git repos only: the branch or commit the run's worktree starts from. Use it to continue work on a branch a previous run produced. |
| `read_in_place` | false | Audits only (autonomy 0 or 1, no engineer): read-mode seats work in the live checkout instead of a worktree, so gitignored data, logs and reports are visible to every seat alike. Read mode cannot write. Without this, a worktree hides ignored files and one seat may cite files another cannot see. |
| `context_files` | `[]` | Documents (memos, pre-registrations, specs) injected as evidence blocks into every seat's prompt except the engineer's. Paths relative to the config file. Each is capped at 20k characters. |
| `test_command` | `python -m pytest -q` | Run by the orchestrator in the workspace with API keys stripped from the environment and a timeout. Exit code 0 means pass. pytest summary counts are parsed when present. |
| `python` | roundtable's own interpreter | Substituted when `test_command` starts with `python` or `python3`. Point this at the target repo's venv interpreter when it has one. |
| `test_timeout_s` | 300 | |
| `env` | `{}` | Extra environment variables for the test command and experiment commands, for example `DB_PATH` when tests read a developer database that a fresh worktree does not have. API keys are still stripped. |

## `autonomy`

| level | behaviour |
|---|---|
| 0 | Advise only. One proposal, one critique, then the report. |
| 1 | Propose and revise until accepted or the round limit; report the accepted plan. No code is written. |
| 2 | Level 1, then implement in the workspace, run tests, fix once, validate, report. Nothing is merged. |
| 3, 4 | Reserved for experiments and continuous investigation (M5, M6). Currently behave as 2. |

## `budget`

Every cap is checked before each model call. When one trips the run halts, the synthesizer is
allowed one call for the report, and the report says which cap tripped. Repair attempts (a
second call after invalid JSON) count toward `max_calls` and `max_tokens`.

`max_cost_usd` is enforced when every call has a cost: from the pricing table, or from the
CLI's own estimate when the model is not in the table (Claude Code and Grok report one). A call
with neither leaves the total "partly unknown" and the cost cap is not enforced for that run.

## `providers`

| key | applies to | meaning |
|---|---|---|
| `type` | all | `cli` (subprocess, subscription login) or `api` (SDK, key from env). |
| `cli` | cli | `claude`, `codex`, `grok`, `agy` (Google Antigravity, Google AI Pro login), or `gemini` (needs `GEMINI_API_KEY`). |
| `binary` | cli | Override the executable name or path. |
| `use_api_key` | cli | Keep `*_API_KEY` in the child environment. Default false: keys are stripped so the CLI uses its login and does not bill the API. |
| `max_turns` | cli (answer mode) | Turns allowed per answer-mode call. Grok needs more than one on long prompts; default 3. |
| `extra_args` | cli | Appended verbatim to the command line. |
| `sdk` | api | `anthropic`, `openai_compat`, `gemini`, `ollama`. |
| `api_key_env` | api | Name of the environment variable holding the key. Checked at start. |
| `base_url` | openai_compat | e.g. `https://api.x.ai/v1` for Grok over the API. |
| `host` | ollama | Default `http://localhost:11434`. |

## `agents`

| key | default | meaning |
|---|---|---|
| `provider` | required | A key from `providers`. |
| `model` | backend default | Model id or alias. An empty string means "use the backend's default", which lets `${VAR:-}` work. |
| `temperature` | 0.2 | Ignored by CLIs. |
| `max_tokens` | 4096 | Output cap for API providers. |
| `timeout_s` | 300 | Wall-clock limit per call. Grok critiques take 2 to 4 minutes; engineers in agent mode may need 900+. |
| `effort` | none | `low`, `medium`, `high`. Maps to `grok --reasoning-effort` and codex `model_reasoning_effort`. Ignored elsewhere. |
| `retries` | 2 | Extra attempts on transient provider errors (timeouts, 5xx, disconnects), with backoff of 3, 9, 27 seconds. Not applied to usage limits, missing credentials, or invalid output. |
| `fallback` | none | Another provider key. If this seat's provider fails (invalid JSON twice, timeout, outage), the fallback answers the same prompt. The failed attempts stay in the audit log under the original provider and the report records who actually answered. |
| `mode` | auto | `agent` (engineer only): the CLI runs inside the worktree with tools and edits files itself. `read` (any seat but the engineer): the CLI runs inside the worktree with read-only tools, so proposers and critics can inspect code and results and cite paths; Grok has no read-only mode and falls back to `answer`. `answer`: no workspace; the model works from the prompt alone (the engineer then returns files as JSON). Auto picks `agent` for a cli engineer, `answer` otherwise. |
| `max_turns` | 40 | Agent mode: tool-use turns allowed in one engineer call. |

Roles the pipeline uses: `proposer`, `critic`, `reviser`, `engineer`, `validator`, `synthesizer`.
Experiments use `interpreter` and `experiment_critic` when present, else `synthesizer` and `critic`.
Any role can point at any provider. The reviser is usually the same backend as the proposer.
Missing `synthesizer` produces a fact-only report; missing `engineer` or `validator` fails at
the stage that needs them.

## `permissions`

Currently enforced: `engineer.workspace` must be `write` for the implement stage to run.
The other fields document intent and are reserved for the tool system (M4).

## Environment variables

Nothing is read from `.env` files. The CLIs use their own stored logins. API providers read
the variable named in `api_key_env`. Model ids are best kept out of the file with
`model: "${ROUNDTABLE_CLAUDE_MODEL:-}"`.
