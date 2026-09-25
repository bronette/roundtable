"""Hard caps checked before every model call. When one trips the run halts; it never
silently continues and never skips the final report."""

from __future__ import annotations

import time

from roundtable.config import BudgetCfg
from roundtable.store import Store


class BudgetExceeded(Exception):
    pass


class Budget:
    def __init__(self, cfg: BudgetCfg):
        self.cfg = cfg
        self.t0 = time.monotonic()

    @property
    def elapsed_s(self) -> float:
        return time.monotonic() - self.t0

    def check(self, store: Store, run_id: str, *, provider: str) -> None:
        c = self.cfg
        u = store.usage(run_id)
        t = u["total"]
        if t["calls"] >= c.max_calls:
            raise BudgetExceeded(f"max_calls reached ({t['calls']}/{c.max_calls})")
        if t["tokens"] >= c.max_tokens:
            raise BudgetExceeded(f"max_tokens reached ({t['tokens']}/{c.max_tokens})")
        if t["cost_known"] and t["cost_usd"] >= c.max_cost_usd:
            raise BudgetExceeded(f"max_cost_usd reached (${t['cost_usd']:.2f}/${c.max_cost_usd:.2f})")
        if self.elapsed_s >= c.max_seconds:
            raise BudgetExceeded(f"max_seconds reached ({int(self.elapsed_s)}s/{c.max_seconds}s)")
        pp = c.per_provider.get(provider)
        if pp:
            mine = u["per_provider"].get(provider, {"calls": 0, "input_tokens": 0, "output_tokens": 0})
            if pp.max_calls is not None and mine["calls"] >= pp.max_calls:
                raise BudgetExceeded(f"{provider}: max_calls reached ({mine['calls']}/{pp.max_calls})")
            if pp.max_tokens is not None and mine["input_tokens"] + mine["output_tokens"] >= pp.max_tokens:
                raise BudgetExceeded(f"{provider}: max_tokens reached")

    def summary(self, store: Store, run_id: str) -> dict:
        t = store.usage(run_id)["total"]
        return {"calls": f"{t['calls']}/{self.cfg.max_calls}", "tokens": f"{t['tokens']}/{self.cfg.max_tokens}",
                "cost_usd": (f"{t['cost_usd']:.4f}/{self.cfg.max_cost_usd:.2f}" if t["cost_known"] else "partly unknown"),
                "seconds": f"{int(self.elapsed_s)}/{self.cfg.max_seconds}"}
