"""Google Antigravity CLI (`agy`) headless mode, using the Google login (Google AI Pro/Ultra).

Verified 2026-09-25 with agy 1.2.11:
  agy --print "<prompt>" --output-format json --json-schema '<schema>' --mode plan [--model m] [--effort e] --print-timeout Ns
Envelope: conversation_id, status ("SUCCESS"), response (text), structured_output (object, when a schema
was given), num_turns, duration_seconds, usage{input_tokens, output_tokens, thinking_tokens,
cache_read_tokens, total_tokens}, denied_actions[{action, display_name}].
No system-prompt flag: the system text is prepended to the prompt. The baseline system prompt is
large (34k–75k input tokens per call). Models include Gemini 3.x flash/pro at fixed effort levels,
and Claude and GPT-OSS models routed through Antigravity; `agy models` lists them.
"""

from __future__ import annotations

import json

from pydantic import BaseModel

from roundtable.providers.base import Completion, Message, Usage, json_schema_for, parse_json_loose, split_messages
from roundtable.providers.cli import common
from roundtable.providers.cli.claude import _redact


class AntigravityCLI:
    def __init__(self, name: str, *, binary: str = "agy", model: str | None = None,
                 use_api_key: bool = False, extra_args: list[str] | None = None, max_turns: int = 3):
        self.name = name
        self.binary = common.require_binary(binary)
        self.model = model
        self.use_api_key = use_api_key
        self.extra_args = list(extra_args or [])

    def run(self, messages: list[Message], *, schema: type[BaseModel] | None = None,
            temperature: float = 0.2, max_tokens: int = 4096, timeout_s: float = 180.0,
            effort: str | None = None, workspace: str | None = None, max_turns: int | None = None) -> Completion:
        system, prompt = split_messages(messages)
        if not workspace:
            # plan mode auto-denies every tool call headlessly and then returns an empty response,
            # so the model must not try to compute with a shell or read anything.
            prompt = ("Tool use is DISABLED for this request. Any tool call (running a command, reading or "
                      "writing a file, searching) is automatically denied and the request fails with no answer. "
                      "Reason in your head and reply directly.\n\n" + prompt)
        full_prompt = f"SYSTEM INSTRUCTIONS:\n{system}\n\nTASK:\n{prompt}" if system else prompt
        with common.answer_dir() as tmp:
            cwd = workspace or tmp
            argv = [self.binary, "--print", full_prompt, "--output-format", "json",
                    "--print-timeout", f"{max(30, int(timeout_s) - 10)}s"]
            if workspace:
                # agent mode: edits and shell auto-approved inside the worktree, with agy's terminal sandbox
                argv += ["--dangerously-skip-permissions", "--sandbox"]
            else:
                argv += ["--mode", "plan"]   # read-only; the prompt also says not to use tools
            if schema is not None:
                argv += ["--json-schema", json.dumps(json_schema_for(schema))]
            if self.model:
                argv += ["--model", self.model]
            if effort:
                argv += ["--effort", effort]
            argv += self.extra_args
            rc, out, err, ms = common.run_argv(
                argv, cwd=cwd, env=common.scrubbed_env(keep_api_keys=self.use_api_key), timeout_s=timeout_s)
        try:
            data = common.last_json_object(out)
        except Exception:  # noqa: BLE001
            data = None
        if data is None or (rc != 0 and "response" not in data):
            common.raise_for_failure("agy", rc, out, err)
        if data.get("status") not in (None, "SUCCESS"):
            common.raise_for_failure("agy", rc, json.dumps({k: data.get(k) for k in ("status", "error", "denied_actions")}), err)
        u = data.get("usage") or {}
        text = data.get("response") or ""
        parsed = data.get("structured_output")
        if parsed is None and schema is not None:
            try:
                parsed = parse_json_loose(text)
            except Exception:  # noqa: BLE001
                parsed = None   # the Agent logs the reply and runs the repair round
        return Completion(
            text=text if parsed is None else json.dumps(parsed), parsed=parsed,
            usage=Usage(input_tokens=int(u.get("input_tokens", 0)), output_tokens=int(u.get("output_tokens", 0)),
                        cached_input_tokens=int(u.get("cache_read_tokens", 0)),
                        reasoning_tokens=int(u.get("thinking_tokens", 0))),
            provider=self.name, model=self.model or "antigravity-default", latency_ms=ms,
            request_id=data.get("conversation_id"), stop_reason=data.get("status"),
            raw={k: data.get(k) for k in ("status", "num_turns", "duration_seconds", "denied_actions")}, argv=_redact(argv),
        )
