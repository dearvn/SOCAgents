"""SQLite store used by the CLI. Snapshots and audit rows are append-only."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from socagents.core.errors import SocAgentsError
from socagents.core.ids import new_id
from socagents.core.timeutil import iso, utcnow
from socagents.runtime.states import RunStatus, check_run_transition

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    agent_id TEXT,
    kind TEXT NOT NULL,
    status TEXT NOT NULL,
    mode TEXT NOT NULL,
    model TEXT NOT NULL,
    input TEXT NOT NULL,
    output TEXT,
    error TEXT,
    steps INTEGER NOT NULL DEFAULT 0,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    cost_usd REAL,
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT
);
CREATE TABLE IF NOT EXISTS run_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL REFERENCES runs(id),
    seq INTEGER NOT NULL,
    type TEXT NOT NULL,
    payload TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (run_id, seq)
);
CREATE TABLE IF NOT EXISTS run_checkpoints (
    run_id TEXT NOT NULL REFERENCES runs(id),
    step INTEGER NOT NULL,
    state TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (run_id, step)
);
CREATE TABLE IF NOT EXISTS tool_calls (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(id),
    tool TEXT NOT NULL,
    args TEXT NOT NULL,
    risk_class TEXT,
    decision TEXT NOT NULL,
    status TEXT NOT NULL,
    error_code TEXT,
    latency_ms INTEGER NOT NULL,
    snapshot_id TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS data_snapshots (
    id TEXT PRIMARY KEY,
    run_id TEXT REFERENCES runs(id),
    socswift_user_id TEXT,
    tool TEXT NOT NULL,
    args TEXT NOT NULL,
    payload TEXT NOT NULL,
    source TEXT,
    as_of TEXT,
    delayed INTEGER NOT NULL,
    delayed_sec INTEGER,
    mode TEXT NOT NULL,
    trust TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS usage_ledger (
    id TEXT PRIMARY KEY,
    run_id TEXT REFERENCES runs(id),
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    role TEXT NOT NULL,
    input_tokens INTEGER NOT NULL,
    output_tokens INTEGER NOT NULL,
    cost_usd REAL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT,
    actor TEXT NOT NULL,
    action TEXT NOT NULL,
    detail TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_run_events_run ON run_events(run_id, seq);
CREATE INDEX IF NOT EXISTS idx_tool_calls_run ON tool_calls(run_id);
CREATE INDEX IF NOT EXISTS idx_snapshots_run ON data_snapshots(run_id);
CREATE TRIGGER IF NOT EXISTS snapshots_immutable BEFORE UPDATE ON data_snapshots
BEGIN SELECT RAISE(ABORT, 'data_snapshots are immutable'); END;
CREATE TRIGGER IF NOT EXISTS audit_immutable BEFORE UPDATE ON audit_events
BEGIN SELECT RAISE(ABORT, 'audit_events are append-only'); END;
CREATE TRIGGER IF NOT EXISTS audit_no_delete BEFORE DELETE ON audit_events
BEGIN SELECT RAISE(ABORT, 'audit_events are append-only'); END;
"""

TERMINAL = {RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED, RunStatus.EXPIRED}


def _dumps(value: Any) -> str:
    return json.dumps(value, default=str, separators=(",", ":"))


class Store:
    def __init__(self, path: Path | str) -> None:
        in_memory = str(path) == ":memory:"
        if not in_memory:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path))
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA foreign_keys = ON")
        if not in_memory:
            self._db.execute("PRAGMA journal_mode = WAL")
        self._db.executescript(SCHEMA)
        self._db.commit()

    def close(self) -> None:
        self._db.close()

    # runs

    def create_run(
        self,
        *,
        kind: str,
        mode: str,
        model: str,
        input: dict[str, Any],
        agent_id: str | None = None,
    ) -> str:
        run_id = new_id("run")
        self._db.execute(
            "INSERT INTO runs (id, agent_id, kind, status, mode, model, input, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                run_id,
                agent_id,
                kind,
                RunStatus.QUEUED.value,
                mode,
                model,
                _dumps(input),
                iso(utcnow()),
            ),
        )
        self._db.commit()
        return run_id

    def transition_run(
        self,
        run_id: str,
        to: RunStatus,
        *,
        output: dict[str, Any] | None = None,
        error: dict[str, Any] | None = None,
    ) -> None:
        row = self._db.execute("SELECT status FROM runs WHERE id = ?", (run_id,)).fetchone()
        if row is None:
            raise SocAgentsError(f"Unknown run {run_id}.", code="run_not_found")
        check_run_transition(RunStatus(row["status"]), to)
        now = iso(utcnow())
        self._db.execute(
            "UPDATE runs SET status = ?, "
            "started_at = CASE WHEN ? = 'running' AND started_at IS NULL "
            "THEN ? ELSE started_at END, "
            "finished_at = CASE WHEN ? THEN ? ELSE finished_at END, "
            "output = COALESCE(?, output), error = COALESCE(?, error) WHERE id = ?",
            (
                to.value,
                to.value,
                now,
                to in TERMINAL,
                now,
                _dumps(output) if output is not None else None,
                _dumps(error) if error is not None else None,
                run_id,
            ),
        )
        self._db.commit()

    def set_run_usage(
        self,
        run_id: str,
        *,
        steps: int,
        input_tokens: int,
        output_tokens: int,
        cost_usd: float | None,
    ) -> None:
        self._db.execute(
            "UPDATE runs SET steps = ?, input_tokens = ?, output_tokens = ?, cost_usd = ? "
            "WHERE id = ?",
            (steps, input_tokens, output_tokens, cost_usd, run_id),
        )
        self._db.commit()

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        row = self._db.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        return dict(row) if row else None

    # events and checkpoints

    def add_event(self, run_id: str, type: str, payload: dict[str, Any]) -> int:
        row = self._db.execute(
            "SELECT COALESCE(MAX(seq), 0) + 1 AS seq FROM run_events WHERE run_id = ?", (run_id,)
        ).fetchone()
        seq = int(row["seq"])
        self._db.execute(
            "INSERT INTO run_events (run_id, seq, type, payload, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (run_id, seq, type, _dumps(payload), iso(utcnow())),
        )
        self._db.commit()
        return seq

    def list_events(self, run_id: str) -> list[dict[str, Any]]:
        rows = self._db.execute(
            "SELECT seq, type, payload FROM run_events WHERE run_id = ? ORDER BY seq", (run_id,)
        ).fetchall()
        return [
            {"seq": r["seq"], "type": r["type"], "payload": json.loads(r["payload"])} for r in rows
        ]

    def save_checkpoint(self, run_id: str, step: int, state: dict[str, Any]) -> None:
        self._db.execute(
            "INSERT OR REPLACE INTO run_checkpoints (run_id, step, state, created_at) "
            "VALUES (?, ?, ?, ?)",
            (run_id, step, _dumps(state), iso(utcnow())),
        )
        self._db.commit()

    def latest_checkpoint(self, run_id: str) -> dict[str, Any] | None:
        row = self._db.execute(
            "SELECT state FROM run_checkpoints WHERE run_id = ? ORDER BY step DESC LIMIT 1",
            (run_id,),
        ).fetchone()
        return json.loads(row["state"]) if row else None

    # tool calls and snapshots

    def record_tool_call(
        self,
        *,
        run_id: str,
        tool: str,
        args: dict[str, Any],
        risk_class: str | None,
        decision: str,
        status: str,
        error_code: str | None,
        latency_ms: int,
        snapshot_id: str | None,
    ) -> str:
        call_id = new_id("tc")
        self._db.execute(
            "INSERT INTO tool_calls (id, run_id, tool, args, risk_class, decision, status, "
            "error_code, latency_ms, snapshot_id, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                call_id,
                run_id,
                tool,
                _dumps(args),
                risk_class,
                decision,
                status,
                error_code,
                latency_ms,
                snapshot_id,
                iso(utcnow()),
            ),
        )
        self._db.commit()
        return call_id

    def list_tool_calls(self, run_id: str) -> list[dict[str, Any]]:
        rows = self._db.execute(
            "SELECT * FROM tool_calls WHERE run_id = ? ORDER BY created_at, id", (run_id,)
        ).fetchall()
        return [dict(r) for r in rows]

    def save_snapshot(
        self,
        *,
        run_id: str | None,
        tool: str,
        args: dict[str, Any],
        payload: dict[str, Any],
        source: str | None,
        as_of: str | None,
        delayed_sec: int | None,
        mode: str,
        trust: str,
        socswift_user_id: str | None = None,
    ) -> str:
        snapshot_id = new_id("snp")
        self._db.execute(
            "INSERT INTO data_snapshots (id, run_id, socswift_user_id, tool, args, payload, "
            "source, as_of, delayed, delayed_sec, mode, trust, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                snapshot_id,
                run_id,
                socswift_user_id,
                tool,
                _dumps(args),
                _dumps(payload),
                source,
                as_of,
                int(bool(delayed_sec)),
                delayed_sec,
                mode,
                trust,
                iso(utcnow()),
            ),
        )
        self._db.commit()
        return snapshot_id

    def get_snapshot(self, snapshot_id: str) -> dict[str, Any] | None:
        row = self._db.execute(
            "SELECT * FROM data_snapshots WHERE id = ?", (snapshot_id,)
        ).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["args"] = json.loads(result["args"])
        result["payload"] = json.loads(result["payload"])
        result["delayed"] = bool(result["delayed"])
        return result

    # usage and audit

    def record_usage(
        self,
        *,
        run_id: str | None,
        provider: str,
        model: str,
        role: str,
        input_tokens: int,
        output_tokens: int,
        cost_usd: float | None,
    ) -> None:
        self._db.execute(
            "INSERT INTO usage_ledger (id, run_id, provider, model, role, input_tokens, "
            "output_tokens, cost_usd, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                new_id("use"),
                run_id,
                provider,
                model,
                role,
                input_tokens,
                output_tokens,
                cost_usd,
                iso(utcnow()),
            ),
        )
        self._db.commit()

    def audit(
        self,
        *,
        actor: str,
        action: str,
        run_id: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> None:
        self._db.execute(
            "INSERT INTO audit_events (run_id, actor, action, detail, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (run_id, actor, action, _dumps(detail) if detail is not None else None, iso(utcnow())),
        )
        self._db.commit()
