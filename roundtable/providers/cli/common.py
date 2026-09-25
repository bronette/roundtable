"""Subprocess plumbing shared by the CLI adapters."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
from contextlib import contextmanager
from typing import Any, Iterator

from roundtable.providers.base import ProviderError, ProviderUnavailable, UsageLimitError, looks_like_usage_limit

KEY_ENV_VARS = ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "XAI_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY")


def require_binary(binary: str) -> str:
    path = shutil.which(binary)
    if not path:
        raise ProviderUnavailable(f"{binary!r} not found on PATH")
    return path


def scrubbed_env(*, keep_api_keys: bool) -> dict[str, str]:
    """Child env. Subscription mode strips API keys so the CLI cannot silently bill the API
    instead of using the login; agent-mode children never see keys either."""
    env = dict(os.environ)
    if not keep_api_keys:
        for k in KEY_ENV_VARS:
            env.pop(k, None)
    return env


@contextmanager
def answer_dir() -> Iterator[str]:
    """An empty cwd for answer-mode calls so the CLI picks up no CLAUDE.md / AGENTS.md."""
    d = tempfile.mkdtemp(prefix="roundtable-answer-")
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)


def run_argv(argv: list[str], *, cwd: str, env: dict[str, str], timeout_s: float) -> tuple[int, str, str, int]:
    t0 = time.monotonic()
    try:
        p = subprocess.run(
            argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
            capture_output=True, text=True, timeout=timeout_s,
        )
    except subprocess.TimeoutExpired as e:
        raise ProviderError(f"{argv[0]} timed out after {timeout_s}s") from e
    ms = int((time.monotonic() - t0) * 1000)
    return p.returncode, p.stdout, p.stderr, ms


def raise_for_failure(binary: str, rc: int, stdout: str, stderr: str) -> None:
    blob = f"{stderr}\n{stdout}"
    if looks_like_usage_limit(blob):
        raise UsageLimitError(f"{binary}: {blob.strip()[-500:]}")
    raise ProviderError(f"{binary} exited {rc}: {blob.strip()[-800:]}")


def last_json_object(stdout: str) -> dict[str, Any]:
    """The CLIs print one JSON object; tolerate leading log lines."""
    s = stdout.strip()
    try:
        v = json.loads(s)
        if isinstance(v, dict):
            return v
    except json.JSONDecodeError:
        pass
    i = s.find("{")
    if i != -1:
        try:
            v = json.loads(s[i:])
            if isinstance(v, dict):
                return v
        except json.JSONDecodeError:
            pass
    raise ProviderError(f"no JSON envelope in output: {s[-400:]!r}")


def ndjson(stdout: str) -> list[dict[str, Any]]:
    out = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out
