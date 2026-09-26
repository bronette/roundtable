"""API-key adapters: anthropic, openai_compat (OpenAI + xAI), gemini, ollama.
SDKs are imported lazily so a missing key or package only affects the provider that needs it."""

from __future__ import annotations

import json
import os
import time
from typing import Any

from pydantic import BaseModel

from roundtable.providers.base import (
    Completion, Message, ProviderError, ProviderUnavailable, UsageLimitError, Usage,
    json_schema_for, parse_json_loose, split_messages,
)


def _key(env_name: str | None, provider: str) -> str:
    if not env_name:
        raise ProviderUnavailable(f"{provider}: api_key_env not configured")
    v = os.environ.get(env_name)
    if not v:
        raise ProviderUnavailable(f"{provider}: environment variable {env_name} is not set")
    return v


def _wrap(provider: str, e: Exception) -> ProviderError:
    status = getattr(e, "status_code", None) or getattr(e, "code", None)
    if status in (429,) or "rate" in str(e).lower() or "quota" in str(e).lower():
        return UsageLimitError(f"{provider}: {e}")
    return ProviderError(f"{provider}: {e}")


class AnthropicAPI:
    def __init__(self, name: str, *, model: str | None, api_key_env: str | None):
        self.name, self.model = name, model or "claude-sonnet-5"
        self._key = _key(api_key_env, name)

    def run(self, messages: list[Message], *, schema: type[BaseModel] | None = None,
            temperature: float = 0.2, max_tokens: int = 4096, timeout_s: float = 180.0,
            effort: str | None = None, workspace: str | None = None, max_turns: int | None = None) -> Completion:
        import anthropic
        client = anthropic.Anthropic(api_key=self._key, timeout=timeout_s)
        system = "\n\n".join(m.content for m in messages if m.role == "system") or anthropic.NOT_GIVEN
        convo = [{"role": m.role, "content": m.content} for m in messages if m.role != "system"]
        kwargs: dict[str, Any] = dict(model=self.model, system=system, messages=convo,
                                      max_tokens=max_tokens, temperature=temperature)
        if schema is not None:
            kwargs["tools"] = [{"name": "emit", "description": "Return the structured answer.",
                                "input_schema": json_schema_for(schema)}]
            kwargs["tool_choice"] = {"type": "tool", "name": "emit"}
        t0 = time.monotonic()
        try:
            resp = client.messages.create(**kwargs)
        except Exception as e:  # noqa: BLE001
            raise _wrap(self.name, e) from e
        ms = int((time.monotonic() - t0) * 1000)
        parsed, text = None, ""
        for block in resp.content:
            if block.type == "tool_use" and schema is not None:
                parsed = dict(block.input)
            elif block.type == "text":
                text += block.text
        if schema is not None and parsed is None:
            parsed = parse_json_loose(text)
        u = resp.usage
        return Completion(
            text=json.dumps(parsed) if parsed is not None else text, parsed=parsed,
            usage=Usage(input_tokens=u.input_tokens + (getattr(u, "cache_creation_input_tokens", 0) or 0),
                        output_tokens=u.output_tokens,
                        cached_input_tokens=getattr(u, "cache_read_input_tokens", 0) or 0),
            provider=self.name, model=resp.model, latency_ms=ms, request_id=resp.id,
            stop_reason=resp.stop_reason, raw=resp.model_dump(mode="json"),
        )


class OpenAICompatAPI:
    """OpenAI, and any OpenAI-compatible endpoint such as xAI via base_url."""

    def __init__(self, name: str, *, model: str | None, api_key_env: str | None, base_url: str | None = None):
        self.name, self.model, self.base_url = name, model or "gpt-5", base_url
        self._key = _key(api_key_env, name)

    def run(self, messages: list[Message], *, schema: type[BaseModel] | None = None,
            temperature: float = 0.2, max_tokens: int = 4096, timeout_s: float = 180.0,
            effort: str | None = None, workspace: str | None = None, max_turns: int | None = None) -> Completion:
        import openai
        client = openai.OpenAI(api_key=self._key, base_url=self.base_url, timeout=timeout_s)
        kwargs: dict[str, Any] = dict(
            model=self.model, messages=[{"role": m.role, "content": m.content} for m in messages],
            temperature=temperature, max_completion_tokens=max_tokens)
        if schema is not None:
            kwargs["response_format"] = {"type": "json_schema", "json_schema": {
                "name": schema.__name__, "strict": True, "schema": json_schema_for(schema, strict=True)}}
        t0 = time.monotonic()
        try:
            resp = client.chat.completions.create(**kwargs)
        except Exception as e:  # noqa: BLE001
            raise _wrap(self.name, e) from e
        ms = int((time.monotonic() - t0) * 1000)
        choice = resp.choices[0]
        text = choice.message.content or ""
        parsed = parse_json_loose(text) if schema is not None else None
        u = resp.usage
        details = getattr(u, "prompt_tokens_details", None)
        return Completion(
            text=text, parsed=parsed,
            usage=Usage(input_tokens=u.prompt_tokens, output_tokens=u.completion_tokens,
                        cached_input_tokens=getattr(details, "cached_tokens", 0) or 0),
            provider=self.name, model=resp.model, latency_ms=ms, request_id=resp.id,
            stop_reason=choice.finish_reason, raw=resp.model_dump(mode="json"),
        )


class GeminiAPI:
    def __init__(self, name: str, *, model: str | None, api_key_env: str | None):
        self.name, self.model = name, model or "gemini-flash-latest"   # alias Google maintains; concrete names retire
        self._key = _key(api_key_env, name)

    def run(self, messages: list[Message], *, schema: type[BaseModel] | None = None,
            temperature: float = 0.2, max_tokens: int = 4096, timeout_s: float = 180.0,
            effort: str | None = None, workspace: str | None = None, max_turns: int | None = None) -> Completion:
        from google import genai
        from google.genai import types
        client = genai.Client(api_key=self._key, http_options=types.HttpOptions(timeout=int(timeout_s * 1000)))
        system, prompt = split_messages(messages)
        cfg = types.GenerateContentConfig(
            system_instruction=system, temperature=temperature, max_output_tokens=max_tokens,
            response_mime_type="application/json" if schema is not None else None,
            response_json_schema=json_schema_for(schema) if schema is not None else None,
        )
        t0 = time.monotonic()
        try:
            resp = client.models.generate_content(model=self.model, contents=prompt, config=cfg)
        except Exception as e:  # noqa: BLE001
            raise _wrap(self.name, e) from e
        ms = int((time.monotonic() - t0) * 1000)
        text = resp.text or ""
        parsed = parse_json_loose(text) if schema is not None else None
        u = resp.usage_metadata
        return Completion(
            text=text, parsed=parsed,
            usage=Usage(input_tokens=getattr(u, "prompt_token_count", 0) or 0,
                        output_tokens=getattr(u, "candidates_token_count", 0) or 0,
                        cached_input_tokens=getattr(u, "cached_content_token_count", 0) or 0,
                        reasoning_tokens=getattr(u, "thoughts_token_count", 0) or 0),
            provider=self.name, model=self.model, latency_ms=ms,
            request_id=getattr(resp, "response_id", None), stop_reason=None,
            raw=resp.model_dump(mode="json") if hasattr(resp, "model_dump") else {},
        )


class OllamaAPI:
    def __init__(self, name: str, *, model: str | None, host: str | None = None):
        self.name, self.model, self.host = name, model or "qwen3:30b-a3b", host or "http://localhost:11434"

    def run(self, messages: list[Message], *, schema: type[BaseModel] | None = None,
            temperature: float = 0.2, max_tokens: int = 4096, timeout_s: float = 180.0,
            effort: str | None = None, workspace: str | None = None, max_turns: int | None = None) -> Completion:
        import ollama
        client = ollama.Client(host=self.host, timeout=timeout_s)
        t0 = time.monotonic()
        try:
            resp = client.chat(
                model=self.model, messages=[{"role": m.role, "content": m.content} for m in messages],
                format=json_schema_for(schema) if schema is not None else None,
                options={"temperature": temperature, "num_predict": max_tokens},
            )  # model default for `think`: qwen3 reasons in a separate channel, keeping JSON fields clean
        except Exception as e:  # noqa: BLE001
            raise _wrap(self.name, e) from e
        ms = int((time.monotonic() - t0) * 1000)
        text = resp.message.content or ""
        parsed = parse_json_loose(text) if schema is not None else None
        return Completion(
            text=text, parsed=parsed,
            usage=Usage(input_tokens=int(resp.prompt_eval_count or 0), output_tokens=int(resp.eval_count or 0)),
            provider=self.name, model=resp.model or self.model, latency_ms=ms, stop_reason=resp.done_reason,
            raw={"total_duration": resp.total_duration},
        )
