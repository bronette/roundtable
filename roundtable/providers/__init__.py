"""Registry: build a Provider from a ProviderCfg + agent model."""

from __future__ import annotations

from roundtable.config import ProviderCfg
from roundtable.providers.base import Provider, ProviderUnavailable


def build_provider(name: str, cfg: ProviderCfg, *, model: str | None) -> Provider:
    if cfg.type == "cli":
        kw = dict(binary=cfg.binary or cfg.cli, model=model, use_api_key=cfg.use_api_key, extra_args=cfg.extra_args)
        if cfg.cli == "claude":
            from roundtable.providers.cli.claude import ClaudeCLI
            return ClaudeCLI(name, **kw)
        if cfg.cli == "codex":
            from roundtable.providers.cli.codex import CodexCLI
            return CodexCLI(name, **kw)
        if cfg.cli == "grok":
            from roundtable.providers.cli.grok import GrokCLI
            return GrokCLI(name, max_turns=cfg.max_turns, **kw)
        raise ProviderUnavailable(f"{name}: unknown cli {cfg.cli!r}")
    from roundtable.providers import api
    if cfg.sdk == "anthropic":
        return api.AnthropicAPI(name, model=model, api_key_env=cfg.api_key_env)
    if cfg.sdk == "openai_compat":
        return api.OpenAICompatAPI(name, model=model, api_key_env=cfg.api_key_env, base_url=cfg.base_url)
    if cfg.sdk == "gemini":
        return api.GeminiAPI(name, model=model, api_key_env=cfg.api_key_env)
    if cfg.sdk == "ollama":
        return api.OllamaAPI(name, model=model, host=cfg.host)
    raise ProviderUnavailable(f"{name}: unknown sdk {cfg.sdk!r}")
