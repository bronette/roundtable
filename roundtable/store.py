"""SQLite store: one file per project. Thin repository functions, no ORM.
`agent_calls` is the audit log; domain tables reference it."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from roundtable.providers.base import Completion, Message
from roundtable.util import dumps, new_id, now_iso, sha256_text

DDL = """
CREATE TABLE IF NOT EXISTS projects (
  id TEXT PRIMARY KEY, name TEXT NOT NULL, objective TEXT NOT NULL,
  requirements_json TEXT NOT NULL, config_json TEXT NOT NULL, created_at TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS runs (
  id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id),
  stage TEXT NOT NULL, status TEXT NOT NULL,
  round INTEGER NOT NULL DEFAULT 0, fix_round INTEGER NOT NULL DEFAULT 0,
  workspace_path TEXT NOT NULL, started_at TEXT NOT NULL, finished_at TEXT, halt_reason TEXT,
  acceptance_json TEXT, acceptance_hash TEXT, acceptance_locked_at TEXT, ws_json TEXT);

CREATE TABLE IF NOT EXISTS agent_calls (
  id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(id),
  stage TEXT NOT NULL, role TEXT NOT NULL, provider TEXT NOT NULL, model TEXT NOT NULL,
  attempt INTEGER NOT NULL DEFAULT 1,
  prompt_hash TEXT NOT NULL, messages_json TEXT NOT NULL, context_refs_json TEXT NOT NULL,
  argv_json TEXT,
  response_text TEXT, parsed_json TEXT, schema_name TEXT,
  valid INTEGER NOT NULL, error TEXT,
  input_tokens INTEGER, output_tokens INTEGER, cached_input_tokens INTEGER, reasoning_tokens INTEGER,
  cost_usd REAL, reported_cost_usd REAL, cost_source TEXT, billing TEXT NOT NULL DEFAULT 'api',
  latency_ms INTEGER, request_id TEXT,
  started_at TEXT NOT NULL, finished_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS ix_calls_run ON agent_calls(run_id, started_at);

CREATE TABLE IF NOT EXISTS proposals (
  run_id TEXT NOT NULL REFERENCES runs(id), id TEXT NOT NULL,
  call_id TEXT NOT NULL REFERENCES agent_calls(id),
  revision_of TEXT, round INTEGER NOT NULL,
  status TEXT NOT NULL, body_json TEXT NOT NULL, created_at TEXT NOT NULL,
  PRIMARY KEY (run_id, id));

CREATE TABLE IF NOT EXISTS critiques (
  run_id TEXT NOT NULL REFERENCES runs(id), id TEXT NOT NULL,
  call_id TEXT NOT NULL REFERENCES agent_calls(id),
  target_kind TEXT NOT NULL, target_id TEXT NOT NULL,
  verdict TEXT NOT NULL, body_json TEXT NOT NULL, created_at TEXT NOT NULL,
  PRIMARY KEY (run_id, id));

CREATE TABLE IF NOT EXISTS artifacts (
  run_id TEXT NOT NULL REFERENCES runs(id), id TEXT NOT NULL,
  call_id TEXT REFERENCES agent_calls(id),
  kind TEXT NOT NULL, path TEXT NOT NULL, sha256 TEXT NOT NULL, bytes INTEGER NOT NULL, created_at TEXT NOT NULL,
  PRIMARY KEY (run_id, id));

CREATE TABLE IF NOT EXISTS implementations (
  run_id TEXT NOT NULL REFERENCES runs(id), id TEXT NOT NULL,
  call_id TEXT NOT NULL REFERENCES agent_calls(id),
  proposal_id TEXT NOT NULL, fix_round INTEGER NOT NULL,
  artifact_ids_json TEXT NOT NULL, body_json TEXT NOT NULL, created_at TEXT NOT NULL,
  PRIMARY KEY (run_id, id));

CREATE TABLE IF NOT EXISTS test_runs (
  run_id TEXT NOT NULL REFERENCES runs(id), id TEXT NOT NULL,
  implementation_id TEXT NOT NULL,
  command TEXT NOT NULL, exit_code INTEGER NOT NULL,
  passed INTEGER, failed INTEGER, errors INTEGER, timed_out INTEGER NOT NULL,
  output_artifact_id TEXT, duration_s REAL, created_at TEXT NOT NULL,
  PRIMARY KEY (run_id, id));

CREATE TABLE IF NOT EXISTS decisions (
  id INTEGER PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(id),
  from_stage TEXT NOT NULL, to_stage TEXT NOT NULL,
  reason TEXT NOT NULL, refs_json TEXT NOT NULL, created_at TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS open_questions (
  id INTEGER PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(id),
  raised_by_role TEXT NOT NULL, question TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'open', created_at TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS hypotheses (
  id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id),
  statement TEXT NOT NULL, status TEXT NOT NULL, body_json TEXT NOT NULL, created_at TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS experiments (
  id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id),
  hypothesis_id TEXT REFERENCES hypotheses(id),
  prereg_json TEXT NOT NULL, prereg_hash TEXT NOT NULL, locked_at TEXT NOT NULL,
  result_json TEXT, result_hash_check INTEGER, interpreted_at TEXT, decision TEXT);

CREATE TABLE IF NOT EXISTS lessons (
  id INTEGER PRIMARY KEY, project_id TEXT, scope TEXT NOT NULL,
  text TEXT NOT NULL, tags TEXT NOT NULL, source_run_id TEXT, created_at TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS usage_by_provider (
  run_id TEXT NOT NULL, provider TEXT NOT NULL,
  calls INTEGER NOT NULL, input_tokens INTEGER NOT NULL, output_tokens INTEGER NOT NULL,
  cost_usd REAL, PRIMARY KEY (run_id, provider));
"""


class Store:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, isolation_level=None)  # autocommit; explicit BEGIN where needed
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript(DDL)
        self._migrate()

    # columns added after the first release; CREATE TABLE IF NOT EXISTS does not add them to old files
    MIGRATIONS = {"runs": {"ws_json": "TEXT"}, "agent_calls": {"cost_source": "TEXT"}}

    def _migrate(self) -> None:
        for table, cols in self.MIGRATIONS.items():
            have = {r["name"] for r in self.db.execute(f"PRAGMA table_info({table})")}
            for col, typ in cols.items():
                if col not in have:
                    self.db.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")

    def close(self) -> None:
        self.db.close()

    # ---- projects / runs

    def create_project(self, *, name: str, objective: str, requirements: list[str], config: dict[str, Any]) -> str:
        pid = new_id("proj")
        self.db.execute(
            "INSERT INTO projects VALUES (?,?,?,?,?,?)",
            (pid, name, objective, dumps(requirements), dumps(config), now_iso()))
        return pid

    def create_run(self, *, project_id: str, workspace_path: str, stage: str = "INIT") -> str:
        rid = new_id("run")
        self.db.execute(
            "INSERT INTO runs (id, project_id, stage, status, workspace_path, started_at) VALUES (?,?,?,?,?,?)",
            (rid, project_id, stage, "running", workspace_path, now_iso()))
        return rid

    def set_stage(self, run_id: str, stage: str, *, round: int | None = None, fix_round: int | None = None) -> None:
        self.db.execute("UPDATE runs SET stage=? WHERE id=?", (stage, run_id))
        if round is not None:
            self.db.execute("UPDATE runs SET round=? WHERE id=?", (round, run_id))
        if fix_round is not None:
            self.db.execute("UPDATE runs SET fix_round=? WHERE id=?", (fix_round, run_id))

    def set_ws(self, run_id: str, ws: dict[str, Any]) -> None:
        self.db.execute("UPDATE runs SET ws_json=?, workspace_path=? WHERE id=?", (dumps(ws), ws["path"], run_id))

    def reopen_run(self, run_id: str, stage: str) -> None:
        self.db.execute("UPDATE runs SET status='running', finished_at=NULL, halt_reason=NULL, stage=? WHERE id=?", (stage, run_id))

    def call(self, call_id: str) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM agent_calls WHERE id=?", (call_id,)).fetchone()

    def artifact_text(self, run_id: str, artifact_id: str, run_dir: Path) -> str:
        r = self.db.execute("SELECT path FROM artifacts WHERE run_id=? AND id=?", (run_id, artifact_id)).fetchone()
        if not r:
            return ""
        try:
            return (run_dir / r["path"]).read_text()
        except OSError:
            return ""

    def finish_run(self, run_id: str, status: str, halt_reason: str | None = None) -> None:
        self.db.execute("UPDATE runs SET status=?, finished_at=?, halt_reason=? WHERE id=?",
                        (status, now_iso(), halt_reason, run_id))

    def get_run(self, run_id: str) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()

    # ---- audit log

    def record_call(
        self, *, run_id: str, stage: str, role: str, provider: str, model: str, attempt: int,
        messages: list[Message], context_refs: list[str], schema_name: str | None,
        completion: Completion | None, valid: bool, error: str | None,
        cost_usd: float | None, billing: str, started_at: str, cost_source: str | None = None,
    ) -> str:
        cid = new_id("call")
        messages_json = dumps([{"role": m.role, "content": m.content} for m in messages])
        c = completion
        self.db.execute(
            """INSERT INTO agent_calls (id, run_id, stage, role, provider, model, attempt, prompt_hash,
               messages_json, context_refs_json, argv_json, response_text, parsed_json, schema_name, valid, error,
               input_tokens, output_tokens, cached_input_tokens, reasoning_tokens, cost_usd, reported_cost_usd, cost_source,
               billing, latency_ms, request_id, started_at, finished_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (cid, run_id, stage, role, provider, model, attempt, sha256_text(messages_json),
             messages_json, dumps(context_refs), dumps(c.argv) if c and c.argv else None,
             c.text if c else None, dumps(c.parsed) if c and c.parsed is not None else None, schema_name,
             int(valid), error,
             c.usage.input_tokens if c else None, c.usage.output_tokens if c else None,
             c.usage.cached_input_tokens if c else None, c.usage.reasoning_tokens if c else None,
             cost_usd, c.reported_cost_usd if c else None, cost_source, billing,
             c.latency_ms if c else None, c.request_id if c else None, started_at, now_iso()))
        if c is not None:
            self.db.execute(
                """INSERT INTO usage_by_provider (run_id, provider, calls, input_tokens, output_tokens, cost_usd)
                   VALUES (?,?,1,?,?,?)
                   ON CONFLICT(run_id, provider) DO UPDATE SET
                     calls=calls+1, input_tokens=input_tokens+excluded.input_tokens,
                     output_tokens=output_tokens+excluded.output_tokens,
                     cost_usd=CASE WHEN cost_usd IS NULL AND excluded.cost_usd IS NULL THEN NULL
                                   ELSE COALESCE(cost_usd,0)+COALESCE(excluded.cost_usd,0) END""",
                (run_id, provider, c.usage.input_tokens, c.usage.output_tokens, cost_usd))
        return cid

    def usage(self, run_id: str) -> dict[str, Any]:
        rows = self.db.execute("SELECT * FROM usage_by_provider WHERE run_id=?", (run_id,)).fetchall()
        total = {"calls": 0, "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0, "cost_known": True}
        per: dict[str, dict[str, Any]] = {}
        for r in rows:
            per[r["provider"]] = dict(r)
            total["calls"] += r["calls"]
            total["input_tokens"] += r["input_tokens"]
            total["output_tokens"] += r["output_tokens"]
            if r["cost_usd"] is None:
                total["cost_known"] = False
            else:
                total["cost_usd"] += r["cost_usd"]
        total["tokens"] = total["input_tokens"] + total["output_tokens"]
        return {"total": total, "per_provider": per}

    def calls(self, run_id: str) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM agent_calls WHERE run_id=? ORDER BY started_at, rowid", (run_id,)).fetchall()

    def record_decision(self, run_id: str, from_stage: str, to_stage: str, reason: str, refs: list[str]) -> None:
        self.db.execute("INSERT INTO decisions (run_id, from_stage, to_stage, reason, refs_json, created_at) VALUES (?,?,?,?,?,?)",
                        (run_id, from_stage, to_stage, reason, dumps(refs), now_iso()))

    def decisions(self, run_id: str) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM decisions WHERE run_id=? ORDER BY id", (run_id,)).fetchall()

    # ---- per-run domain rows; ids are short and unique within a run (P1, C2, ...)

    def _next_id(self, table: str, run_id: str, prefix: str) -> str:
        n = self.db.execute(f"SELECT COUNT(*) FROM {table} WHERE run_id=?", (run_id,)).fetchone()[0]
        return f"{prefix}{n + 1}"

    def add_proposal(self, run_id: str, *, call_id: str, revision_of: str | None, round: int, body: dict[str, Any]) -> str:
        pid = self._next_id("proposals", run_id, "P")
        self.db.execute("INSERT INTO proposals VALUES (?,?,?,?,?,?,?,?)",
                        (run_id, pid, call_id, revision_of, round, "proposed", dumps(body), now_iso()))
        if revision_of:
            self.set_proposal_status(run_id, revision_of, "superseded")
        return pid

    def set_proposal_status(self, run_id: str, pid: str, status: str) -> None:
        self.db.execute("UPDATE proposals SET status=? WHERE run_id=? AND id=?", (status, run_id, pid))

    def proposal(self, run_id: str, pid: str) -> dict[str, Any]:
        r = self.db.execute("SELECT * FROM proposals WHERE run_id=? AND id=?", (run_id, pid)).fetchone()
        return json.loads(r["body_json"])

    def proposals(self, run_id: str) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM proposals WHERE run_id=? ORDER BY created_at", (run_id,)).fetchall()

    def add_critique(self, run_id: str, *, call_id: str, target_kind: str, target_id: str, verdict: str, body: dict[str, Any]) -> str:
        cid = self._next_id("critiques", run_id, "C")
        self.db.execute("INSERT INTO critiques VALUES (?,?,?,?,?,?,?,?)",
                        (run_id, cid, call_id, target_kind, target_id, verdict, dumps(body), now_iso()))
        return cid

    def critiques(self, run_id: str) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM critiques WHERE run_id=? ORDER BY created_at", (run_id,)).fetchall()

    def add_open_question(self, run_id: str, role: str, question: str) -> None:
        self.db.execute("INSERT INTO open_questions (run_id, raised_by_role, question, created_at) VALUES (?,?,?,?)",
                        (run_id, role, question, now_iso()))

    def open_questions(self, run_id: str) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM open_questions WHERE run_id=? ORDER BY id", (run_id,)).fetchall()

    def lock_acceptance(self, run_id: str, criteria: list[dict[str, Any]]) -> str:
        body = dumps(criteria)
        h = sha256_text(body)
        self.db.execute("UPDATE runs SET acceptance_json=?, acceptance_hash=?, acceptance_locked_at=? WHERE id=?",
                        (body, h, now_iso(), run_id))
        return h

    def add_artifact(self, run_id: str, *, call_id: str | None, kind: str, name: str, content: str, artifacts_dir: Path) -> str:
        aid = self._next_id("artifacts", run_id, "A")
        artifacts_dir.mkdir(parents=True, exist_ok=True)
        rel = f"artifacts/{aid}_{name}"
        (artifacts_dir / f"{aid}_{name}").write_text(content)
        data = content.encode()
        self.db.execute("INSERT INTO artifacts VALUES (?,?,?,?,?,?,?,?)",
                        (run_id, aid, call_id, kind, rel, sha256_text(content), len(data), now_iso()))
        return aid

    def add_implementation(self, run_id: str, *, call_id: str, proposal_id: str, fix_round: int,
                           artifact_ids: list[str], body: dict[str, Any]) -> str:
        iid = self._next_id("implementations", run_id, "I")
        self.db.execute("INSERT INTO implementations VALUES (?,?,?,?,?,?,?,?)",
                        (run_id, iid, call_id, proposal_id, fix_round, dumps(artifact_ids), dumps(body), now_iso()))
        return iid

    def implementations(self, run_id: str) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM implementations WHERE run_id=? ORDER BY created_at", (run_id,)).fetchall()

    def add_test_run(self, run_id: str, *, implementation_id: str, result: dict[str, Any], output_artifact_id: str | None) -> str:
        tid = self._next_id("test_runs", run_id, "T")
        self.db.execute("INSERT INTO test_runs VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                        (run_id, tid, implementation_id, result["command"], result["exit_code"], result["passed"],
                         result["failed"], result["errors"], int(result["timed_out"]), output_artifact_id,
                         result["duration_s"], now_iso()))
        return tid

    def test_runs(self, run_id: str) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM test_runs WHERE run_id=? ORDER BY created_at", (run_id,)).fetchall()

    # ---- experiments (pre-registered; criteria hashed before any result)

    def preregister(self, *, project_id: str, prereg: dict[str, Any], hypothesis_id: str | None = None) -> tuple[str, str]:
        n = self.db.execute("SELECT COUNT(*) FROM experiments WHERE project_id=?", (project_id,)).fetchone()[0]
        eid = f"E{n + 1}"
        body = dumps(prereg)
        h = sha256_text(body)
        self.db.execute("INSERT INTO experiments (id, project_id, hypothesis_id, prereg_json, prereg_hash, locked_at) VALUES (?,?,?,?,?,?)",
                        (eid, project_id, hypothesis_id, body, h, now_iso()))
        return eid, h

    def experiment(self, eid: str) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM experiments WHERE id=?", (eid,)).fetchone()

    def experiments(self, project_id: str | None = None) -> list[sqlite3.Row]:
        if project_id:
            return self.db.execute("SELECT * FROM experiments WHERE project_id=? ORDER BY locked_at", (project_id,)).fetchall()
        return self.db.execute("SELECT * FROM experiments ORDER BY locked_at").fetchall()

    def record_result(self, eid: str, result: dict[str, Any]) -> bool:
        """Stores the result and re-checks that the pre-registration has not been altered since locking."""
        r = self.experiment(eid)
        if r is None:
            raise KeyError(eid)
        intact = sha256_text(r["prereg_json"]) == r["prereg_hash"]
        self.db.execute("UPDATE experiments SET result_json=?, result_hash_check=? WHERE id=?", (dumps(result), int(intact), eid))
        return intact

    def record_interpretation(self, eid: str, decision: str, interpretation: dict[str, Any], review: dict[str, Any] | None) -> None:
        r = self.experiment(eid)
        result = json.loads(r["result_json"]) if r and r["result_json"] else {}
        result["interpretation"] = interpretation
        result["review"] = review
        self.db.execute("UPDATE experiments SET result_json=?, interpreted_at=?, decision=? WHERE id=?",
                        (dumps(result), now_iso(), decision, eid))

    def latest_project(self, name: str) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM projects WHERE name=? ORDER BY created_at DESC LIMIT 1", (name,)).fetchone()

    def runs(self, limit: int = 20) -> list[sqlite3.Row]:
        return self.db.execute(
            "SELECT r.*, p.name AS project_name, p.objective FROM runs r JOIN projects p ON p.id=r.project_id "
            "ORDER BY r.started_at DESC LIMIT ?", (limit,)).fetchall()
