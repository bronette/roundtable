"""Replays canned outputs and asserts prompt-construction guards. Drives the whole
pipeline offline. Pattern copied from mttr-correlator's RecordedProvider."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel

from roundtable.providers.base import Completion, Message, ProviderError, Usage


class PromptGuardViolation(AssertionError):
    """A recording's expects/forbids guard failed: a prompt-construction regression."""


@dataclass
class Recording:
    output: dict[str, Any] | str
    expects_prompt_contains: list[str] = field(default_factory=list)
    forbids_prompt_contains: list[str] = field(default_factory=list)
    model: str = "recorded"
    input_tokens: int = 100
    output_tokens: int = 50


@dataclass
class RecordedProvider:
    recordings: list[Recording]
    name: str = "recorded"
    calls: list[dict[str, Any]] = field(default_factory=list)
    _cursor: int = 0

    def run(
        self,
        messages: list[Message],
        *,
        schema: type[BaseModel] | None = None,
        temperature: float = 0.2,
        max_tokens: int = 4096,
        timeout_s: float = 180.0,
        effort: str | None = None,
    ) -> Completion:
        if self._cursor >= len(self.recordings):
            raise ProviderError(f"{self.name}: no recording for call #{self._cursor + 1}")
        rec = self.recordings[self._cursor]
        self._cursor += 1
        prompt = "\n".join(f"{m.role}: {m.content}" for m in messages)
        for needle in rec.expects_prompt_contains:
            if needle not in prompt:
                raise PromptGuardViolation(f"prompt lacks {needle!r}")
        for needle in rec.forbids_prompt_contains:
            if needle in prompt:
                raise PromptGuardViolation(f"prompt contains forbidden {needle!r}")
        self.calls.append({"messages": messages, "schema": schema.__name__ if schema else None})
        if isinstance(rec.output, str):
            text, parsed = rec.output, None
        else:
            text, parsed = json.dumps(rec.output), dict(rec.output)
        return Completion(
            text=text, parsed=parsed,
            usage=Usage(input_tokens=rec.input_tokens, output_tokens=rec.output_tokens),
            provider=self.name, model=rec.model, latency_ms=0, stop_reason="end_turn",
        )
