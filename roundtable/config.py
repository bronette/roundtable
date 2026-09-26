"""project.yaml → typed config. `${VAR}` and `${VAR:-default}` expand from the environment."""

from __future__ import annotations

import os
import re
import shutil
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, model_validator

_VAR = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


class ConfigError(ValueError):
    pass


def expand_env(value: Any) -> Any:
    if isinstance(value, str):
        def sub(m: re.Match[str]) -> str:
            name, default = m.group(1), m.group(2)
            v = os.environ.get(name)
            if v is None or v == "":
                if default is None:
                    raise ConfigError(f"environment variable {name} is not set (referenced as ${{{name}}})")
                return default
            return v
        return _VAR.sub(sub, value)
    if isinstance(value, dict):
        return {k: expand_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [expand_env(v) for v in value]
    return value


class ProjectCfg(BaseModel):
    name: str
    objective: str
    requirements: list[str] = []
    repo: str | None = None          # any directory or git repo; None = greenfield
    domain: str | None = None        # e.g. "trading" enables the trading critic checklist (M5)
    test_command: str = "python -m pytest -q"
    python: str | None = None        # interpreter for `python ...` test commands; default: roundtable's own
    test_timeout_s: float = 300.0


class ProviderBudget(BaseModel):
    max_tokens: int | None = None
    max_calls: int | None = None


class BudgetCfg(BaseModel):
    max_rounds: int = 3
    max_fix_rounds: int = 1
    max_calls: int = 12
    max_tokens: int = 200_000
    max_cost_usd: float = 5.0
    max_seconds: int = 900
    per_provider: dict[str, ProviderBudget] = {}


class ProviderCfg(BaseModel):
    type: Literal["cli", "api"]
    cli: Literal["claude", "codex", "grok", "gemini", "agy"] | None = None
    sdk: Literal["anthropic", "openai_compat", "gemini", "ollama"] | None = None
    api_key_env: str | None = None
    base_url: str | None = None
    host: str | None = None
    binary: str | None = None
    use_api_key: bool = False        # cli: keep *_API_KEY in the child env (bills the API, not the subscription)
    extra_args: list[str] = []
    max_turns: int = 3               # cli: agent turns allowed per call (grok needs >1 for structured output)

    @model_validator(mode="after")
    def _shape(self) -> "ProviderCfg":
        if self.type == "cli" and not self.cli:
            raise ValueError("cli providers need `cli: claude|codex|grok|gemini|agy`")
        if self.type == "api" and not self.sdk:
            raise ValueError("api providers need `sdk: anthropic|openai_compat|gemini|ollama`")
        return self


class AgentCfg(BaseModel):
    provider: str
    model: str | None = None
    temperature: float = 0.2
    max_tokens: int = 4096
    timeout_s: float = 300.0
    effort: Literal["low", "medium", "high"] | None = None   # reasoning effort where the backend supports it
    retries: int = Field(default=2, ge=0)                    # extra attempts on transient provider errors (timeouts, 5xx, disconnects)
    fallback: str | None = None                              # provider to use when this one fails twice (logged; report shows who answered)
    mode: Literal["answer", "agent"] | None = None           # engineer only; default: agent for cli providers, answer otherwise
    max_turns: int = 40                                      # agent mode: tool-use turns allowed per call

    @model_validator(mode="after")
    def _empty_model(self) -> "AgentCfg":
        if self.model == "":
            self.model = None
        return self


class PermissionCfg(BaseModel):
    workspace: Literal["none", "read", "write"] = "none"
    run_tests: bool = False
    web: bool = False


class Config(BaseModel):
    project: ProjectCfg
    autonomy: int = Field(default=2, ge=0, le=4)
    budget: BudgetCfg = BudgetCfg()
    providers: dict[str, ProviderCfg]
    agents: dict[str, AgentCfg]
    permissions: dict[str, PermissionCfg] = {}
    pricing: str | None = None
    runs_dir: str = "runs"
    source_path: str | None = None

    @model_validator(mode="after")
    def _refs(self) -> "Config":
        for role, a in self.agents.items():
            if a.provider not in self.providers:
                raise ValueError(f"agent {role!r} references unknown provider {a.provider!r}")
            if a.fallback and a.fallback not in self.providers:
                raise ValueError(f"agent {role!r} fallback references unknown provider {a.fallback!r}")
        return self

    def check_providers(self) -> dict[str, str]:
        """Provider name → 'ok' or the reason it cannot be used. Never raises."""
        out: dict[str, str] = {}
        for name, p in self.providers.items():
            if p.type == "cli":
                binary = p.binary or p.cli or ""
                if not shutil.which(binary):
                    out[name] = f"{binary!r} not on PATH"
                elif p.cli == "gemini" and not (os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")):
                    out[name] = "GEMINI_API_KEY not set (personal login unsupported)"
                else:
                    out[name] = "ok"
            elif p.sdk == "ollama":
                out[name] = "ok"
            else:
                out[name] = "ok" if (p.api_key_env and os.environ.get(p.api_key_env)) else f"{p.api_key_env} not set"
        return out

    def resolve_path(self, p: str) -> Path:
        base = Path(self.source_path).parent if self.source_path else Path.cwd()
        return (base / os.path.expanduser(p)).resolve() if not os.path.isabs(os.path.expanduser(p)) else Path(os.path.expanduser(p))


def load_config(path: str | Path) -> Config:
    path = Path(path)
    with open(path) as f:
        raw = yaml.safe_load(f) or {}
    raw = expand_env(raw)
    try:
        cfg = Config.model_validate(raw)
    except Exception as e:  # noqa: BLE001
        raise ConfigError(f"{path}: {e}") from e
    cfg.source_path = str(path.resolve())
    return cfg
