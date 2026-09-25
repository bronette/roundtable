"""USD per million tokens by model. Unknown models cost None: the report never invents a number."""

from __future__ import annotations

from pathlib import Path

import yaml

from roundtable.providers.base import Usage


class Pricing:
    def __init__(self, table: dict[str, dict[str, float]] | None = None):
        self.table = table or {}

    @classmethod
    def load(cls, path: str | Path | None) -> "Pricing":
        if not path or not Path(path).exists():
            return cls()
        with open(path) as f:
            data = yaml.safe_load(f) or {}
        return cls({k: dict(v) for k, v in (data.get("models") or {}).items()})

    def rate(self, model: str) -> dict[str, float] | None:
        if model in self.table:
            return self.table[model]
        for key in sorted(self.table, key=len, reverse=True):   # longest prefix wins
            if model.startswith(key):
                return self.table[key]
        return None

    def cost(self, model: str, usage: Usage) -> float | None:
        r = self.rate(model)
        if r is None:
            return None
        cached_rate = r.get("cached_input", r["input"])
        return (usage.input_tokens * r["input"] + usage.cached_input_tokens * cached_rate
                + usage.output_tokens * r["output"]) / 1_000_000
