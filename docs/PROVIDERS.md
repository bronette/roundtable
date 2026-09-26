# Providers

Every backend implements one method:

```python
def run(self, messages, *, schema=None, temperature=0.2, max_tokens=4096, timeout_s=180.0,
        effort=None, workspace=None, max_turns=None) -> Completion
```

`messages` is a list of system/user/assistant turns. `schema` is a pydantic model; when given,
the backend must return a JSON object and the orchestrator validates it (one repair round on
failure). `workspace` switches CLI backends into agent mode inside that directory. The return
value carries text, parsed JSON, token usage, model, latency, the provider's own cost estimate
if any, and for CLIs the exact command line, all of which land in the `agent_calls` table.

Adapters know nothing about roles, prompts, budgets, or the database.

## CLI providers (subscription logins)

Verified 2026-09-25 on macOS with the versions shown. These use whatever login the CLI already
has; `*_API_KEY` variables are removed from the child environment so the CLI cannot silently
bill the API instead. Answer-mode calls run in an empty temp directory so no `CLAUDE.md` or
`AGENTS.md` leaks in; agent-mode calls run in the worktree, where those files are wanted.

### claude (Claude Code 2.1.x)

Answer mode:
```
claude -p "<prompt>" --output-format json --json-schema '<schema>' --tools "" --max-turns 1 \
       --no-session-persistence --strict-mcp-config [--system-prompt "<system>"] [--model <m>]
```
Agent mode replaces `--tools "" --max-turns 1` with
`--max-turns <n> --permission-mode acceptEdits --allowedTools Read,Edit,Write,MultiEdit,Glob,Grep,LS,Bash(python:*),Bash(python3:*),Bash(pytest:*),Bash(uv run:*),Bash(ls:*),Bash(cat:*)`.

Envelope fields used: `structured_output`, `result`, `usage.{input_tokens, output_tokens,
cache_creation_input_tokens, cache_read_input_tokens}`, `total_cost_usd` (client-side estimate),
`modelUsage` (model name), `session_id`, `is_error`, `subtype`.

Notes: `--system-prompt` replaces the CLI's default (~10k tokens). The claude.ai login is used
when no API key is present; that is documented behaviour. Anthropic paused (June 2026) a plan
to bill programmatic use separately; if it returns, the `billing` column already records which
calls were subscription calls.

### codex (codex-cli 0.155.x)

Answer mode:
```
codex exec --json --ephemeral --skip-git-repo-check -C <tmp> -s read-only \
      --output-schema <tmp>/schema.json -o <tmp>/last.txt [-m <m>] "<prompt>"    < /dev/null
```
Agent mode: `-C <worktree> -s workspace-write` (macOS Seatbelt sandbox; writes only
under the worktree).

Events used (NDJSON): `thread.started`, `item.completed` with `agent_message`,
`turn.completed.usage`, `error`, `turn.failed`. The final message is read from the `-o` file.

Gotchas: strict schema mode requires `additionalProperties: false` and every property in
`required` on every object, and rejects `default`; the adapter rewrites pydantic schemas
accordingly. stdin must be redirected or the CLI waits on it. There is no system-prompt flag;
the system text is prepended to the user prompt. Events never name the model; the adapter reads
`model = ` from `~/.codex/config.toml` when set. `effort` maps to `-c model_reasoning_effort=`.

### grok (Grok Build 1.0.x)

Answer mode:
```
grok --single "<prompt>" --output-format json --json-schema '<schema>' --max-turns <n> \
     --disable-web-search --cwd <tmp> [--system-prompt-override "<system>"] [-m <m>] [--reasoning-effort <e>]
```
Agent mode: `--cwd <worktree> --always-approve --max-turns <n>`. Grok has no sandbox.

Envelope fields used: `structuredOutput`, `text`, `stopReason`, `usage.{input_tokens,
output_tokens, cache_read_input_tokens, reasoning_tokens}`, `total_cost_usd`, `modelUsage`,
`requestId`, `structuredOutputError`.

Gotchas: `-p` is a top-level alias of `--single`, not a subcommand of `grok agent`. With
`--max-turns 1` on long prompts the model returns empty text and no structured output; the
provider default is 3 turns. When Grok exits 1 with `structuredOutputError`, the adapter still
returns the text so the audit log keeps it and the repair round runs. On long prompts Grok can
spend 10k+ reasoning tokens and then report `stopReason: cancelled` with empty text, at any
`--reasoning-effort`; configure a `fallback` provider for seats that use it. Grok is the
slowest backend as a critic (2 to 4 minutes, 10k to 15k output tokens) and the most thorough.

### agy (Google Antigravity CLI 1.2.x)

Google's replacement for the Gemini CLI for individuals. Uses the Google login (Google AI Pro
or Ultra); no key. `agy models` lists Gemini 3.x flash and pro at fixed effort levels, plus
Claude and GPT-OSS models routed through Google.

Answer mode:
```
agy --print "<system + prompt>" --output-format json --json-schema '<schema>' --mode plan \
    --print-timeout <n>s [--model <m>] [--effort low|medium|high|max]
```
Agent mode: `--dangerously-skip-permissions --sandbox` with cwd set to the worktree (agy's
sandbox applies terminal restrictions).

Envelope fields used: `structured_output`, `response`, `status`, `usage.{input_tokens,
output_tokens, thinking_tokens, cache_read_tokens}`, `conversation_id`, `denied_actions`.

Gotchas: no system-prompt flag, so the system text is prepended to the prompt. In plan mode a
model that decides to run a command or read a file is auto-denied headlessly and returns an
empty response; the adapter prepends a "tool use is disabled" notice in answer mode, which was
enough in testing (the general "do not read or write files" line alone was not). Verified
2026-09-25: answer mode 16 s on gemini-3.8-flash-medium; agent mode wrote and tested two files
in a worktree in 51 s. The baseline system prompt is very large:
34k input tokens with a low-effort flash model and 75k with the default model on a trivial call,
so budget by calls. Settings live in `~/.gemini/antigravity-cli/settings.json`.

### gemini (Gemini CLI 0.61.x)

The Gemini CLI refuses the individual Google login (`IneligibleTierError`, "migrate to
Antigravity"), verified on 0.47 and 0.61. It works with an API key instead: create one at
https://aistudio.google.com/apikey (free tier available) and `export GEMINI_API_KEY=...`. The
adapter keeps that one variable in the child environment and strips the other vendors' keys.

Answer mode:
```
gemini -p "<system + schema + prompt>" -o json --approval-mode plan -m <model>
    GEMINI_CLI_TRUST_WORKSPACE=true  GEMINI_CLI_HOME=<per-call temp dir>
```
The per-call home holds a `.gemini/settings.json` selecting `gemini-api-key` auth. This is
needed because `~/.gemini/settings.json` typically still selects the retired personal login and
the CLI ignores `GEMINI_DEFAULT_AUTH_TYPE` once a type is saved. Verified 2026-09-25 on 0.61:
a trivial call answered in 2.9 s on `gemini-3.8-flash` (the `gemini-flash-latest` alias) with a
7.8k-token baseline system prompt.
Agent mode: `--approval-mode yolo` with cwd set to the worktree. There is no sandbox flag in
headless mode beyond the worktree boundary.

The CLI has no schema or system-prompt flags; both ride in the prompt and the orchestrator
parses and validates the reply with its usual repair round. Envelope: `response` (text) and
`stats.models.<model>.tokens.{prompt, candidates, cached, thoughts}`. The same key also drives
the `gemini` API provider, which does support native JSON-schema output and is the better
choice for answer-mode seats; use the CLI for the engineer seat when you want Gemini editing
files itself.

## API providers

| sdk | package | structured output | usage fields |
|---|---|---|---|
| `anthropic` | `anthropic` | one forced tool call whose `input_schema` is the schema | `input_tokens`, `output_tokens`, cache fields |
| `openai_compat` | `openai` | `response_format: json_schema, strict: true`; xAI via `base_url` | `prompt_tokens`, `completion_tokens`, cached |
| `gemini` | `google-genai` | `response_mime_type: application/json` + `response_json_schema` | `usage_metadata` |

Gemini API notes: the default model is the `gemini-flash-latest` alias because concrete names
retire (`gemini-2.5-flash` now returns 404 for new keys). On the free tier the pro models return
429 quota errors (surfaced as `UsageLimitError`) and the flash models occasionally return
503 "high demand" (a `ProviderError`); `gemini-flash-lite-latest` answered in under a second
when the others were busy. Give Gemini seats a `fallback` or use the lite model for cheap roles.
| `ollama` | `ollama` | `format=<schema>` on `chat()` | `prompt_eval_count`, `eval_count` |

API providers ignore `workspace`; an engineer on an API provider runs in answer mode. Do not
pass `think=False` to qwen3 on Ollama: its chain of thought leaks into the JSON fields.

## Pricing

`pricing.yaml` maps model names to USD per million tokens (`input`, `output`, optional
`cached_input`). Lookup is exact, then longest prefix. Unknown models get `cost_usd = NULL` and
the run's total shows "partly unknown". The vendor-reported estimate from CLI envelopes is
stored separately as `reported_cost_usd`.

## Adding a provider

1. Add a class with the `run` signature above in `roundtable/providers/` (or `providers/cli/`).
2. Return a `Completion`; set `parsed` to the JSON object if the backend produced one, else
   leave it `None` and the orchestrator will try to parse `text` and run the repair round.
3. Register it in `roundtable/providers/__init__.py` and add the `sdk` or `cli` literal to
   `ProviderCfg` in `config.py`.
4. Add an envelope test in `tests/test_cli_adapters.py` using a recorded response.

No workflow code changes.

## The recorded provider

`RecordedProvider` replays canned outputs in order and asserts prompt guards
(`expects_prompt_contains`, `forbids_prompt_contains`) on every call. The pipeline tests use it
to make the anti-sycophancy rules executable: the critic recording forbids the proposer's
provider name in its prompt; the validator recording forbids the proposal's approach text. A
prompt-construction regression fails the offline suite instead of quietly biasing a live run.
