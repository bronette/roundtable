"""xAI Grok CLI headless mode, using the xAI account login by default.

Verified 2026-09-25 with grok 1.0.40:
  grok --single "<prompt>" --output-format json --json-schema '<schema>' --max-turns 1 --disable-web-search
Envelope: text, structuredOutput, stopReason, sessionId, requestId, usage{input_tokens,output_tokens,
cache_read_input_tokens,cache_creation_input_tokens,reasoning_tokens}, total_cost_usd, modelUsage{<model>}.
"""

from __future__ import annotations

import json

from pydantic import BaseModel

from roundtable.providers.base import Completion, Message, ProviderError, SchemaError, Usage, json_schema_for, parse_json_loose, split_messages
from roundtable.providers.cli import common
from roundtable.providers.cli.claude import _redact


class GrokCLI:
    def __init__(self, name: str, *, binary: str = "grok", model: str | None = None,
                 use_api_key: bool = False, extra_args: list[str] | None = None, max_turns: int = 3):
        self.name = name
        self.max_turns = max_turns
        self.binary = common.require_binary(binary)
        self.model = model
        self.use_api_key = use_api_key
        self.extra_args = list(extra_args or [])

    def run(self, messages: list[Message], *, schema: type[BaseModel] | None = None,
            temperature: float = 0.2, max_tokens: int = 4096, timeout_s: float = 180.0,
            effort: str | None = None, workspace: str | None = None, max_turns: int | None = None,
            readonly: bool = False) -> Completion:
        system, prompt = split_messages(messages)
        if readonly:
            workspace = None   # grok has no read-only tool mode; fall back to answer mode
        with common.answer_dir() as tmp:
            cwd = workspace or tmp
            # max_turns > 1: grok spends a turn reasoning before it emits structured output on long
            # tasks; with --max-turns 1 the envelope comes back with empty text and no structuredOutput.
            turns = (max_turns or 40) if workspace else self.max_turns
            argv = [self.binary, "--single", prompt, "--output-format", "json", "--max-turns", str(turns),
                    "--disable-web-search", "--cwd", cwd]
            if workspace:
                argv += ["--always-approve"]   # no sandbox in grok: containment is the worktree + the prompt
            if schema is not None:
                argv += ["--json-schema", json.dumps(json_schema_for(schema))]
            if system:
                argv += ["--system-prompt-override", system]
            if self.model:
                argv += ["-m", self.model]
            if effort:
                argv += ["--reasoning-effort", effort]
            argv += self.extra_args
            rc, out, err, ms = common.run_argv(
                argv, cwd=cwd, env=common.scrubbed_env(keep_api_keys=self.use_api_key), timeout_s=timeout_s)
        # grok exits 1 when the model wrote prose instead of the requested JSON, but still prints
        # the full envelope. That is a schema failure (repairable), not a provider failure.
        try:
            data = common.last_json_object(out)
        except ProviderError:
            data = None
        if data is None or (rc != 0 and "text" not in data):
            common.raise_for_failure("grok", rc, out, err)
        u = data.get("usage") or {}
        model_used = next(iter(data.get("modelUsage") or {}), self.model or "grok")
        text = data.get("text") or ""
        parsed = data.get("structuredOutput")
        if parsed is None and schema is not None:
            try:
                parsed = parse_json_loose(text)
            except SchemaError:
                parsed = None  # the Agent records the reply and runs the repair round
        return Completion(
            text=text if parsed is None else json.dumps(parsed), parsed=parsed,
            usage=Usage(
                input_tokens=int(u.get("input_tokens", 0)),
                output_tokens=int(u.get("output_tokens", 0)),
                cached_input_tokens=int(u.get("cache_read_input_tokens", 0)),
                reasoning_tokens=int(u.get("reasoning_tokens", 0)),
            ),
            provider=self.name, model=model_used, latency_ms=ms,
            request_id=data.get("requestId") or data.get("sessionId"), stop_reason=data.get("stopReason"),
            reported_cost_usd=data.get("total_cost_usd"), raw=data, argv=_redact(argv),
        )
