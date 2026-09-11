"""Desk tables in the local SQLite store. Member rows are sealed (encrypted) like snapshots."""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any

from pydantic import BaseModel

from socagents.core.ids import new_id
from socagents.core.timeutil import iso, utcnow
from socagents.db.store import Store
from socagents.desk.models import DeskReport, UsageSummary

DESK_SCHEMA = """
CREATE TABLE IF NOT EXISTS desk_runs (
    id TEXT PRIMARY KEY,
    symbol TEXT NOT NULL,
    profile TEXT NOT NULL,
    mode TEXT NOT NULL,
    status TEXT NOT NULL,
    models TEXT NOT NULL,
    error TEXT,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    cost_usd REAL,
    started_at TEXT NOT NULL,
    finished_at TEXT
);
CREATE TABLE IF NOT EXISTS analyst_reports (
    id TEXT PRIMARY KEY,
    desk_run_id TEXT NOT NULL REFERENCES desk_runs(id) ON DELETE CASCADE,
    role TEXT NOT NULL,
    run_id TEXT,
    report TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS debate_turns (
    id TEXT PRIMARY KEY,
    desk_run_id TEXT NOT NULL REFERENCES desk_runs(id) ON DELETE CASCADE,
    round INTEGER NOT NULL,
    side TEXT NOT NULL,
    turn TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS desk_ideas (
    id TEXT PRIMARY KEY,
    desk_run_id TEXT NOT NULL REFERENCES desk_runs(id) ON DELETE CASCADE,
    idx INTEGER NOT NULL,
    idea TEXT NOT NULL,
    convertible INTEGER NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS desk_reports (
    id TEXT PRIMARY KEY,
    desk_run_id TEXT NOT NULL UNIQUE REFERENCES desk_runs(id) ON DELETE CASCADE,
    symbol TEXT NOT NULL,
    mode TEXT NOT NULL,
    profile TEXT NOT NULL,
    as_of TEXT NOT NULL,
    report TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_desk_reports_symbol ON desk_reports(symbol, as_of DESC);
CREATE TRIGGER IF NOT EXISTS desk_reports_immutable BEFORE UPDATE ON desk_reports
BEGIN SELECT RAISE(ABORT, 'desk_reports are immutable'); END;
"""


class DeskStore:
    def __init__(self, store: Store) -> None:
        self._store = store
        self._db = store.connection
        self._db.executescript(DESK_SCHEMA)
        self._db.commit()

    def _seal(self, value: BaseModel, mode: str) -> str:
        return self._store.seal(value.model_dump(mode="json"), mode)

    def create_run(self, *, symbol: str, profile: str, mode: str, models: dict[str, str]) -> str:
        desk_run_id = new_id("desk")
        self._db.execute(
            "INSERT INTO desk_runs (id, symbol, profile, mode, status, models, started_at) "
            "VALUES (?, ?, ?, ?, 'running', ?, ?)",
            (desk_run_id, symbol, profile, mode, json.dumps(models), iso(utcnow())),
        )
        self._db.commit()
        return desk_run_id

    def finish_run(
        self, desk_run_id: str, *, status: str, usage: UsageSummary, error: str | None = None
    ) -> None:
        self._db.execute(
            "UPDATE desk_runs SET status = ?, error = ?, input_tokens = ?, output_tokens = ?, "
            "cost_usd = ?, finished_at = ? WHERE id = ?",
            (
                status,
                error,
                usage.input_tokens,
                usage.output_tokens,
                usage.cost_usd,
                iso(utcnow()),
                desk_run_id,
            ),
        )
        self._db.commit()

    def save_analyst_report(
        self, desk_run_id: str, *, role: str, run_id: str | None, report: BaseModel, mode: str
    ) -> None:
        self._db.execute(
            "INSERT INTO analyst_reports (id, desk_run_id, role, run_id, report, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (new_id("ar"), desk_run_id, role, run_id, self._seal(report, mode), iso(utcnow())),
        )
        self._db.commit()

    def save_turn(
        self, desk_run_id: str, *, round: int, side: str, turn: BaseModel, mode: str
    ) -> None:
        self._db.execute(
            "INSERT INTO debate_turns (id, desk_run_id, round, side, turn, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (new_id("dt"), desk_run_id, round, side, self._seal(turn, mode), iso(utcnow())),
        )
        self._db.commit()

    def save_idea(
        self, desk_run_id: str, *, idx: int, idea: BaseModel, convertible: bool, mode: str
    ) -> None:
        self._db.execute(
            "INSERT INTO desk_ideas (id, desk_run_id, idx, idea, convertible, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                new_id("di"),
                desk_run_id,
                idx,
                self._seal(idea, mode),
                int(convertible),
                iso(utcnow()),
            ),
        )
        self._db.commit()

    def save_report(self, report: DeskReport) -> None:
        self._db.execute(
            "INSERT INTO desk_reports (id, desk_run_id, symbol, mode, profile, as_of, report, "
            "created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                report.id,
                report.desk_run_id,
                report.symbol,
                report.mode,
                report.profile,
                iso(report.as_of),
                self._seal(report, report.mode),
                iso(report.created_at),
            ),
        )
        self._db.commit()

    def get_report(self, ref: str) -> DeskReport | None:
        """Find a report by report id, desk run id, or a unique prefix of either."""
        rows = self._db.execute(
            "SELECT report FROM desk_reports WHERE id = ? OR desk_run_id = ? "
            "OR id LIKE ? OR desk_run_id LIKE ? ORDER BY created_at DESC LIMIT 2",
            (ref, ref, f"{ref}%", f"{ref}%"),
        ).fetchall()
        if len(rows) != 1:
            return None
        payload = self._store.unseal(rows[0]["report"])
        if payload.get("redacted"):
            return None
        return DeskReport.model_validate(payload)

    def list_reports(self, limit: int = 20) -> list[dict[str, Any]]:
        rows = self._db.execute(
            "SELECT r.id, r.symbol, r.mode, r.profile, r.as_of, r.created_at, d.cost_usd "
            "FROM desk_reports r JOIN desk_runs d ON d.id = r.desk_run_id "
            "ORDER BY r.created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]

    def purge_member(self, *, older_than_days: int | None = None) -> int:
        query = "SELECT id FROM desk_runs WHERE mode = 'member'"
        params: tuple[Any, ...] = ()
        if older_than_days is not None:
            query += " AND started_at < ?"
            params = (iso(utcnow() - timedelta(days=older_than_days)),)
        ids = [r["id"] for r in self._db.execute(query, params).fetchall()]
        for table in ("desk_reports", "desk_ideas", "debate_turns", "analyst_reports"):
            self._db.executemany(f"DELETE FROM {table} WHERE desk_run_id = ?", [(i,) for i in ids])
        self._db.executemany("DELETE FROM desk_runs WHERE id = ?", [(i,) for i in ids])
        self._db.commit()
        return len(ids)
