"""Google Gemini CLI headless mode.

The personal Google login is no longer accepted by this client (IneligibleTierError, verified with
0.47 and 0.61 on 2026-09-25), so this adapter keeps GEMINI_API_KEY in the child environment and the
CLI authenticates with it. Get a key at https://aistudio.google.com/apikey (free tier available).

  gemini -p "<prompt>" -o json --approval-mode plan|yolo [-m <model>]     GEMINI_CLI_TRUST_WORKSPACE=true
Envelope (json output): {"response": "<text>", "stats": {"models": {"<model>": {"tokens": {"prompt": n, "candidates": n, "cached": n, "thoughts": n, "total": n}}}}}
The CLI has no schema flag; the system prompt and a JSON instruction ride in the prompt and the
orchestrator parses and validates the reply, with its usual repair round.
"""

from __future__ import annotations

import json
import os

from pydantic import BaseModel

from roundtable.providers.base import Completion, Message, ProviderUnavailable, Usage, json_schema_for, parse_json_loose, split_messages
from roundtable.providers.cli import common
from roundtable.providers.cli.claude import _redact


class GeminiCLI:
    def __init__(self, name: str, *, binary: str = "gemini", model: str | None = None,
                 use_api_key: bool = True, extra_args: list[str] | None = None, max_turns: int = 3):
        self.name = name
        self.binary = common.require_binary(binary)
        self.model = model
        self.extra_args = list(extra_args or [])
        if not (os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")):
            raise ProviderUnavailable(f"{name}: GEMINI_API_KEY is not set (the Gemini CLI no longer accepts personal logins)")

    def run(self, messages: list[Message], *, schema: type[BaseModel] | None = None,
            temperature: float = 0.2, max_tokens: int = 4096, timeout_s: float = 180.0,
            effort: str | None = None, workspace: str | None = None, max_turns: int | None = None,
            readonly: bool = False) -> Completion:
        system, prompt = split_messages(messages)
        parts = []
        if system:
            parts.append(f"SYSTEM INSTRUCTIONS:\n{system}")
        if schema is not None:
            parts.append("Reply with ONLY a JSON object (no prose, no code fence) matching this JSON Schema:\n"
                         + json.dumps(json_schema_for(schema)))
        parts.append(f"TASK:\n{prompt}")
        full_prompt = "\n\n".join(parts)
        env = common.scrubbed_env(keep_api_keys=False)
        for k in ("GEMINI_API_KEY", "GOOGLE_API_KEY"):
            if os.environ.get(k):
                env[k] = os.environ[k]
        env["GEMINI_CLI_TRUST_WORKSPACE"] = "true"
        with common.answer_dir() as tmp:
            # The user's ~/.gemini/settings.json may still select the retired personal login, and the CLI
            # ignores GEMINI_DEFAULT_AUTH_TYPE once a type is saved. Point the CLI at an isolated home whose
            # settings select API-key auth; it is created per call and discarded with the temp dir.
            home = os.path.join(tmp, "gemini-home")
            os.makedirs(os.path.join(home, ".gemini"), exist_ok=True)
            with open(os.path.join(home, ".gemini", "settings.json"), "w") as f:
                json.dump({"security": {"auth": {"selectedType": "gemini-api-key"}}}, f)
            env["GEMINI_CLI_HOME"] = home
            cwd = workspace or tmp
            argv = [self.binary, "-p", full_prompt, "-o", "json", "--approval-mode", "yolo" if (workspace and not readonly) else "plan",
                    "-m", self.model or "gemini-flash-latest"]
            argv += self.extra_args
            rc, out, err, ms = common.run_argv(argv, cwd=cwd, env=env, timeout_s=timeout_s)
        try:
            data = common.last_json_object(out)
        except Exception:  # noqa: BLE001
            data = None
        if rc != 0 and not data:
            common.raise_for_failure("gemini", rc, out, err)
        if data is None:
            common.raise_for_failure("gemini", rc, out, err)
        text = data.get("response") or ""
        models = ((data.get("stats") or {}).get("models") or {})
        model_used = next(iter(models), self.model or "gemini")
        tok = (models.get(model_used) or {}).get("tokens") or {}
        parsed = None
        if schema is not None:
            try:
                parsed = parse_json_loose(text)
            except Exception:  # noqa: BLE001
                parsed = None
        return Completion(
            text=text, parsed=parsed,
            usage=Usage(input_tokens=int(tok.get("prompt", 0)), output_tokens=int(tok.get("candidates", 0)),
                        cached_input_tokens=int(tok.get("cached", 0)), reasoning_tokens=int(tok.get("thoughts", 0))),
            provider=self.name, model=model_used, latency_ms=ms, stop_reason="end_turn",
            raw={"stats": data.get("stats"), "error": data.get("error")}, argv=_redact(argv),
        )
