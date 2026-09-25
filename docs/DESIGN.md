# Roundtable — design (pre-implementation)

Status: DRAFT, awaiting approval. Nothing is implemented yet.
Date: 2026-09-25

Roundtable is a Python orchestrator that runs several LLM providers as a team of
specialists (propose → critique → revise → implement → test → review → synthesize),
with every call logged to SQLite and structured JSON between agents.

**Scope.** Roundtable is a general-purpose, standalone tool. It is pointed at any
directory or git repository (yours, a fresh one, or an unrelated open-source
project) and any objective. It has no dependency on Oracle, on `~/code-projects`,
or on any existing project; Oracle was used only as a lookup while writing this
document. Supported providers are Anthropic (Claude), OpenAI (GPT/Codex models),
Google (Gemini), xAI (Grok), and Ollama (local models). Adding a provider means
adding one adapter file; no workflow code changes.

---

## 1. Analysis of the proposed architecture

What the spec gets right, and what I would keep unchanged:

- **Hub-and-spoke through the orchestrator.** Agents never talk to each other; the
  orchestrator builds each agent's context from shared state. This is the single
  most important decision and everything below assumes it.
- **Structured outputs validated by schema.** Cheap, and it is what makes the
  critic/validator loop auditable.
- **Verdict vocabulary** (`ACCEPT / REVISE / REJECT / NEEDS_EVIDENCE / NEEDS_EXPERIMENT`).
- **SQLite as the only store.** Correct for a single-machine, single-operator tool.
- **Experiments with criteria locked before results.** This matches the
  pre-registration discipline already in `research/HYPOTHESIS_GATE.md`.
- **Budgets as hard stops, with a human summary when hit.**

Facts about this machine that change the plan:

| Fact | Consequence |
|---|---|
| Only `ANTHROPIC_API_KEY` is set. No OpenAI or xAI key found in env or `.env` files. | Adapters for OpenAI/xAI can be written and unit-tested against recorded responses, but the four-model demo in the first milestone cannot run until you add keys. Any role can be pointed at Anthropic or Ollama meanwhile. |
| xAI's API is OpenAI-compatible (`https://api.x.ai/v1`). | `GrokProvider` is `OpenAIProvider` with a different base URL and key. |
| Gemini has its own SDK (`google-genai`) with native JSON-schema output. | Fourth adapter, `gemini`. Needs `GEMINI_API_KEY`, also not present yet. Four adapters total: anthropic, openai_compat, gemini, ollama. |
| Ollama has `qwen3:30b-a3b`, `qwen2.5-coder:14b`, `deepseek-r1:14b`. No Llama build. | "Llama / local" = `OllamaProvider`, model name from config. |
| `oracle-ai/oracle/llm/ollama_client.py` already wraps Ollama with token usage. | Copy the ~30 lines that matter; do not import `oracle` as a dependency. Roundtable stays standalone. |
| Python 3.14 + `uv`, no SDKs installed globally. | Project gets its own `uv` venv; `requires-python >= 3.12`. |

---

## 2. Unnecessary complexity (cut or defer)

| Spec item | Decision | Why |
|---|---|---|
| General tool system with provider-native function calling (Python exec, shell, git, web, DB, APIs…) | **Defer to M4.** In M1–M3 an agent does not call tools. It returns structured output and the orchestrator performs the one or two actions that output implies (write files to the workspace, run pytest). | Tool-calling parity across Anthropic, OpenAI, xAI, and Ollama is the flakiest part of any multi-provider system. Keeping the agent loop to one request → one validated JSON reply makes the first milestone deterministic and cheap. Role permissions still exist; they gate what the *orchestrator* will do with an agent's output. |
| Git branches per agent, diff review before merge | **Defer to M4.** M1–M3 use a plain workspace directory per run; the engineer's files are written there and versioned by copying into `artifacts/`. | Git adds value once there are multiple concurrent engineers or long-lived projects. The first milestone has one engineer and one task. |
| Planner that decomposes objectives into a task graph | **Defer to M6.** The first milestone is a single task. | Your own first-milestone list starts at "Claude proposes", not "Planner decomposes". A planner on top of a working single-task pipeline is a small addition; a planner first is a large one. |
| Researcher role, hypotheses table populated by LLMs | **Defer to M5/M6.** Table exists from day one so the schema does not churn. | No research step in the first milestone. |
| Autonomy levels 0–4 | **Keep the setting, implement levels 0–2 only.** Level 3–4 (autonomous experiments, continuous investigation) come with M5. | Level 2 is exactly what the first milestone needs: write code in a workspace, run tests. |
| Vector-search memory | **Cut.** "Memory" in v1 is (a) the project's own SQLite rows selected by task, and (b) a `lessons` table with keyword search. | You said start simple. Selecting rows by task already achieves "do not dump the whole conversation into every prompt". |
| Per-provider *spending* limits | **Keep counts and tokens per provider; cost is derived from a `pricing.yaml`.** Unknown model → cost `NULL`, token cap still enforced. | Providers do not return dollar amounts. Adapters stay dumb; the orchestrator prices. |
| Separate `agent_outputs` and `messages` blobs | **One `agent_calls` table** holds prompt, response, parsed JSON, usage, timing. Domain tables (`proposals`, `critiques`…) reference it. | One row per LLM call answers every "which model / which prompt / when / what did it see / what did it produce" question. |
| Web dashboard | **Cut for now.** `roundtable show <run>` prints the same information as a rich table. | As you specified. |
| Retry/backoff framework, async, parallel agents | **Cut.** Synchronous, one call at a time, one retry on schema failure. | Sequential is easier to log and debug; parallel research agents arrive with M6. |

Net: the first milestone is about **8 modules and roughly 1,200 lines**, most of them
schemas and SQL.

---

## 3. Smallest viable architecture

```
project.yaml ──► Orchestrator ──► Pipeline (state machine)
                     │                  │
                     │        ┌─────────┴──────────┐
                     │        ▼                    ▼
                     │   Agent(role)          Actions (orchestrator-only)
                     │   = prompt template      write_workspace(files)
                     │   + provider             run_tests(workspace)
                     │   + output schema
                     │        │
                     ▼        ▼
                 Store (SQLite) ◄──── every call, artifact, verdict, decision
```

Five concepts, nothing else:

1. **Provider** – sends messages, returns text (+ parsed JSON if a schema was
   requested) and usage. Four implementations: `anthropic`, `openai_compat`
   (OpenAI and xAI), `gemini`, `ollama`.
2. **Agent** – a role name bound to a provider, a model, a system prompt
   template, an output schema, and a permission set. Fully described in YAML.
3. **Pipeline** – a state machine over `Stage` values. Each stage builds a
   context pack from the store, calls one agent, validates, writes rows, and
   returns the next stage. Budget checked at every transition.
4. **Store** – SQLite, WAL mode, one file per project. Thin repository functions,
   no ORM.
5. **Actions** – the only side effects: write files into the run workspace, run
   `pytest` in a subprocess. Gated by the role's permissions.

---

## 4. Directory structure

```
roundtable/
├── pyproject.toml                 # uv-managed; deps: anthropic, openai, google-genai, ollama, pydantic, pyyaml, rich, typer
├── README.md
├── docs/
│   └── DESIGN.md                  # this file
├── examples/
│   ├── project.yaml               # the first-milestone example
│   └── pricing.yaml               # $/Mtok per model; unknown → cost NULL
├── roundtable/
│   ├── __init__.py
│   ├── cli.py                     # `roundtable run|show|calls|resume`
│   ├── config.py                  # pydantic models for project.yaml; env-var key loading
│   ├── schemas.py                 # all inter-agent JSON (Proposal, Critique, …)
│   ├── store.py                   # SQLite DDL + repository functions
│   ├── budget.py                  # token / call / round / cost / time caps
│   ├── providers/
│   │   ├── __init__.py            # registry: name → class
│   │   ├── base.py                # Message, Usage, Completion, Provider protocol
│   │   ├── anthropic_provider.py
│   │   ├── openai_compat.py       # OpenAI + xAI (base_url)
│   │   ├── gemini_provider.py
│   │   └── ollama_provider.py
│   ├── agents.py                  # Agent: role + provider + prompt + schema + permissions; `call()`
│   ├── prompts/                   # one .md per role; {{placeholders}} filled from the context pack
│   │   ├── proposer.md
│   │   ├── critic.md
│   │   ├── reviser.md
│   │   ├── engineer.md
│   │   ├── validator.md
│   │   └── synthesizer.md
│   ├── actions.py                 # write_workspace(), run_tests(); permission checks live here
│   ├── pipeline.py                # Stage enum + transition table + run loop
│   └── report.py                  # final human summary (markdown) from the store
├── tests/
│   ├── test_schemas.py
│   ├── test_store.py
│   ├── test_providers_fake.py     # FakeProvider returns canned JSON; drives the whole pipeline offline
│   └── test_pipeline.py
└── runs/                          # gitignored: <project>/<run_id>/workspace, artifacts, roundtable.db
```

Config file, `examples/project.yaml`:

```yaml
project:
  name: dd-demo
  objective: >
    Implement max_drawdown(equity: list[float]) -> float in dd.py, returning the
    largest peak-to-trough decline as a positive fraction (0.0 if none), with pytest tests.
  requirements:
    - "Returns 0.0 for monotonically increasing or empty series"
    - "Handles a series that ends at its trough"
    - "Pure Python, no third-party dependencies"
  repo: ~/some/project             # ANY directory or git repo. Omit for a greenfield task.
                                   # Each run works in its own copy (git worktree if repo is git,
                                   # else a file copy) under runs/<project>/<run_id>/workspace.
                                   # The original is never modified until a human merges (M4).

autonomy: 2                        # 0 advise, 1 propose, 2 write to workspace + run tests

budget:
  max_rounds: 3                    # critique → revise cycles
  max_fix_rounds: 1                # engineer retries after failing tests
  max_calls: 12
  max_tokens: 200000
  max_cost_usd: 5.00
  max_seconds: 900
  per_provider:
    anthropic: { max_tokens: 120000 }
    ollama:    { max_tokens: 200000 }

providers:
  anthropic:    { type: anthropic,     api_key_env: ANTHROPIC_API_KEY }
  openai:       { type: openai_compat, api_key_env: OPENAI_API_KEY }
  xai:          { type: openai_compat, api_key_env: XAI_API_KEY, base_url: https://api.x.ai/v1 }
  gemini:       { type: gemini,        api_key_env: GEMINI_API_KEY }
  ollama:       { type: ollama,        host: http://localhost:11434 }

agents:
  proposer:     { provider: anthropic, model: ${ANTHROPIC_MODEL} }
  critic:       { provider: xai,       model: ${XAI_MODEL} }
  reviser:      { provider: anthropic, model: ${ANTHROPIC_MODEL} }
  engineer:     { provider: openai,    model: ${OPENAI_MODEL} }      # Codex-class model
  validator:    { provider: gemini,    model: ${GEMINI_MODEL} }
  reviewer:     { provider: ollama,    model: qwen3:30b-a3b }        # optional second, local review
  synthesizer:  { provider: anthropic, model: ${ANTHROPIC_MODEL} }

permissions:                       # what the orchestrator will DO with a role's output
  proposer:    { workspace: none,  run_tests: false }
  critic:      { workspace: read,  run_tests: false }
  reviser:     { workspace: none,  run_tests: false }
  engineer:    { workspace: write, run_tests: false }
  validator:   { workspace: read,  run_tests: true }
  synthesizer: { workspace: read,  run_tests: false }
```

`${VAR}` expands from the environment so model IDs are never in the repo.
Missing keys fail at config load with the provider and env var named, before
any call is made.

---

## 5. Provider interface

```python
# roundtable/providers/base.py
from dataclasses import dataclass, field
from typing import Literal, Protocol
from pydantic import BaseModel

@dataclass(frozen=True)
class Message:
    role: Literal["system", "user", "assistant"]
    content: str

@dataclass(frozen=True)
class Usage:
    input_tokens: int
    output_tokens: int
    cached_input_tokens: int = 0

@dataclass
class Completion:
    text: str                     # raw model text (or serialized JSON when schema was used)
    parsed: dict | None           # provider-side JSON if it produced one; orchestrator re-validates
    usage: Usage
    provider: str
    model: str
    latency_ms: int
    request_id: str | None = None
    stop_reason: str | None = None
    raw: dict = field(default_factory=dict)   # provider response, stored for audit

class ProviderError(Exception): ...
class SchemaError(ProviderError): ...      # model produced no parseable JSON

class Provider(Protocol):
    name: str
    def run(
        self,
        messages: list[Message],
        *,
        schema: type[BaseModel] | None = None,
        temperature: float = 0.2,
        max_tokens: int = 4096,
        timeout_s: float = 180.0,
    ) -> Completion: ...
```

How each adapter gets structured output (the only per-provider logic):

| Provider | Mechanism |
|---|---|
| `anthropic` | One tool whose `input_schema` is `schema.model_json_schema()`, `tool_choice` forced to it. Response `tool_use.input` is the JSON. Usage from `usage.input_tokens/output_tokens`. |
| `openai_compat` | `response_format={"type": "json_schema", "json_schema": {...,"strict": true}}`. Same code path for xAI with `base_url`. If a model rejects strict mode, fall back to `json_object` + schema in the system prompt. |
| `gemini` | `response_mime_type="application/json"` + `response_schema=<schema>` in `GenerateContentConfig`. Usage from `usage_metadata`. |
| `ollama` | `format=<json schema dict>` on `chat()`. Usage from `prompt_eval_count/eval_count`. |

The orchestrator, not the adapter, does `schema.model_validate(parsed)`. On failure it
makes **one** repair call: same messages plus an assistant turn with the bad
output and a user turn with the validation error. A second failure marks the
step `FAILED_SCHEMA` and the pipeline halts to synthesis.

Adapters know nothing about roles, prompts, budgets, or the store.

---

## 6. Agent and message schemas

All in `roundtable/schemas.py` as pydantic models. IDs are short and stable
(`P1`, `P1r2`, `C3`, `I1`, `T1`, `R1`, `S1`) so agents can cite each other.

```python
class Verdict(StrEnum):
    ACCEPT = "ACCEPT"; REVISE = "REVISE"; REJECT = "REJECT"
    NEEDS_EVIDENCE = "NEEDS_EVIDENCE"; NEEDS_EXPERIMENT = "NEEDS_EXPERIMENT"

class Severity(StrEnum):
    BLOCKER = "blocker"; MAJOR = "major"; MINOR = "minor"

class Problem(BaseModel):
    severity: Severity
    description: str
    location: str | None = None          # file:line, requirement id, assumption index…

class Proposal(BaseModel):                # proposer + reviser
    proposal_id: str
    revision_of: str | None = None
    claim: str                            # one sentence: what will be built / what is asserted
    approach: str                         # reasoning summary, <= ~300 words
    assumptions: list[str]
    evidence: list[str]                   # things known, cited; empty is allowed and honest
    risks: list[str]
    tests_required: list[str]             # concrete, checkable
    responses_to_criticism: list[CriticismResponse] = []   # reviser only
    confidence: float = Field(ge=0, le=1)

class CriticismResponse(BaseModel):
    problem: str
    action: Literal["fixed", "rejected", "deferred"]
    reason: str

class Critique(BaseModel):
    proposal_id: str
    verdict: Verdict
    problems: list[Problem]               # may be empty ONLY with verdict ACCEPT and a stated reason
    counterarguments: list[str]
    tests_required: list[str]
    confidence: float = Field(ge=0, le=1)

class FileChange(BaseModel):
    path: str                             # relative, no '..', within workspace
    content: str

class Implementation(BaseModel):
    proposal_id: str
    files: list[FileChange]
    test_command: str = "python -m pytest -q"
    notes: str
    assumptions: list[str]
    known_gaps: list[str]

class TestResult(BaseModel):              # produced by actions.run_tests, never by an LLM
    command: str
    exit_code: int
    passed: int; failed: int; errors: int
    duration_s: float
    stdout_tail: str                      # last ~4 KB
    timed_out: bool = False

class RequirementCheck(BaseModel):
    requirement: str
    satisfied: bool
    evidence: str                         # must cite a file/line or a test name

class Review(BaseModel):                  # validator
    implementation_id: str
    verdict: Verdict
    requirement_checks: list[RequirementCheck]
    problems: list[Problem]
    confidence: float = Field(ge=0, le=1)

class Disagreement(BaseModel):
    topic: str
    positions: list[Position]             # {agent_role, position}
    resolved: bool
    resolution: str | None = None

class Synthesis(BaseModel):
    what_was_built: str
    test_summary: str
    disagreements: list[Disagreement]     # preserved, not collapsed
    remaining_risks: list[str]
    recommended_next_experiment: str | None
    overall_verdict: Verdict              # REJECT is a valid, expected outcome
    confidence: float = Field(ge=0, le=1)
```

Anti-sycophancy rules enforced in prompts and validators:

- A `Critique` with `verdict=ACCEPT` and zero problems must include a
  `counterarguments` entry explaining what was checked. Validator rejects
  otherwise → repair call.
- The critic prompt never includes which provider or model wrote the proposal.
- The validator receives requirements + files + test output. It does **not**
  receive the proposal's reasoning or the critic's verdicts, so it checks the
  artifact, not the argument.
- The synthesizer receives all verdicts and must list every `REVISE/REJECT`
  problem that was marked `rejected` or `deferred` by the reviser as an
  unresolved disagreement.

Context packs (what each agent actually sees; nothing else):

For an existing repository the workspace is not empty, so agents need to read
before they write. In M1–M3 this is a two-step engineer stage, still without
tool calling: step one returns `FilesRequested{paths: [...]}` from the file
tree; the orchestrator returns those files (size-capped, wrapped as evidence
blocks); step two returns the `Implementation`. The critic and validator get
the same read-only mechanism. M4 replaces this with native tool calling.

| Role | Receives |
|---|---|
| proposer | objective, requirements, relevant `lessons` (keyword match on objective) |
| critic | objective, requirements, the proposal JSON, workspace file tree (read) |
| reviser | its own prior proposal, the critique JSON |
| engineer | objective, requirements, accepted proposal, workspace file tree, contents of files it asks for (see below), prior `TestResult` + own prior files on a fix round |
| validator | objective, requirements, files, `TestResult` |
| synthesizer | objective, all proposals/critiques/reviews (JSON), `TestResult`, budget summary |

---

## 7. SQLite schema

One database per project, `runs/<project>/roundtable.db`, WAL mode, foreign keys on.

```sql
CREATE TABLE projects (
  id TEXT PRIMARY KEY, name TEXT NOT NULL, objective TEXT NOT NULL,
  requirements_json TEXT NOT NULL, config_json TEXT NOT NULL, created_at TEXT NOT NULL);

CREATE TABLE runs (
  id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id),
  stage TEXT NOT NULL,                       -- current Stage; enables resume
  status TEXT NOT NULL,                      -- running | done | halted_budget | halted_error | rejected
  round INTEGER NOT NULL DEFAULT 0, fix_round INTEGER NOT NULL DEFAULT 0,
  workspace_path TEXT NOT NULL, started_at TEXT NOT NULL, finished_at TEXT,
  halt_reason TEXT);

-- The audit log. One row per LLM call. Answers who/what/when/what-did-it-see/what-did-it-cost.
CREATE TABLE agent_calls (
  id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(id),
  stage TEXT NOT NULL, role TEXT NOT NULL, provider TEXT NOT NULL, model TEXT NOT NULL,
  attempt INTEGER NOT NULL DEFAULT 1,        -- 2 = schema repair call
  prompt_hash TEXT NOT NULL,                 -- sha256 of messages_json; groups identical prompts
  messages_json TEXT NOT NULL,               -- exact input the agent received
  context_refs_json TEXT NOT NULL,           -- ids of rows used to build the prompt (P1, C2, T1 …)
  response_text TEXT, parsed_json TEXT, schema_name TEXT,
  valid INTEGER NOT NULL,                    -- did parsed_json validate
  error TEXT,
  input_tokens INTEGER, output_tokens INTEGER, cached_input_tokens INTEGER,
  cost_usd REAL,                             -- NULL when model not in pricing.yaml
  latency_ms INTEGER, request_id TEXT,
  started_at TEXT NOT NULL, finished_at TEXT NOT NULL);
CREATE INDEX ix_calls_run ON agent_calls(run_id, started_at);

CREATE TABLE proposals (
  id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(id),
  call_id TEXT NOT NULL REFERENCES agent_calls(id),
  revision_of TEXT REFERENCES proposals(id), round INTEGER NOT NULL,
  status TEXT NOT NULL,                      -- proposed | criticized | superseded | accepted | rejected
  body_json TEXT NOT NULL, created_at TEXT NOT NULL);

CREATE TABLE critiques (
  id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(id),
  call_id TEXT NOT NULL REFERENCES agent_calls(id),
  target_kind TEXT NOT NULL, target_id TEXT NOT NULL,   -- proposal | implementation
  verdict TEXT NOT NULL, body_json TEXT NOT NULL, created_at TEXT NOT NULL);

CREATE TABLE artifacts (
  id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(id),
  call_id TEXT REFERENCES agent_calls(id),   -- NULL for test output produced by the orchestrator
  kind TEXT NOT NULL,                        -- file | test_output | report
  path TEXT NOT NULL,                        -- relative to run dir; content copied to artifacts/<id>/
  sha256 TEXT NOT NULL, bytes INTEGER NOT NULL, created_at TEXT NOT NULL);

CREATE TABLE implementations (
  id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(id),
  call_id TEXT NOT NULL REFERENCES agent_calls(id),
  proposal_id TEXT NOT NULL REFERENCES proposals(id), fix_round INTEGER NOT NULL,
  artifact_ids_json TEXT NOT NULL, body_json TEXT NOT NULL, created_at TEXT NOT NULL);

CREATE TABLE test_runs (
  id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(id),
  implementation_id TEXT NOT NULL REFERENCES implementations(id),
  command TEXT NOT NULL, exit_code INTEGER NOT NULL,
  passed INTEGER, failed INTEGER, errors INTEGER, timed_out INTEGER NOT NULL,
  output_artifact_id TEXT REFERENCES artifacts(id), duration_s REAL, created_at TEXT NOT NULL);

CREATE TABLE decisions (                     -- every state transition and every verdict application
  id INTEGER PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(id),
  from_stage TEXT NOT NULL, to_stage TEXT NOT NULL,
  reason TEXT NOT NULL, refs_json TEXT NOT NULL, created_at TEXT NOT NULL);

CREATE TABLE open_questions (
  id INTEGER PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(id),
  raised_by_role TEXT NOT NULL, question TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'open', created_at TEXT NOT NULL);

-- Present from day one so later milestones do not migrate; unused until M5/M6.
CREATE TABLE hypotheses (
  id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id),
  statement TEXT NOT NULL, status TEXT NOT NULL, body_json TEXT NOT NULL, created_at TEXT NOT NULL);

CREATE TABLE experiments (
  id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id),
  hypothesis_id TEXT REFERENCES hypotheses(id),
  prereg_json TEXT NOT NULL,                 -- hypothesis, expected, method, success/failure criteria, data, assumptions
  prereg_hash TEXT NOT NULL,                 -- sha256 of prereg_json; result rows must carry the same hash
  locked_at TEXT NOT NULL,
  result_json TEXT, result_hash_check INTEGER, -- 1 if prereg_hash matched when result was written
  interpreted_at TEXT, decision TEXT);       -- PASS | KILL | INCONCLUSIVE

CREATE TABLE lessons (                       -- cross-run research memory; keyword search only in v1
  id INTEGER PRIMARY KEY, project_id TEXT, scope TEXT NOT NULL,   -- project | global
  text TEXT NOT NULL, tags TEXT NOT NULL, source_run_id TEXT, created_at TEXT NOT NULL);

CREATE TABLE usage_by_provider (             -- materialized per run for fast budget checks
  run_id TEXT NOT NULL, provider TEXT NOT NULL,
  calls INTEGER NOT NULL, input_tokens INTEGER NOT NULL, output_tokens INTEGER NOT NULL,
  cost_usd REAL, PRIMARY KEY (run_id, provider));
```

"What changed afterward" is answered by joining `proposals.revision_of`,
`critiques.target_id`, and `decisions.refs_json`, all of which point back to
`agent_calls.id`.

---

## 8. Orchestration state machine

```python
class Stage(StrEnum):
    INIT = "INIT"; PROPOSE = "PROPOSE"; CRITIQUE = "CRITIQUE"; REVISE = "REVISE"
    IMPLEMENT = "IMPLEMENT"; TEST = "TEST"; FIX = "FIX"; REVIEW = "REVIEW"
    SYNTHESIZE = "SYNTHESIZE"; DONE = "DONE"; HALTED = "HALTED"
```

```
INIT ──► PROPOSE ──► CRITIQUE ──┬─ ACCEPT ─────────────────────► IMPLEMENT ──► TEST ──┬─ pass ──► REVIEW ──► SYNTHESIZE ──► DONE
                       ▲        ├─ REVISE, round < max ─► REVISE ┘                     └─ fail ──► FIX ──► TEST (once) ──► REVIEW …
                       │        ├─ REVISE, round == max ─────────► IMPLEMENT  (open criticisms attached to context; reported as disagreement)
                       └────────┤
                                ├─ REJECT ───────────────────────► SYNTHESIZE  (nothing built; report says why)
                                └─ NEEDS_EVIDENCE / NEEDS_EXPERIMENT ─► SYNTHESIZE + open_questions row  (no researcher in v1)

Any stage: budget exceeded or unrecoverable error ──► HALTED ──► SYNTHESIZE(partial) ──► DONE
```

Transition rules, all recorded in `decisions`:

| From | Condition | To |
|---|---|---|
| PROPOSE | proposal valid | CRITIQUE |
| CRITIQUE | ACCEPT | IMPLEMENT |
| CRITIQUE | REVISE and `round < max_rounds` | REVISE (round += 1) |
| CRITIQUE | REVISE and `round == max_rounds` | IMPLEMENT (flag `unresolved=true`) |
| CRITIQUE | REJECT | SYNTHESIZE |
| CRITIQUE | NEEDS_EVIDENCE / NEEDS_EXPERIMENT | SYNTHESIZE (+ open question) |
| REVISE | revision valid | CRITIQUE |
| IMPLEMENT | files written (path-checked) | TEST |
| TEST | exit 0 | REVIEW |
| TEST | exit ≠ 0 and `fix_round < max_fix_rounds` | FIX (fix_round += 1) |
| TEST | exit ≠ 0 and fix rounds exhausted | REVIEW (validator sees failing tests) |
| FIX | files written | TEST |
| REVIEW | any verdict | SYNTHESIZE (verdict is data for the synthesizer, not a branch) |
| SYNTHESIZE | synthesis valid | DONE |
| any | `budget.exceeded()` | HALTED → SYNTHESIZE with `partial=true` |

Loop body (`pipeline.run`):

```python
while stage not in (DONE,):
    budget.check(store, run_id)                 # raises BudgetExceeded → HALTED
    handler = HANDLERS[stage]                   # one function per stage
    next_stage, reason, refs = handler(ctx)     # builds context pack, calls agent, writes rows
    store.record_decision(run_id, stage, next_stage, reason, refs)
    store.set_stage(run_id, next_stage)
    stage = next_stage
```

Because `runs.stage` is persisted after every transition, `roundtable resume
<run_id>` (M3) restarts at the current stage with no re-billing.

Autonomy gating inside handlers: level 0 stops after CRITIQUE (advice only);
level 1 stops after the final REVISE/ACCEPT; level 2 runs the whole pipeline.
Levels 3–4 are reserved for experiments (M5).

---

## 9. One complete example execution

`roundtable run examples/project.yaml` with the config in §4 and `max_rounds: 3`.

```
[run 9f2c1a] project=dd-demo autonomy=2 budget: 12 calls / 200k tok / $5.00 / 900s
[INIT]      workspace runs/dd-demo/9f2c1a/workspace
[PROPOSE]   proposer  anthropic/<model>  1.9s  in=812 out=441  $0.01
            P1  claim="Track running peak; drawdown = (peak - x)/peak; return max"  conf=0.86
            assumptions: 3  tests_required: 4
[CRITIQUE]  critic    xai/<model>        4.1s  in=1104 out=388  $0.01
            C1 on P1 → REVISE  conf=0.78
              major   "Division by zero when peak == 0 (equity can start at 0 or go negative)"
              minor   "tests_required omits the 'ends at trough' requirement"
[REVISE]    reviser   anthropic/<model>  2.3s  in=1391 out=502  $0.01
            P1r2  revision_of=P1  conf=0.9
              fixed:    peak<=0 → treat drawdown as 0 for that step; document assumption
              fixed:    added test 'ends at trough'
              rejected: none
[CRITIQUE]  critic    xai/<model>        3.7s  in=1522 out=214  $0.01
            C2 on P1r2 → ACCEPT  conf=0.84  checked: zero/negative peak, empty input, monotone input
[IMPLEMENT] engineer  openai/<model>     6.0s  in=1633 out=1210 $0.02
            I1  files: dd.py (23 lines), test_dd.py (31 lines)  known_gaps: 0
[TEST]      python -m pytest -q   0.4s   exit=1   passed=4 failed=1
            FAILED test_dd.py::test_ends_at_trough - assert 0.5 == 0.6
[FIX]       engineer  openai/<model>     5.2s  in=2410 out=980  $0.02
            I1f1  files: dd.py  notes="off-by-one: peak updated after drawdown calc, not before"
[TEST]      python -m pytest -q   0.4s   exit=0   passed=5 failed=0
[REVIEW]    validator ollama/qwen3:30b-a3b  21.8s  in=2044 out=610  cost=n/a
            R1 on I1f1 → ACCEPT  conf=0.7
              req "0.0 for monotone/empty"        ✓  test_dd.py::test_monotone, ::test_empty
              req "ends at trough"                 ✓  test_dd.py::test_ends_at_trough
              req "pure python"                    ✓  no imports besides typing
              minor "no test for a single-element series"
[SYNTHESIZE] synthesizer anthropic/<model> 3.4s  in=3980 out=720 $0.02
            S1 overall=ACCEPT conf=0.82  disagreements: 1 (unresolved: 0)
[DONE]      8 calls, 20,461 tokens, $0.10, 49s.  report: runs/dd-demo/9f2c1a/report.md
```

`report.md` (abridged):

```
# dd-demo — run 9f2c1a

Overall verdict: ACCEPT (confidence 0.82)

## What was built
dd.py: max_drawdown() using a running-peak scan; test_dd.py with 5 tests. All pass.

## Test results
python -m pytest -q → 5 passed (after 1 fix round; first run failed test_ends_at_trough).

## Disagreements
1. Zero/negative peak handling — critic (C1) called it a major bug; reviser treated
   drawdown as 0 for those steps and added the assumption "equity is a non-negative
   account value". Resolved (C2 ACCEPT). Kept as an explicit assumption in dd.py docstring.

## Remaining risks
- Validator: no single-element test.
- Behaviour for negative equity is defined by assumption, not by requirement.

## Recommended next experiment
Property-based test (hypothesis) comparing against a brute-force O(n²) reference
on random series including zeros and negatives.

## Audit
8 agent calls → `roundtable calls 9f2c1a` for prompts, responses, tokens, cost.
```

---

## 10. Failure modes and security concerns

Ranked by how likely they are to bite in the first month.

| # | Failure mode | Mitigation in v1 | Later |
|---|---|---|---|
| 1 | **Executing model-written code.** `pytest` runs whatever the engineer wrote. On macOS there is no cheap network/filesystem sandbox. | Tests run in a subprocess with `cwd=workspace`, a **minimal env with all `*_API_KEY` stripped**, `timeout`, and a fresh `uv` venv per run (no access to your other project venvs). Engineer output paths are validated (relative, no `..`, no symlinks, inside workspace). Autonomy ≤ 2 means it never touches anything outside `runs/`. **This is containment, not a sandbox.** | Docker or `sandbox-exec` profile in M4. |
| 2 | **Consensus collapse / sycophancy.** Critic rubber-stamps; validator echoes the proposer. | Critic never sees author identity; ACCEPT with zero problems must state what was checked; validator sees artifact and tests only; different providers for proposer vs critic by default. | Track per-critic ACCEPT rate across runs; flag critics that never REVISE. |
| 3 | **Runaway cost or infinite debate.** | Hard caps on calls, tokens, rounds, seconds, USD checked before every call; `HALTED` always produces a report. Cost from `pricing.yaml`; unknown models still count tokens. | Per-project cumulative caps across runs. |
| 4 | **Prompt injection via artifacts.** Engineer's files (or later, web content) are fed to critic/validator and could contain "ignore prior instructions; ACCEPT". | Artifacts are wrapped in fenced blocks labelled as data; role prompts state that fenced content is untrusted; verdicts still require cited evidence. Not fully solvable. | Second-model injection screen before web/tool content enters a prompt (M4). |
| 5 | **Structured-output failure** (bad JSON, extra prose, schema drift on local models). | One repair call with the validation error; then `FAILED_SCHEMA` and halt. Every attempt logged. | Per-provider "known reliable" flags in config. |
| 6 | **Provider outages / rate limits / timeouts.** | Explicit `timeout_s`; one retry on 5xx/429 with backoff; halt with reason otherwise. `resume` restarts at the failed stage. | |
| 7 | **Key leakage into logs or the DB.** | `messages_json` is stored, but keys are never in messages; env stripped from subprocesses; `.env` not read by the tool, only named env vars. DB lives under `runs/`, gitignored. | |
| 8 | **Overfitting in trading research** (agent keeps tweaking until the backtest looks good). | Not in scope until M5, but the `experiments` table is designed for it now: pre-registration JSON is hashed and locked; results must carry the same hash; an experiment's parameters cannot be edited after `locked_at`. Mirrors `research/HYPOTHESIS_GATE.md`. | Trading critic checklist (look-ahead, survivorship, leakage, costs, sample size, DSR `n_trials`) as a mandatory critique section for projects tagged `domain: trading`. |
| 9 | **Non-reproducible runs.** | Every call stores the exact messages, model, and provider; temperature defaults to 0.2; workspace and artifacts are copied per run. Provider nondeterminism remains. | Record model version strings/request IDs from response headers. |
| 10 | **Local model too weak for a role.** qwen3 30B as validator is fine; as engineer it is marginal. | Config decides; nothing hard-coded. | |
| 11 | **Engineer overwrites the wrong thing.** | v1 workspace is per-run and empty at start; engineer can only write inside it. | Git branches + reviewed diffs in M4 make "never blindly overwrite" enforceable for real repos. |
| 12 | **Missing API keys** for OpenAI/xAI/Gemini. | Fails at config load with the env var named; you can point every role at `anthropic` or `ollama` until keys exist. | |

---

## Implementation plan

Each milestone is a separate approval point and leaves the tool runnable.

| M | Deliverable | Proves | Size |
|---|---|---|---|
| **M0** | Package skeleton, `pyproject.toml`, `config.py`, `store.py` with the full DDL, `providers/base.py`, all four adapters (`anthropic`, `openai_compat`, `gemini`, `ollama`), `RecordedProvider`, `roundtable call <role> "<prompt>"` smoke command. Unit tests for store and schemas. | Provider interface works against every backend you have a key for, with usage logged to SQLite. | ~500 lines |
| **M1** | `schemas.py`, `agents.py`, prompts for proposer/critic/reviser, `pipeline.py` with `PROPOSE → CRITIQUE → REVISE` loop, `budget.py`, `roundtable run` and `roundtable show`. Full pipeline test on `FakeProvider`. | The critique loop, round limits, and halting behave; logs answer every audit question. | ~450 lines |
| **M2** | `actions.py` (per-run workspace from `repo:` via git worktree or copy, file reads on request, writes with path checks, pytest runner), engineer/validator/synthesizer prompts, remaining stages, `report.py`. **This is your first milestone.** | End-to-end `roundtable run project.yaml` against an existing repo with four providers. Needs `OPENAI_API_KEY`, `XAI_API_KEY`, `GEMINI_API_KEY`. | ~450 lines |
| **M3** | `resume`, retries/backoff, `pricing.yaml`, `roundtable calls <run>` for prompt/response inspection, per-provider budgets, autonomy levels 0–2. | Operability. | ~200 lines |
| **M4** | Git: per-run branch `agent/<role>/<run>`, engineer commits, validator reviews `git diff`, merge only on human approval (or autonomy ≥ 3 later). Docker/`sandbox-exec` test runner. Provider-native tool calling for engineer with role permissions. | Safe use on real repos. | ~500 lines |
| **M5** | Experiments: `preregister` → lock → `run` → `interpret` CLI; trading critic checklist; `domain: trading` project tag; autonomy 3. | Strategy research that can kill a strategy. | ~400 lines |
| **M6** | Planner stage producing a task list; researcher role; parallel proposals; `lessons` retrieval; autonomy 4 with budgets. | The full collaboration protocol from the spec. | ~500 lines |
| later | Read-only web dashboard over the SQLite file. | | |

Decisions I have made that you may want to override:

1. Package and CLI are named `roundtable` (`python -m roundtable run project.yaml`).
2. When critics still say REVISE at `max_rounds`, the pipeline **implements anyway** and reports the unresolved criticisms, rather than halting. Halting is a one-line config change (`on_unresolved: halt`).
3. NEEDS_EVIDENCE / NEEDS_EXPERIMENT end the run in v1 (no researcher yet) and are recorded as open questions.
4. Agents do not call tools in M1–M3; the orchestrator performs the actions their output implies.
5. Sequential execution only; no async, no parallel agents, until M6.

---

## Addendum: patterns to copy from `mttr-correlator` (found via Oracle after the first draft)

`mttr-correlator/src/causeway/llm/provider.py` already solves three things this
design needs. Roundtable stays standalone (no import), but the code is copied,
not re-derived.

| Pattern | What it does there | Use in roundtable |
|---|---|---|
| `RecordedProvider` with prompt guards | Replays canned outputs per role and ordinal, and asserts `expects_prompt_contains` / `forbids_prompt_contains` on every prompt. | Replaces the planned `FakeProvider`. The guards turn the anti-sycophancy rules into tests: the critic recording forbids the proposer's model name in its prompt; the validator recording forbids the proposal's `approach` text. A prompt-construction regression fails the offline pipeline test instead of silently biasing a live run. |
| `evidence_block()` | Wraps untrusted text in an XML-ish block with `id`, `type`, and `trust` attributes so the model reads it as data. | Every artifact, test output, and (later) web result enters a prompt only through this wrapper. This is the concrete form of mitigation #4 in §10. |
| `pricing.py` | `(input, output)` USD per token by model, with a default rate. | Same shape, loaded from `examples/pricing.yaml`. One difference: roundtable stores `NULL` for unknown models rather than a default rate, so the report never shows a made-up dollar figure. |

Also present there and worth keeping in mind for M4: the single `HttpTransport`
seam with a `RecordedTransport` for fully offline tests. Roundtable does not need
it while the three provider SDKs are the only network callers, but it is the
right shape if web research tools arrive.

Milestone M0 changes accordingly: `RecordedProvider` with prompt guards instead
of `FakeProvider`; `evidence_block()` lands in M1 with the first prompts.

---

## Addendum 2: subscription-backed CLI providers (verified 2026-09-25)

You asked whether the system can use your Claude, Codex, and Grok subscriptions
the way you do from the terminal, instead of API keys. It can. Each CLI has a
headless mode that takes a prompt, enforces a JSON schema, and prints a JSON
envelope with usage. I smoke-tested every one with a trivial prompt from a
scratch directory, using only the logins already on this machine.

| CLI | Version | Login | Headless invocation that worked | Structured output | Usage in envelope |
|---|---|---|---|---|---|
| `claude` | 2.1.282 | claude.ai (subscription) | `claude -p "<prompt>" --output-format json --json-schema '<schema>' --tools "" --max-turns 1 --no-session-persistence` | `structured_output` | `usage.*`, `total_cost_usd` (list price, informational) |
| `codex` | 0.155.1 | ChatGPT | `codex exec --json --ephemeral --skip-git-repo-check -C <dir> -s read-only --output-schema schema.json -o last.txt "<prompt>"` | file from `-o`, or `item.completed` event | `turn.completed.usage` |
| `grok` | 1.0.40 | xAI account | `grok --single "<prompt>" --output-format json --json-schema '<schema>' --max-turns 1 --disable-web-search` | `structuredOutput` | `usage.*`, `total_cost_usd` |
| `gemini` | 0.47.0 | Google (individual) | **Refused:** `IneligibleTierError: This client is no longer supported for Gemini Code Assist for individuals … migrate to Antigravity`. | — | — |

Gotchas found:

- Codex strict mode requires `"additionalProperties": false` on every object in
  the schema. The adapter sets it when converting pydantic models.
- Codex reads stdin unless it is redirected; the adapter passes `stdin=DEVNULL`.
- `grok -p` is a top-level flag (`--single`), not a subcommand of `grok agent`.
- Each CLI ships a large coding-agent system prompt (roughly 10k–15k input tokens
  on a trivial call). Claude accepts `--system-prompt` and Grok
  `--system-prompt-override` to replace it; Codex has no override, so the role
  prompt goes in the user turn. This affects subscription quota, not dollars.
- Gemini via subscription is off the table with the installed CLI. Gemini needs
  `GEMINI_API_KEY` and the API adapter, or a different client.

### Design change: two provider kinds, one interface

`Provider.run()` is unchanged. Two families implement it:

```yaml
providers:
  claude_cli: { type: cli, cli: claude }                 # subscription
  codex_cli:  { type: cli, cli: codex }                  # subscription
  grok_cli:   { type: cli, cli: grok }                   # subscription
  anthropic:  { type: api, sdk: anthropic,      api_key_env: ANTHROPIC_API_KEY }
  openai:     { type: api, sdk: openai_compat,  api_key_env: OPENAI_API_KEY }
  xai:        { type: api, sdk: openai_compat,  api_key_env: XAI_API_KEY, base_url: https://api.x.ai/v1 }
  gemini:     { type: api, sdk: gemini,         api_key_env: GEMINI_API_KEY }
  ollama:     { type: api, sdk: ollama,         host: http://localhost:11434 }
```

CLI adapters (`providers/cli/{claude,codex,grok}.py`) build the command line,
write the schema to a temp file where needed, run the subprocess with a timeout
and `stdin=DEVNULL`, and parse the envelope into the same `Completion`. The
`agent_calls` row records the exact argv, and `cost_usd` is stored as the
CLI-reported list price with a `billing: subscription` flag so the report can
show "would have cost $X at list" without claiming you paid it.

### Design change: the engineer runs as a real agent in the worktree

This is the larger win. Roles that only *think* (proposer, critic, reviser,
validator, synthesizer) run in **answer mode**: tools off, one turn, JSON out.
The **engineer** role can instead run in **agent mode**: the CLI is launched
inside the run's git worktree with its own tools enabled, exactly as you do by
hand with `codex --yolo` or `claude --dangerously-skip-permissions`, and the
orchestrator captures the result as a `git diff` plus a short structured
summary.

| Role mode | Command shape | What the orchestrator does afterward |
|---|---|---|
| answer | `claude -p … --tools ""` / `codex exec -s read-only` / `grok --single --max-turns 1` | validate JSON, write rows |
| agent (engineer only) | `claude -p … --permission-mode acceptEdits --add-dir <worktree>` / `codex exec -C <worktree> -s workspace-write --full-auto` / `grok --single --cwd <worktree> --always-approve` | `git add -A && git diff --cached` in the worktree → `Implementation.diff`; run tests; the engineer never touches anything outside the worktree |

Consequences:

- The two-step "FilesRequested" dance from the previous addendum is no longer
  needed when the engineer is a CLI agent; it stays as the fallback for API
  providers and local models.
- Containment improves. Codex `-s workspace-write` uses a real macOS Seatbelt
  sandbox, and Claude Code has its own sandbox; both are stronger than my
  planned bare subprocess. The worktree is still the only writable location,
  and the original repo is untouched until a human merges.
- Role permissions map onto CLI flags: `workspace: write` → agent mode;
  `workspace: read` → `-s read-only` / `--tools ""` / `--disable-web-search`;
  `web: false` → search disabled.
- Budgets still apply. CLI envelopes report tokens; the orchestrator counts
  them against `max_tokens`, and subscriptions have their own rate windows,
  so `HALTED` on a CLI "usage limit" error is a first-class outcome.

### Milestone impact

- **M0** ships the three CLI adapters first (they need no keys) plus `anthropic`
  and `ollama` API adapters. `openai_compat` and `gemini` API adapters remain in
  M0 but are exercised only when keys exist.
- **M2** engineer stage supports agent mode via CLI; answer mode stays for API
  providers.
- The first-milestone demo (Claude proposes, Grok critiques, Claude revises,
  Codex implements, tests run, a local model reviews, Claude synthesizes) is
  runnable today with **no API keys at all**: `claude_cli`, `grok_cli`,
  `codex_cli`, and `ollama`.

### Policy note on subscription use (from the Claude Code docs, checked 2026-09-25)

- Headless `claude -p` and the Agent SDK use the claude.ai login when no
  `ANTHROPIC_API_KEY` is set. This is documented behaviour, not a trick.
- What Anthropic does not allow is third-party developers offering claude.ai
  login or its rate limits inside *their* products to *their* users. A personal
  orchestrator on your own machine, using your own login, is not that.
- Anthropic announced and then paused (June 2026) a plan to bill programmatic
  usage (`-p`, SDK, CI) separately at API rates. It may return with notice. The
  `billing: subscription` flag in `agent_calls` exists so that, if it does, the
  report already shows what each run would have cost at list.
- `total_cost_usd` from the CLI is a client-side estimate, never a bill.
- Subscription limits are a rolling 5-hour window plus weekly caps, shared with
  your interactive use. A run that exhausts them halts with the CLI's error
  recorded as the reason and resumes later with `roundtable resume`.

---

## Status log

### 2026-09-25 — M0 and M1 shipped

- **M0**: config, store with full DDL, provider interface, CLI adapters (claude, codex, grok on
  subscription logins), API adapters, RecordedProvider, `providers` / `call` / `calls`. All four
  live backends verified with one structured call each.
- **M1**: schemas with honesty validators, prompts, context packs with `<evidence>` wrapping,
  budget enforcement, the PROPOSE → CRITIQUE → REVISE state machine, acceptance-criteria lock,
  scope triage (`needs_decomposition`), report, `run` / `runs` / `show`. Eight offline pipeline
  scenarios with prompt guards. 35 tests.
- **First live run** (`run_b81e36eb75`): Claude proposed, Grok found three real defects in the
  acceptance criteria (a degenerate test oracle, a NaN-becomes-peak bug in the sketched control
  flow, an int-vs-float check gap), Claude fixed all three, Grok's second critique timed out at
  300 s. The synthesizer reported the halt honestly and listed P2 as unreviewed. This is the
  intended behaviour for a provider failure.

Lessons folded in:

- Grok as critic reasons for minutes (224 s, 15.6k output tokens on one critique). Added a
  per-agent `effort: low|medium|high` (maps to `grok --reasoning-effort` and codex
  `model_reasoning_effort`) and raised the critic timeout to 600 s in the example config.
- The synthesizer gets exactly one call even after the budget trips. A halted run without a
  report is worse than one call over budget. If the synthesizer's own provider is the one at
  its usage limit, the fact-only report is written instead.
- Two adjacent decisions from the questions Kevin asked before M1: (1) "solved" is defined by
  four observable checks (criteria satisfied with cited evidence, tests executed and passed,
  no open blocker, synthesizer ACCEPT with disagreements listed); (2) the orchestrator, not a
  model, takes point; within a stage one role owns the output and the critic can only block.
- Grok with `--max-turns 1` returned an empty reply with no structured output on long critique
  prompts (three of three first attempts in run 3). With `max_turns: 3` (new `ProviderCfg`
  field) the first attempt succeeded on 1 of 2 critiques in run 4 and the repair round caught
  the other. The adapter now returns unparsed replies as a `Completion` so the audit log keeps
  what the model actually wrote; the Agent decides that it is a schema failure.
- Answer-mode agents mentioned their random scratch-directory path in proposals; the common
  prompt now tells them to ignore the working directory.
- **Run 4 (`run_05481fd76b`) completed the whole M1 loop**: P1 → C1 REVISE (1 blocker: raising
  on non-positive equity violated requirement 1) → P2 → C2 ACCEPT → criteria AC1–AC8 locked →
  IMPLEMENT (stub) → report. Four disagreements preserved, one resolved, three flagged as
  proposer choices the requirements do not pin down. 6 calls, 12 minutes, all on subscriptions.

### 2026-09-25 — M2 shipped

- **M2**: `actions.py` (per-run workspace as git worktree / copy / greenfield, cache exclusion,
  path-checked writes, commit on the run branch, test runner with stripped env and timeout),
  engineer in agent mode (CLI inside the worktree) or answer mode (files as JSON), TEST / FIX /
  REVIEW stages, validator prompt and pack, report sections for implementation, tests and review,
  per-role `fallback` provider, `effort`, `mode`, `max_turns`, docs (GUIDE, CONFIG, PROVIDERS),
  landing page under `site/`. 51 tests.
- **Run 7 (`run_53f50047d6`) completed the whole pipeline live**: P1 → C1 REVISE → P2 → C2 REVISE
  (blocker) → P3 → C3 REVISE → P4 → C4 ACCEPT → AC1–AC7 locked → Claude (agent mode) wrote
  dd.py + test_dd.py, commit on `roundtable/run_53f50047d6` → orchestrator ran pytest: 20 passed →
  qwen3 validator 7/7 with cited tests → synthesizer ACCEPT with two unresolved disagreements
  preserved. 12 calls, 25 minutes, all on subscriptions.
- C3 is the run's best moment: every fixture in the accepted criteria also passed a wrong
  (reset-on-uptick) implementation, and Grok said so. The revision added discriminating fixtures.

Lessons:

- Codex agent mode: `codex exec` has no `--full-auto`; `-s workspace-write` alone is the sandboxed
  non-interactive mode. Codex also began returning 401 on its ChatGPT session mid-session; the
  engineer seat was pointed at Claude via `ROUNDTABLE_ENGINEER_PROVIDER`. Roles as config paid off.
- Grok on long prompts (revised proposals) sometimes spends 10k–17k reasoning tokens and then
  reports `stopReason: cancelled` with empty text, at any effort. Added per-role `fallback`.
  Grok also needs `--max-turns > 1`. Its critiques remain the best of the three.
- A Codex "error" event followed by a reconnect is not a failure; only a turn that never
  completes is. The adapter now judges by `turn.completed`.
- Agents leave `__pycache__` and `.pytest_cache` in the worktree; excluded via `info/exclude`.
- Answer-mode agents mention their scratch cwd unless told to ignore it.
- A clean four-round run uses exactly 12 calls; example `max_calls` raised to 16.
