"""Agent = role + provider + schema. `call()` runs one validated exchange, with one repair
attempt on schema failure, and logs every attempt to the store."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel

from roundtable.config import AgentCfg, Config
from roundtable.pricing import Pricing
from roundtable.providers import build_provider
from roundtable.providers.base import Completion, Message, Provider, ProviderError, ProviderUnavailable, SchemaError, UsageLimitError, validate_output
from roundtable.store import Store
from roundtable.util import now_iso


@dataclass
class CallResult:
    call_id: str
    completion: Completion
    output: BaseModel | None
    attempts: int


BACKOFF_S = (3.0, 9.0, 27.0)
_sleep = time.sleep   # patched in tests


def _transient(e: Exception) -> bool:
    """Worth retrying: timeouts, 5xx, disconnects. Not: usage limits, missing binaries/keys, bad schemas."""
    if isinstance(e, (UsageLimitError, ProviderUnavailable, SchemaError)):
        return False
    return isinstance(e, ProviderError)


def priced_cost(pricing: Pricing, completion: Completion) -> tuple[float | None, str | None]:
    """Table price when the model is known; else the vendor's own estimate; else unknown."""
    c = pricing.cost(completion.model, completion.usage)
    if c is not None:
        return c, "table"
    if completion.reported_cost_usd is not None:
        return float(completion.reported_cost_usd), "reported"
    return None, None


class Agent:
    def __init__(self, role: str, cfg: AgentCfg, provider: Provider, *, billing: str, pricing: Pricing,
                 fallback: "Agent | None" = None):
        self.role, self.cfg, self.provider, self.billing, self.pricing = role, cfg, provider, billing, pricing
        self.fallback = fallback

    @classmethod
    def from_config(cls, role: str, config: Config, pricing: Pricing) -> "Agent":
        a = config.agents[role]
        p = config.providers[a.provider]
        provider = build_provider(a.provider, p, model=a.model)
        billing = "subscription" if (p.type == "cli" and not p.use_api_key) else "api"
        fallback = None
        if a.fallback:
            fp = config.providers[a.fallback]
            fcfg = a.model_copy(update={"provider": a.fallback, "model": None, "fallback": None})
            fallback = cls(role, fcfg, build_provider(a.fallback, fp, model=None),
                           billing="subscription" if (fp.type == "cli" and not fp.use_api_key) else "api", pricing=pricing)
        return cls(role, a, provider, billing=billing, pricing=pricing, fallback=fallback)

    def call(self, **kw) -> CallResult:
        """One validated exchange. If this provider fails (twice on schema, or a provider error other than
        a usage limit on the fallback itself) and a fallback is configured, the fallback answers the same prompt.
        Every attempt, including the failed ones, is in the audit log under its own provider name."""
        try:
            return self._call(**kw)
        except (ProviderError, SchemaError) as e:
            if self.fallback is None:
                raise
            kw["store"].add_open_question(kw["run_id"], self.role,
                f"{self.provider.name} failed at {kw['stage']} ({type(e).__name__}: {str(e)[:160]}); "
                f"{self.fallback.provider.name} answered instead")
            return self.fallback._call(**kw)

    def _call(
        self, *, store: Store, run_id: str, stage: str, system: str, prompt: str,
        schema: type[BaseModel] | None, context_refs: list[str] | None = None, workspace: str | None = None,
        readonly: bool = False,
    ) -> CallResult:
        messages = [Message("system", system), Message("user", prompt)]
        refs = context_refs or []
        last_error: str | None = None
        attempt = 0
        for schema_round in (1, 2):
            transient_left = self.cfg.retries
            while True:
                attempt += 1
                started = now_iso()
                completion: Completion | None = None
                output: BaseModel | None = None
                error: str | None = None
                try:
                    completion = self.provider.run(
                        messages, schema=schema, temperature=self.cfg.temperature,
                        max_tokens=self.cfg.max_tokens, timeout_s=self.cfg.timeout_s, effort=self.cfg.effort,
                        workspace=workspace, max_turns=self.cfg.max_turns, readonly=readonly)
                    if schema is not None:
                        if completion.parsed is None:
                            raise SchemaError("provider returned no JSON object")
                        output = validate_output(schema, completion.parsed)
                except SchemaError as e:
                    error = f"schema: {e}"
                except Exception as e:  # noqa: BLE001
                    store.record_call(run_id=run_id, stage=stage, role=self.role, provider=self.provider.name,
                                      model=self.cfg.model or "?", attempt=attempt, messages=messages,
                                      context_refs=refs, schema_name=schema.__name__ if schema else None,
                                      completion=None, valid=False, error=f"{type(e).__name__}: {str(e)[:500]}",
                                      cost_usd=None, billing=self.billing, started_at=started)
                    if _transient(e) and transient_left > 0:
                        delay = BACKOFF_S[min(self.cfg.retries - transient_left, len(BACKOFF_S) - 1)]
                        transient_left -= 1
                        _sleep(delay)
                        continue
                    raise
                break
            cost, cost_source = priced_cost(self.pricing, completion) if completion else (None, None)
            call_id = store.record_call(
                run_id=run_id, stage=stage, role=self.role, provider=self.provider.name,
                model=completion.model if completion else (self.cfg.model or "?"), attempt=attempt,
                messages=messages, context_refs=refs, schema_name=schema.__name__ if schema else None,
                completion=completion, valid=error is None, error=error, cost_usd=cost, cost_source=cost_source,
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
