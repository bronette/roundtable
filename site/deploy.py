"""Upload site/ to the cPanel host over SFTP.

Reads connection settings from cpanel/sftp.env (gitignored):
  RT_SFTP_HOST, RT_SFTP_PORT, RT_SFTP_USER, RT_SFTP_PASS, RT_SFTP_REMOTE
Run:  uv run --with paramiko python site/deploy.py
"""

from __future__ import annotations

import os
import posixpath
import sys
from pathlib import Path

import paramiko

ROOT = Path(__file__).resolve().parent.parent
SITE = ROOT / "site"
ENV = ROOT / "cpanel" / "sftp.env"
SKIP = {"deploy.py", "build_docs.py", ".DS_Store"}
SKIP_DIRS = {"__pycache__", ".git"}


def load_env(path: Path) -> dict[str, str]:
    out = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def main() -> int:
    if not ENV.exists():
        print(f"missing {ENV}", file=sys.stderr)
        return 2
    env = load_env(ENV)
    host, port, user, pw = env["RT_SFTP_HOST"], int(env.get("RT_SFTP_PORT", "22")), env["RT_SFTP_USER"], env["RT_SFTP_PASS"]
    remote = env.get("RT_SFTP_REMOTE", "public_html")
    t = paramiko.Transport((host, port))
    t.connect(username=user, password=pw)
    sftp = paramiko.SFTPClient.from_transport(t)
    uploaded = []
    for local in sorted(SITE.rglob("*")):
        if local.is_dir() or local.name in SKIP or any(part in SKIP_DIRS for part in local.relative_to(SITE).parts):
            continue
        rel = local.relative_to(SITE).as_posix()
        dest = posixpath.join(remote, rel)
        parent = posixpath.dirname(dest)
        parts, cur = parent.split("/"), ""
        for part in parts:
            cur = posixpath.join(cur, part) if cur else part
            try:
                sftp.stat(cur)
            except FileNotFoundError:
                sftp.mkdir(cur)
        sftp.put(str(local), dest)
        uploaded.append((rel, local.stat().st_size))
    sftp.close()
    t.close()
    for rel, size in uploaded:
        print(f"uploaded {rel} ({size} bytes) -> {remote}/{rel}")
    print(f"{len(uploaded)} file(s) to {user}@{host}:{remote}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
