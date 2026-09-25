"""Agent = role + provider + schema. `call()` runs one validated exchange, with one repair
attempt on schema failure, and logs every attempt to the store."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel

from roundtable.config import AgentCfg, Config
from roundtable.pricing import Pricing
from roundtable.providers import build_provider
from roundtable.providers.base import Completion, Message, Provider, SchemaError, validate_output
from roundtable.store import Store
from roundtable.util import now_iso


@dataclass
class CallResult:
    call_id: str
    completion: Completion
    output: BaseModel | None
    attempts: int


class Agent:
    def __init__(self, role: str, cfg: AgentCfg, provider: Provider, *, billing: str, pricing: Pricing):
        self.role, self.cfg, self.provider, self.billing, self.pricing = role, cfg, provider, billing, pricing

    @classmethod
    def from_config(cls, role: str, config: Config, pricing: Pricing) -> "Agent":
        a = config.agents[role]
        p = config.providers[a.provider]
        provider = build_provider(a.provider, p, model=a.model)
        billing = "subscription" if (p.type == "cli" and not p.use_api_key) else "api"
        return cls(role, a, provider, billing=billing, pricing=pricing)

    def call(
        self, *, store: Store, run_id: str, stage: str, system: str, prompt: str,
        schema: type[BaseModel] | None, context_refs: list[str] | None = None,
    ) -> CallResult:
        messages = [Message("system", system), Message("user", prompt)]
        refs = context_refs or []
        last_error: str | None = None
        for attempt in (1, 2):
            started = now_iso()
            completion: Completion | None = None
            output: BaseModel | None = None
            error: str | None = None
            try:
                completion = self.provider.run(
                    messages, schema=schema, temperature=self.cfg.temperature,
                    max_tokens=self.cfg.max_tokens, timeout_s=self.cfg.timeout_s, effort=self.cfg.effort)
                if schema is not None:
                    if completion.parsed is None:
                        raise SchemaError("provider returned no JSON object")
                    output = validate_output(schema, completion.parsed)
            except SchemaError as e:
                error = f"schema: {e}"
            except Exception:
                store.record_call(run_id=run_id, stage=stage, role=self.role, provider=self.provider.name,
                                  model=self.cfg.model or "?", attempt=attempt, messages=messages,
                                  context_refs=refs, schema_name=schema.__name__ if schema else None,
                                  completion=None, valid=False, error="provider error", cost_usd=None,
                                  billing=self.billing, started_at=started)
                raise
            cost = self.pricing.cost(completion.model, completion.usage) if completion else None
            call_id = store.record_call(
                run_id=run_id, stage=stage, role=self.role, provider=self.provider.name,
                model=completion.model if completion else (self.cfg.model or "?"), attempt=attempt,
                messages=messages, context_refs=refs, schema_name=schema.__name__ if schema else None,
                completion=completion, valid=error is None, error=error, cost_usd=cost,
                billing=self.billing, started_at=started)
            if error is None:
                return CallResult(call_id, completion, output, attempt)  # type: ignore[arg-type]
            last_error = error
            # one repair round trip: show the model its output and the validation error
            messages = messages + [
                Message("assistant", completion.text if completion else f"(unparseable reply) {last_error}"),
                Message("user", f"Your reply did not validate against the required JSON schema:\n{error}\n\n"
                                f"Reply again with ONLY a JSON object that satisfies the schema."),
            ]
        raise SchemaError(f"{self.role}: output failed validation twice; last error: {last_error}")
