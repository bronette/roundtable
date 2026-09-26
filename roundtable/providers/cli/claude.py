"""Claude Code headless mode, using the claude.ai login by default.

Verified 2026-09-25 with claude 2.1.282:
  claude -p "<prompt>" --output-format json --json-schema '<schema>' --tools "" --max-turns 1
Envelope: result, structured_output, usage{input_tokens,output_tokens,cache_*}, total_cost_usd,
modelUsage{<model>: ...}, session_id, is_error, subtype.
"""

from __future__ import annotations

import json

from pydantic import BaseModel

from roundtable.providers.base import Completion, Message, ProviderError, Usage, json_schema_for, parse_json_loose, split_messages
from roundtable.providers.cli import common


AGENT_TOOLS = ["Read", "Edit", "Write", "MultiEdit", "Glob", "Grep", "LS",
               "Bash(python:*)", "Bash(python3:*)", "Bash(pytest:*)", "Bash(uv run:*)", "Bash(ls:*)", "Bash(cat:*)"]


class ClaudeCLI:
    def __init__(self, name: str, *, binary: str = "claude", model: str | None = None,
                 use_api_key: bool = False, extra_args: list[str] | None = None):
        self.name = name
        self.binary = common.require_binary(binary)
        self.model = model
        self.use_api_key = use_api_key
        self.extra_args = list(extra_args or [])

    def run(self, messages: list[Message], *, schema: type[BaseModel] | None = None,
            temperature: float = 0.2, max_tokens: int = 4096, timeout_s: float = 180.0,
            effort: str | None = None, workspace: str | None = None, max_turns: int | None = None) -> Completion:
        system, prompt = split_messages(messages)
        argv = [self.binary, "-p", prompt, "--output-format", "json", "--no-session-persistence", "--strict-mcp-config"]
        if workspace:
            # agent mode: edits auto-accepted inside the worktree; shell limited to test/python commands
            argv += ["--max-turns", str(max_turns or 40), "--permission-mode", "acceptEdits",
                     "--allowedTools", ",".join(AGENT_TOOLS)]
        else:
            argv += ["--tools", "", "--max-turns", "1"]
        if schema is not None:
            argv += ["--json-schema", json.dumps(json_schema_for(schema))]
        if system:
            argv += ["--system-prompt", system]
        if self.model:
            argv += ["--model", self.model]
        argv += self.extra_args
        env = common.scrubbed_env(keep_api_keys=self.use_api_key)
        if workspace:
            rc, out, err, ms = common.run_argv(argv, cwd=workspace, env=env, timeout_s=timeout_s)
        else:
            with common.answer_dir() as cwd:
                rc, out, err, ms = common.run_argv(argv, cwd=cwd, env=env, timeout_s=timeout_s)
        if rc != 0:
            common.raise_for_failure("claude", rc, out, err)
        data = common.last_json_object(out)
        if data.get("is_error") or data.get("subtype") not in (None, "success"):
            common.raise_for_failure("claude", rc, out, err)
        u = data.get("usage") or {}
        model_used = next(iter(data.get("modelUsage") or {}), self.model or "claude")
        text = data.get("result") or ""
        parsed = data.get("structured_output")
        if parsed is None and schema is not None:
            parsed = parse_json_loose(text)
        return Completion(
            text=text if parsed is None else json.dumps(parsed), parsed=parsed,
            usage=Usage(
                input_tokens=int(u.get("input_tokens", 0)) + int(u.get("cache_creation_input_tokens", 0)),
                output_tokens=int(u.get("output_tokens", 0)),
                cached_input_tokens=int(u.get("cache_read_input_tokens", 0)),
                reasoning_tokens=int((u.get("output_tokens_details") or {}).get("thinking_tokens", 0)),
            ),
            provider=self.name, model=model_used, latency_ms=ms,
            request_id=data.get("session_id"), stop_reason=data.get("stop_reason"),
            reported_cost_usd=data.get("total_cost_usd"), raw=data, argv=_redact(argv),
        )


def _redact(argv: list[str]) -> list[str]:
    """Keep the exact command but truncate the prompt so the audit row stays readable;
    the full prompt is stored separately in messages_json."""
    out = []
    skip = False
    for i, a in enumerate(argv):
        if skip:
            out.append(a[:120] + ("…" if len(a) > 120 else ""))
            skip = False
            continue
        out.append(a)
        if a in ("-p", "--print", "--system-prompt", "--single", "--system-prompt-override", "--json-schema"):
            skip = True
    return out
