"""Replay a desk run on its stored snapshots, and compare two Desk Reports.

A replay serves every tool call from the data recorded in the original run, so a different
model, prompt, or skill sees exactly the same market data. A call the original run never made
returns ``not_recorded`` instead of fetching new data.
"""

from __future__ import annotations

import json
from typing import Any

from socagents.core.errors import SocAgentsError
from socagents.db.store import Store
from socagents.desk.models import DeskReport


def _key(tool: str, args: dict[str, Any]) -> tuple[str, str]:
    return tool, json.dumps(args, sort_keys=True, default=str, separators=(",", ":"))


class ReplayBook:
    """Tool outputs recorded during one desk run, keyed by tool name and arguments."""

    def __init__(self, desk_run_id: str, entries: dict[tuple[str, str], dict[str, Any]]) -> None:
        self.desk_run_id = desk_run_id
        self._entries = entries

    def __len__(self) -> int:
        return len(self._entries)

    def lookup(self, tool: str, args: dict[str, Any]) -> dict[str, Any] | None:
        return self._entries.get(_key(tool, args))

    @classmethod
    def from_store(cls, store: Store, desk_run_id: str) -> ReplayBook:
        rows = store.connection.execute(
            "SELECT s.tool, s.args, s.payload FROM data_snapshots s "
            "JOIN runs r ON r.id = s.run_id "
            "WHERE json_extract(r.input, '$.desk_run_id') = ? ORDER BY s.created_at, s.id",
            (desk_run_id,),
        ).fetchall()
        entries: dict[tuple[str, str], dict[str, Any]] = {}
        for row in rows:
            payload = store.unseal(row["payload"])
            if payload.get("redacted"):
                raise SocAgentsError(
                    "The data for this run was not stored (member data needs an OS keychain), "
                    "so it cannot be replayed.",
                    code="replay_unavailable",
                )
            entries.setdefault(_key(row["tool"], json.loads(row["args"])), payload)
        if not entries:
            raise SocAgentsError(
                f"No stored data for desk run {desk_run_id}. It may have been purged.",
                code="replay_unavailable",
            )
        return cls(desk_run_id, entries)


def _models(report: DeskReport) -> str:
    return ", ".join(sorted(set(report.models.values()))) or "unknown"


def _analyst(report: DeskReport, role: str) -> str:
    for analyst in report.analysts:
        if analyst.role == role:
            return f"{analyst.stance} {analyst.confidence:.2f}"
    return "missing"


def _levels(report: DeskReport) -> str:
    return ", ".join(
        f"{level.price:g}" for level in sorted(report.key_levels, key=lambda x: x.price)
    )


def _ideas(report: DeskReport) -> str:
    if report.ideas:
        return "; ".join(f"{i.idea.contract} ({i.risk_check.display})" for i in report.ideas)
    return report.no_trade_reason or "none"


def _cost(report: DeskReport) -> str:
    cost = report.usage.cost_usd
    return "unknown" if cost is None else f"${cost:.4f}"


def compare_reports(original: DeskReport, replay: DeskReport) -> list[tuple[str, str, str]]:
    """Rows of (field, original, replay) for a side-by-side view."""
    roles = list(dict.fromkeys(a.role for a in original.analysts + replay.analysts))
    rows = [
        ("models", _models(original), _models(replay)),
        ("regime", original.regime, replay.regime),
        *((role, _analyst(original, role), _analyst(replay, role)) for role in roles),
        ("key levels", _levels(original), _levels(replay)),
        ("ideas", _ideas(original), _ideas(replay)),
        ("dissent", original.dissent, replay.dissent),
        ("model calls", str(original.usage.model_calls), str(replay.usage.model_calls)),
        ("cost", _cost(original), _cost(replay)),
    ]
    return rows
