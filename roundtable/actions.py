"""The only side effects in the system: prepare a workspace, read and write files in it,
capture the diff, run the tests. Everything here is called by the orchestrator, never by a
model, and every call is gated by the role's permissions in the pipeline."""

from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from roundtable.providers.cli.common import scrubbed_env
from roundtable.schemas import FileChange, TestResult

IGNORE_COPY = shutil.ignore_patterns(".git", ".venv", "venv", "node_modules", "__pycache__", ".pytest_cache", ".mypy_cache", "runs")
SKIP_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", "dist", "build"}


class WorkspaceError(Exception):
    pass


@dataclass
class Workspace:
    path: Path
    branch: str
    base_commit: str
    source_repo: Path | None      # the user's repo when a worktree was made; None for copies/greenfield
    mode: str                     # worktree | copy | greenfield

    def git(self, *args: str, check: bool = True) -> str:
        r = subprocess.run(["git", "-C", str(self.path), *args], capture_output=True, text=True)
        if check and r.returncode != 0:
            raise WorkspaceError(f"git {' '.join(args)}: {r.stderr.strip()}")
        return r.stdout


def _is_git_repo(p: Path) -> bool:
    return subprocess.run(["git", "-C", str(p), "rev-parse", "--is-inside-work-tree"], capture_output=True).returncode == 0


def _git(path: Path, *args: str) -> str:
    r = subprocess.run(["git", "-C", str(path), *args], capture_output=True, text=True)
    if r.returncode != 0:
        raise WorkspaceError(f"git {' '.join(args)}: {r.stderr.strip()}")
    return r.stdout


EXCLUDE_PATTERNS = ["__pycache__/", "*.pyc", ".pytest_cache/", ".mypy_cache/", ".ruff_cache/", ".venv/", "node_modules/"]


def _exclude_caches(ws_path: Path) -> None:
    """Keep tool caches out of the run's commits without touching the project's .gitignore.
    Works for worktrees too: git resolves info/exclude to the main repository."""
    path = _git(ws_path, "rev-parse", "--git-path", "info/exclude").strip()
    p = Path(path) if os.path.isabs(path) else ws_path / path
    p.parent.mkdir(parents=True, exist_ok=True)
    existing = p.read_text() if p.exists() else ""
    with open(p, "a") as f:
        for pat in EXCLUDE_PATTERNS:
            if pat not in existing:
                f.write(pat + "\n")


def prepare_workspace(run_dir: Path, repo: str | None, run_id: str, *, commit: str | None = None) -> Workspace:
    """A per-run working copy the engineer may write to. The user's repo is never modified
    except for the new branch ref that a worktree needs. `commit` pins a git repo to that revision."""
    ws = run_dir / "workspace"
    branch = f"roundtable/{run_id}"
    if repo:
        src = Path(os.path.expanduser(repo)).resolve()
        if not src.is_dir():
            raise WorkspaceError(f"repo path does not exist: {src}")
        if _is_git_repo(src):
            ws.parent.mkdir(parents=True, exist_ok=True)
            _git(src, "worktree", "add", "-b", branch, str(ws), commit or "HEAD")
            _exclude_caches(ws)
            base = _git(ws, "rev-parse", "HEAD").strip()
            return Workspace(ws, branch, base, src, "worktree")
        shutil.copytree(src, ws, ignore=IGNORE_COPY)
        mode = "copy"
    else:
        ws.mkdir(parents=True, exist_ok=True)
        mode = "greenfield"
    _git(ws, "init", "-q", "-b", branch)
    _exclude_caches(ws)
    _git(ws, "-c", "user.name=roundtable", "-c", "user.email=roundtable@localhost", "add", "-A")
    _git(ws, "-c", "user.name=roundtable", "-c", "user.email=roundtable@localhost",
         "commit", "-q", "--allow-empty", "-m", f"roundtable base for {run_id}")
    base = _git(ws, "rev-parse", "HEAD").strip()
    return Workspace(ws, branch, base, None, mode)


def file_tree(ws: Workspace, *, max_entries: int = 400) -> str:
    lines: list[str] = []
    for root, dirs, files in os.walk(ws.path):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS)
        rel = Path(root).relative_to(ws.path)
        for f in sorted(files):
            lines.append(str(rel / f) if str(rel) != "." else f)
            if len(lines) >= max_entries:
                lines.append(f"... (truncated at {max_entries} entries)")
                return "\n".join(lines)
    return "\n".join(lines) or "(empty)"


def safe_path(ws: Workspace, rel: str) -> Path:
    if not rel or os.path.isabs(rel) or ".." in Path(rel).parts or rel.startswith("~"):
        raise WorkspaceError(f"refusing path outside workspace: {rel!r}")
    target = (ws.path / rel)
    resolved = target.resolve()
    if ws.path.resolve() not in resolved.parents and resolved != ws.path.resolve():
        raise WorkspaceError(f"refusing path outside workspace: {rel!r}")
    if ".git" in Path(rel).parts:
        raise WorkspaceError(f"refusing to touch .git: {rel!r}")
    return target


def read_files(ws: Workspace, paths: list[str], *, max_chars_each: int = 12_000) -> dict[str, str]:
    out: dict[str, str] = {}
    for rel in paths:
        try:
            p = safe_path(ws, rel)
            text = p.read_text(errors="replace")
        except (WorkspaceError, OSError) as e:
            out[rel] = f"(unreadable: {e})"
            continue
        out[rel] = text if len(text) <= max_chars_each else text[:max_chars_each] + f"\n... (truncated at {max_chars_each} chars)"
    return out


def write_files(ws: Workspace, files: list[FileChange]) -> list[str]:
    written = []
    for fc in files:
        p = safe_path(ws, fc.path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(fc.content)
        written.append(fc.path)
    return written


def commit_changes(ws: Workspace, message: str) -> tuple[str, str, list[str]]:
    """Stage everything the engineer did, commit on the run branch, return (commit, diff, changed paths).
    Committing in the worktree adds commits to the run branch only; the user's checked-out branch is untouched."""
    ws.git("add", "-A")
    changed = [l[3:] for l in ws.git("status", "--porcelain").splitlines() if l.strip()]
    diff = ws.git("diff", "--cached", "--no-color")
    if not changed:
        return ws.git("rev-parse", "HEAD").strip(), "", []
    ws.git("-c", "user.name=roundtable", "-c", "user.email=roundtable@localhost", "commit", "-q", "-m", message)
    return ws.git("rev-parse", "HEAD").strip(), diff, changed


def cumulative_changes(ws: Workspace) -> tuple[str, list[str]]:
    """Everything the run has changed so far: base commit → HEAD. Robust to fix rounds that change
    nothing and to resumed runs; this is what reviewers and the report should see."""
    if not ws.base_commit:
        return "", []
    diff = ws.git("diff", "--no-color", f"{ws.base_commit}..HEAD", check=False)
    names = ws.git("diff", "--name-only", f"{ws.base_commit}..HEAD", check=False)
    return diff, [n for n in names.splitlines() if n.strip()]


_PYTEST_SUMMARY = re.compile(r"(\d+) (passed|failed|error|errors|skipped|xfailed|xpassed)")


def run_tests(ws: Workspace, command: str, *, python: str | None = None, timeout_s: float = 300.0) -> TestResult:
    argv = shlex.split(command)
    if argv and argv[0] in ("python", "python3"):
        argv[0] = python or sys.executable
    env = scrubbed_env(keep_api_keys=False)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    t0 = time.monotonic()
    try:
        r = subprocess.run(argv, cwd=ws.path, env=env, capture_output=True, text=True, timeout=timeout_s, stdin=subprocess.DEVNULL)
        out, rc, timed_out = r.stdout + r.stderr, r.returncode, False
    except subprocess.TimeoutExpired as e:
        out = (e.stdout or "") + (e.stderr or "") if isinstance(e.stdout, str) else ""
        rc, timed_out = 124, True
    except FileNotFoundError as e:
        out, rc, timed_out = f"command not found: {e}", 127, False
    counts = {"passed": 0, "failed": 0, "errors": 0}
    for n, kind in _PYTEST_SUMMARY.findall(out):
        if kind in ("error", "errors"):
            counts["errors"] += int(n)
        elif kind in counts:
            counts[kind] += int(n)
    return TestResult(command=" ".join(argv), exit_code=rc, duration_s=round(time.monotonic() - t0, 2),
                      stdout_tail=out[-4000:], timed_out=timed_out, **counts)
