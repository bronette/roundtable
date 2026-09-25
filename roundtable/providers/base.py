"""The one provider interface. Adapters know nothing about roles, prompts, budgets, or the store."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ValidationError

Role = Literal["system", "user", "assistant"]


@dataclass(frozen=True)
class Message:
    role: Role
    content: str


@dataclass(frozen=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cached_input_tokens: int = 0
    reasoning_tokens: int = 0

    @property
    def total(self) -> int:
        return self.input_tokens + self.output_tokens


@dataclass
class Completion:
    text: str
    parsed: dict[str, Any] | None
    usage: Usage
    provider: str
    model: str
    latency_ms: int
    request_id: str | None = None
    stop_reason: str | None = None
    reported_cost_usd: float | None = None  # the provider's own estimate, never a bill
    raw: dict[str, Any] = field(default_factory=dict)
    argv: list[str] | None = None  # CLI providers: the exact command run


class ProviderError(Exception):
    """Any failure talking to a provider."""


class ProviderUnavailable(ProviderError):
    """Missing binary, key, or host. Raised at construction or health check."""


class SchemaError(ProviderError):
    """The model produced no JSON object, or one that fails validation."""


class UsageLimitError(ProviderError):
    """Subscription window or API rate limit exhausted. The pipeline halts and can resume later."""


@runtime_checkable
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
        effort: str | None = None,
        workspace: str | None = None,   # agent mode: run inside this directory with tools enabled
        max_turns: int | None = None,   # agent mode: tool-use turns allowed
    ) -> Completion: ...


# ---------------------------------------------------------------- helpers shared by adapters


def split_messages(messages: list[Message]) -> tuple[str | None, str]:
    """Return (system text, single prompt string).

    Single-prompt backends (the CLIs) get any multi-turn history rendered as a labelled
    transcript, which is how the one schema-repair round trip reaches them.
    """
    system_parts = [m.content for m in messages if m.role == "system"]
    system = "\n\n".join(system_parts) if system_parts else None
    rest = [m for m in messages if m.role != "system"]
    if not rest:
        raise ValueError("no user message")
    if len(rest) == 1 and rest[0].role == "user":
        return system, rest[0].content
    parts = []
    for m in rest:
        label = "USER" if m.role == "user" else "ASSISTANT (your previous reply)"
        parts.append(f"[{label}]\n{m.content}")
    return system, "\n\n".join(parts)


def json_schema_for(schema: type[BaseModel], *, strict: bool = False) -> dict[str, Any]:
    s = schema.model_json_schema()
    return strictify(s) if strict else s


def strictify(node: Any) -> Any:
    """OpenAI/Codex strict mode: every object sets additionalProperties=false and requires
    every property; `default` keywords are removed because strict mode rejects them."""
    if isinstance(node, dict):
        node.pop("default", None)
        if node.get("type") == "object" and "properties" in node:
            node["additionalProperties"] = False
            node["required"] = list(node["properties"].keys())
        for v in node.values():
            strictify(v)
    elif isinstance(node, list):
        for v in node:
            strictify(v)
    return node


_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.S)


def parse_json_loose(text: str) -> dict[str, Any]:
    """Find a JSON object in model text: bare, fenced, or embedded in prose."""
    text = text.strip()
    candidates = [text, *(m.group(1) for m in _FENCE.finditer(text))]
    i, j = text.find("{"), text.rfind("}")
    if i != -1 and j > i:
        candidates.append(text[i : j + 1])
    for cand in candidates:
        try:
            v = json.loads(cand)
        except json.JSONDecodeError:
            continue
        if isinstance(v, dict):
            return v
    raise SchemaError(f"no JSON object in response: {text[:200]!r}")


def validate_output(schema: type[BaseModel], data: dict[str, Any]) -> BaseModel:
    try:
        return schema.model_validate(data)
    except ValidationError as e:
        raise SchemaError(str(e)) from e


def looks_like_usage_limit(text: str) -> bool:
    t = text.lower()
    return any(
        k in t
        for k in (
            "usage limit", "rate limit", "rate_limit", "429", "quota", "too many requests",
            "limit reached", "out of credits", "insufficient_quota",
        )
    )
