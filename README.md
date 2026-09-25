# Roundtable

Multi-model AI collaboration. Claude, Codex, Grok, Gemini, and local models work as a team of
specialists through one Python orchestrator:
propose → critique → revise → implement → test → review → synthesize. Every call is
logged to SQLite. Agents exchange schema-validated JSON, never raw conversation.

Design: [`docs/DESIGN.md`](docs/DESIGN.md). Status: **M1 complete**. The propose → critique →
revise loop runs live with budgets, locked acceptance criteria, and a report. M2 adds the
engineer (a CLI agent inside a per-run git worktree), test execution, validator, and review.

## Providers

Roundtable can use the CLIs you already log in to, with no API keys:

| Provider | Backend | Auth |
|---|---|---|
| `claude_cli` | `claude -p` headless | claude.ai subscription |
| `codex_cli`  | `codex exec` | ChatGPT login |
| `grok_cli`   | `grok --single` | xAI login |
| `ollama`     | local Ollama | none |
| `anthropic`, `openai`, `xai`, `gemini` | vendor APIs | `*_API_KEY` env vars |

Every provider implements the same `run(messages, schema=...) -> Completion`.
Swapping a role between providers is one line in `project.yaml`.

## Quick start

```bash
uv sync
uv run roundtable providers -c examples/project.yaml          # what can run right now
uv run roundtable run examples/project.yaml                    # the pipeline; writes runs/<project>/<run>/report.md
uv run roundtable runs -c examples/project.yaml                # recent runs
uv run roundtable show <run_id> -c examples/project.yaml       # the report again
uv run roundtable calls <run_id> -c examples/project.yaml --full   # the audit log: every prompt, reply, token count
uv run roundtable call proposer "Is 17 prime?" -c examples/project.yaml   # one role, one prompt
uv run pytest
```

## How a run works

```
INIT → PROPOSE → CRITIQUE ─┬─ ACCEPT ──────────────────► IMPLEMENT (M2) → TEST → REVIEW → SYNTHESIZE → DONE
             ▲             ├─ REVISE, round < max ──► REVISE ┘
             └─────────────┤─ REVISE at max rounds ──► IMPLEMENT with open criticisms
                           ├─ REJECT ─────────────────► SYNTHESIZE
                           └─ NEEDS_EVIDENCE / NEEDS_EXPERIMENT ─► SYNTHESIZE + open questions
Any stage: budget exceeded or provider failure → HALTED → SYNTHESIZE (one call) → report
```

- The proposer first judges scope; an oversized objective returns `needs_decomposition` with a split.
- Acceptance criteria are locked (hashed) when the proposal is accepted, before any code exists.
- The critic never sees which model wrote the proposal. A critique that ACCEPTs with no problems
  must list what it checked; an ACCEPT with an open blocker is rejected by schema.
- Budgets (calls, tokens, dollars, seconds, rounds) are checked before every call. A halted
  run still gets a report; the synthesizer is allowed one call past the budget for it.
- Autonomy 0 stops after the first critique; 1 stops at an accepted plan; 2 implements (M2).

## Layout

```
roundtable/
  config.py        project.yaml → typed config (${VAR} expansion, provider checks)
  store.py         SQLite DDL + repository functions; agent_calls is the audit log
  agents.py        role + provider + schema; one repair round on invalid JSON
  pricing.py       $/Mtok table; unknown models cost NULL, never a made-up number
  schemas.py       inter-agent JSON models (Proposal, Critique, Synthesis, ...) with honesty validators
  context.py       context packs per role; <evidence> wrapper for untrusted text
  budget.py        hard caps checked before every call
  pipeline.py      the state machine
  report.py        the human report (facts from SQLite + the synthesizer's narrative)
  prompts/         one .md per role
  providers/
    base.py        Message, Usage, Completion, Provider protocol, JSON helpers
    cli/           claude, codex, grok subprocess adapters (subscription auth)
    api.py         anthropic, openai_compat (OpenAI + xAI), gemini, ollama
    recorded.py    canned outputs + prompt guards for offline tests
  cli.py           roundtable run | runs | show | calls | call | providers
examples/          project.yaml, pricing.yaml
runs/              per-project SQLite DB and per-run workspaces (gitignored)
```

## Notes

- CLI providers run with `*_API_KEY` stripped from the child environment so they use
  the login, not the API. Set `use_api_key: true` on a provider to bill the API instead.
- Answer-mode calls run in an empty temp directory so no `CLAUDE.md` / `AGENTS.md` leaks in.
- Codex strict mode needs `additionalProperties: false` everywhere; the adapter handles it.
- The Gemini CLI's individual Google login is no longer accepted by that client (checked
  2026-09-25); Gemini needs `GEMINI_API_KEY`.

## License

MIT. See [LICENSE](LICENSE).
