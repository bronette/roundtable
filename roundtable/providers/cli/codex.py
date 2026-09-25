"""OpenAI Codex CLI headless mode, using the ChatGPT login by default.

Verified 2026-09-25 with codex-cli 0.155.1:
  codex exec --json --ephemeral --skip-git-repo-check -C <dir> -s read-only \
      --output-schema schema.json -o last.txt "<prompt>"
Events (NDJSON): thread.started, turn.started, item.completed{agent_message}, turn.completed{usage}, error, turn.failed.
Strict mode: every object needs additionalProperties=false and all properties required.
Codex has no system-prompt override; the system text is prepended to the user prompt.
"""

from __future__ import annotations

import json
import os

from pydantic import BaseModel

from roundtable.providers.base import Completion, Message, ProviderError, Usage, json_schema_for, parse_json_loose, split_messages
from roundtable.providers.cli import common
from roundtable.providers.cli.claude import _redact


class CodexCLI:
    def __init__(self, name: str, *, binary: str = "codex", model: str | None = None,
                 use_api_key: bool = False, extra_args: list[str] | None = None):
        self.name = name
        self.binary = common.require_binary(binary)
        self.model = model or _configured_default_model()
        self.use_api_key = use_api_key
        self.extra_args = list(extra_args or [])

    def run(self, messages: list[Message], *, schema: type[BaseModel] | None = None,
            temperature: float = 0.2, max_tokens: int = 4096, timeout_s: float = 180.0,
            effort: str | None = None) -> Completion:
        system, prompt = split_messages(messages)
        full_prompt = f"SYSTEM INSTRUCTIONS:\n{system}\n\nTASK:\n{prompt}" if system else prompt
        with common.answer_dir() as cwd:
            argv = [self.binary, "exec", "--json", "--ephemeral", "--skip-git-repo-check",
                    "-C", cwd, "-s", "read-only", "-o", os.path.join(cwd, "last.txt")]
            if schema is not None:
                schema_path = os.path.join(cwd, "schema.json")
                with open(schema_path, "w") as f:
                    json.dump(json_schema_for(schema, strict=True), f)
                argv += ["--output-schema", schema_path]
            if self.model:
                argv += ["-m", self.model]
            if effort:
                argv += ["-c", f'model_reasoning_effort="{effort}"']
            argv += self.extra_args
            argv.append(full_prompt)
            rc, out, err, ms = common.run_argv(
                argv, cwd=cwd, env=common.scrubbed_env(keep_api_keys=self.use_api_key), timeout_s=timeout_s)
            last = ""
            try:
                with open(os.path.join(cwd, "last.txt")) as f:
                    last = f.read()
            except FileNotFoundError:
                pass
        events = common.ndjson(out)
        for e in events:
            if e.get("type") in ("error", "turn.failed"):
                msg = e.get("message") or json.dumps(e.get("error"))
                common.raise_for_failure("codex", rc, msg, err)
        if rc != 0:
            common.raise_for_failure("codex", rc, out, err)
        usage = {}
        text = last
        for e in events:
            if e.get("type") == "turn.completed":
                usage = e.get("usage") or {}
            if e.get("type") == "item.completed" and (e.get("item") or {}).get("type") == "agent_message":
                text = text or e["item"].get("text", "")
        parsed = parse_json_loose(text) if schema is not None else None
        return Completion(
            text=text, parsed=parsed,
            usage=Usage(
                input_tokens=int(usage.get("input_tokens", 0)),
                output_tokens=int(usage.get("output_tokens", 0)),
                cached_input_tokens=int(usage.get("cached_input_tokens", 0)),
                reasoning_tokens=int(usage.get("reasoning_output_tokens", 0)),
            ),
            provider=self.name, model=self.model or "codex-default", latency_ms=ms,
            request_id=next((e.get("thread_id") for e in events if e.get("type") == "thread.started"), None),
            stop_reason="end_turn", raw={"events": events[-6:]}, argv=_redact(argv[:-1]) + ["<prompt>"],
        )


def _configured_default_model() -> str | None:
    """Codex events do not name the model; read the user's default from ~/.codex/config.toml."""
    import re
    try:
        with open(os.path.expanduser("~/.codex/config.toml")) as f:
            m = re.search(r'^\s*model\s*=\s*"([^"]+)"', f.read(), re.M)
        return m.group(1) if m else None
    except OSError:
        return None
