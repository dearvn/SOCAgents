"""Live terminal view of a desk run, driven by desk events."""

from __future__ import annotations

from dataclasses import dataclass

from rich.console import Group, RenderableType
from rich.table import Table
from rich.text import Text

from socagents.desk.graph import DeskEvent
from socagents.desk.roles import ROLES

ICONS = {
    "waiting": ("·", "dim"),
    "running": ("…", "cyan"),
    "done": ("✓", "green"),
    "failed": ("✗", "red"),
}
STANCE_STYLE = {"bullish": "green", "bearish": "red", "neutral": "yellow"}


@dataclass
class _Row:
    status: str = "waiting"
    stance: str = ""
    confidence: float | None = None
    activity: str = ""


class DeskLiveView:
    def __init__(self, symbol: str, profile: str, roles: list[str]) -> None:
        self.symbol = symbol
        self.profile = profile
        self.mode = ""
        self.rows = {role: _Row() for role in roles}
        self.input_tokens = 0
        self.output_tokens = 0
        self.cost: float | None = 0.0
        self.status = "starting"

    def handle(self, event: DeskEvent) -> None:
        data, role = event.data, event.role
        row = self.rows.get(role) if role else None
        if event.type == "desk.started":
            self.mode = str(data.get("mode", ""))
            self.rows = {r: self.rows.get(r, _Row()) for r in data.get("roles", self.rows)}
            self.status = "running"
        elif row is None:
            if event.type == "report.completed":
                self.status = "done"
            elif event.type == "desk.failed":
                self.status = f"failed: {data.get('error')}"
        elif event.type == "role.started":
            row.status, row.activity = "running", "thinking"
        elif event.type == "role.tool.started":
            row.activity = f"calling {data.get('tool')}"
        elif event.type == "role.model.completed":
            self.input_tokens += int(data.get("input_tokens", 0))
            self.output_tokens += int(data.get("output_tokens", 0))
            cost = data.get("cost_usd")
            self.cost = None if cost is None or self.cost is None else self.cost + cost
        elif event.type == "analyst.completed":
            row.status = "done"
            row.stance = str(data.get("stance", ""))
            row.confidence = data.get("confidence")
            row.activity = str(data.get("summary", ""))
        elif event.type == "debate.turn":
            row.status, row.activity = "done", str(data.get("argument", ""))
        elif event.type == "strategist.idea":
            row.activity = f"{data.get('contract')} → risk {data.get('display')}"
        elif event.type == "role.completed":
            row.status = "done"
            if row.activity in {"thinking", ""} or row.activity.startswith("calling"):
                row.activity = "done"
        elif event.type == "role.failed":
            error = data.get("error") or {}
            row.status, row.activity = "failed", str(error.get("message", "failed"))

    def __rich__(self) -> RenderableType:
        table = Table(show_edge=False, pad_edge=False, expand=True)
        table.add_column("", width=1)
        table.add_column("role", no_wrap=True)
        table.add_column("stance", no_wrap=True)
        table.add_column("conf", justify="right", no_wrap=True)
        table.add_column("activity", overflow="ellipsis", no_wrap=True, ratio=1)
        for name, row in self.rows.items():
            icon, style = ICONS.get(row.status, ("?", ""))
            table.add_row(
                Text(icon, style=style),
                ROLES[name].title if name in ROLES else name,
                Text(row.stance, style=STANCE_STYLE.get(row.stance, "")),
                "" if row.confidence is None else f"{row.confidence:.2f}",
                " ".join(row.activity.split()),  # one line per role, even for long arguments
            )
        cost = "cost n/a" if self.cost is None else f"${self.cost:.4f}"
        header = Text(
            f"SOC Desk · {self.symbol} · {self.profile} · {self.mode or '…'} mode · "
            f"tokens {self.input_tokens:,} in / {self.output_tokens:,} out · {cost} · "
            f"{self.status}",
            style="bold",
        )
        return Group(header, table)
