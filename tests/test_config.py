import pathlib

import pytest

from roundtable.config import ConfigError, expand_env, load_config

MIN = """
project: {name: t, objective: o}
providers:
  c: {type: cli, cli: claude}
  a: {type: api, sdk: anthropic, api_key_env: NOPE_KEY}
agents:
  proposer: {provider: c, model: "${RT_TEST_MODEL:-}"}
  critic: {provider: a, model: "${RT_TEST_MODEL:-fallback}"}
"""


def test_env_expansion(monkeypatch):
    monkeypatch.delenv("RT_TEST_MODEL", raising=False)
    assert expand_env("${RT_TEST_MODEL:-d}") == "d"
    monkeypatch.setenv("RT_TEST_MODEL", "m1")
    assert expand_env({"k": ["${RT_TEST_MODEL}"]}) == {"k": ["m1"]}
    monkeypatch.delenv("RT_TEST_MODEL")
    with pytest.raises(ConfigError):
        expand_env("${RT_TEST_MODEL}")


def test_load_config_and_provider_status(tmp_path, monkeypatch):
    monkeypatch.delenv("RT_TEST_MODEL", raising=False)
    monkeypatch.delenv("NOPE_KEY", raising=False)
    p = tmp_path / "project.yaml"
    p.write_text(MIN)
    cfg = load_config(p)
    assert cfg.agents["proposer"].model is None          # empty string → None
    assert cfg.agents["critic"].model == "fallback"
    st = cfg.check_providers()
    assert st["a"] == "NOPE_KEY not set"
    assert st["c"] in ("ok", "'claude' not on PATH")


def test_unknown_provider_reference(tmp_path):
    p = tmp_path / "project.yaml"
    p.write_text("project: {name: t, objective: o}\nproviders: {}\nagents: {x: {provider: nope}}\n")
    with pytest.raises(ConfigError):
        load_config(p)


def test_pricing_prefix_match():
    from roundtable.pricing import Pricing
    from roundtable.providers.base import Usage
    pr = Pricing({"claude-sonnet-5": {"input": 3.0, "output": 15.0, "cached_input": 0.3}})
    assert pr.cost("claude-sonnet-5-20260101", Usage(1_000_000, 1_000_000, 1_000_000)) == pytest.approx(18.3)
    assert pr.cost("unknown", Usage(1, 1)) is None


def test_relative_paths_resolve_against_config_file(tmp_path):
    (tmp_path / "cfg").mkdir(); (tmp_path / "repo").mkdir()
    p = tmp_path / "cfg" / "project.yaml"
    p.write_text("project: {name: t, objective: o, repo: ../repo, python: ../repo/py}\nproviders: {}\nagents: {}\n")
    cfg = load_config(p)
    assert cfg.resolve_path(cfg.project.repo) == (tmp_path / "repo").resolve()
    assert cfg.resolve_path(cfg.project.python) == (tmp_path / "repo" / "py").resolve()
    assert cfg.resolve_path("/abs/x") == pathlib.Path("/abs/x")
