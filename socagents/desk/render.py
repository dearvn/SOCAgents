"""Render Desk Reports for the terminal and as Markdown."""

from __future__ import annotations

from typing import Any

from rich.console import Group, RenderableType
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from socagents.core.timeutil import fmt_delay, fmt_et
from socagents.desk.models import DeskReport, ReviewedIdea
from socagents.desk.roles import ROLES

STANCE_STYLE = {"bullish": "green", "bearish": "red", "neutral": "yellow"}
DISPLAY_STYLE = {"pass": "green", "fix": "yellow", "reject": "red"}


def _price(value: float | None) -> str:
    return "n/a" if value is None else f"{value:,.2f}"


def _idea_text(item: ReviewedIdea) -> Text:
    idea, check = item.idea, item.risk_check
    entry = idea.entry
    trigger = (
        f"on {entry.condition.replace('_', ' ')} {entry.trigger_price:g}"
        if entry.trigger_price is not None
        else "at market"
    )
    text = Text()
    text.append(f"{idea.structure.replace('_', ' ')} {idea.contract} {trigger}\n", style="bold")
    basis = "option premium" if idea.price_basis == "premium" else "share price"
    text.append(
        f"entry ≈ {_price(idea.est_entry_premium)} · stop {_price(idea.stop)} · target "
        f"{_price(idea.target)} ({basis}) · max loss "
        f"{'n/a' if check.max_loss_usd is None else f'${check.max_loss_usd:,.0f}'}\n"
    )
    text.append(f"invalidation: {idea.invalidation}\n")
    text.append("risk: ")
    text.append(f"{check.display} ({check.mode})", style=DISPLAY_STYLE[check.display])
    for reason in check.reasons:
        text.append(f"\n  - {reason.message}")
    if check.critique:
        text.append(f"\nrisk officer: {check.critique}", style="dim")
    if item.label:
        text.append(f"\n{item.label}", style="yellow")
    if not item.convertible and item.not_convertible_reason:
        text.append(f"\nnot convertible: {item.not_convertible_reason}", style="dim")
    return text


def render_report(report: DeskReport) -> RenderableType:
    delay = "delayed" if report.data_freshness.delayed else "real-time"
    title = (
        f"SOC Desk · {report.symbol} · {report.profile} · {report.mode} mode · data {delay} · "
        f"as of {fmt_et(report.as_of)}"
    )
    parts: list[RenderableType] = [
        Panel(Text(f"Regime: {report.regime}\n\n{report.summary}"), title=title, title_align="left")
    ]

    if report.key_levels:
        levels = Table(title="Key levels", title_justify="left", show_edge=False, pad_edge=False)
        levels.add_column("price", justify="right")
        levels.add_column("level")
        levels.add_column("evidence", style="dim")
        for level in sorted(report.key_levels, key=lambda lvl: lvl.price, reverse=True):
            levels.add_row(_price(level.price), level.kind, ", ".join(level.evidence[:2]))
        parts.append(levels)

    if report.scenarios:
        scenarios = Table(title="Scenarios", title_justify="left", show_edge=False, pad_edge=False)
        scenarios.add_column("case")
        scenarios.add_column("if")
        scenarios.add_column("then")
        for scenario in report.scenarios:
            scenarios.add_row(scenario.name, scenario.condition, scenario.path)
        parts.append(scenarios)

    analysts = Table(title="Analysts", title_justify="left", show_edge=False, pad_edge=False)
    analysts.add_column("role")
    analysts.add_column("stance")
    analysts.add_column("conf", justify="right")
    analysts.add_column("summary")
    for a in report.analysts:
        analysts.add_row(
            ROLES[a.role].title if a.role in ROLES else a.role,
            Text(a.stance, style=STANCE_STYLE[a.stance]),
            f"{a.confidence:.2f}",
            a.summary,
        )
    parts.append(analysts)

    if report.debate:
        debate = Text()
        for turn in report.debate:
            debate.append(f"R{turn.round} {turn.side}: ", style="bold")
            debate.append(f"{turn.argument}\n")
        parts.append(Panel(debate, title="Debate", title_align="left"))

    if report.ideas:
        for item in report.ideas:
            parts.append(Panel(_idea_text(item), title="Idea", title_align="left"))
    elif report.no_trade_reason:
        parts.append(Text(f"No trade: {report.no_trade_reason}"))

    parts.append(Text(f"Dissent: {report.dissent}"))
    if report.missing_roles:
        parts.append(Text(f"Missing roles: {', '.join(report.missing_roles)}", style="red"))
    if report.removed_unverified:
        parts.append(
            Text("Removed unverified numbers: " + "; ".join(report.removed_unverified), style="dim")
        )
    for notice in report.notices:
        parts.append(Text(notice, style="yellow"))
    cost = "unknown" if report.usage.cost_usd is None else f"${report.usage.cost_usd:.4f}"
    parts.append(
        Text(
            f"{report.usage.model_calls} model calls · tokens {report.usage.input_tokens:,} in / "
            f"{report.usage.output_tokens:,} out · cost {cost} · oldest data "
            f"{fmt_delay(report.data_freshness.oldest_snapshot_sec)} · report {report.id}",
            style="dim",
        )
    )
    parts.append(Text(report.disclaimer, style="dim"))
    return Group(*parts)


def shareable_markdown(view: dict[str, Any]) -> str:
    """Markdown for a shareable view (regime, levels, scenarios only)."""
    lines = [
        f"# SOC Desk · {view['symbol']}",
        "",
        f"As of {view['as_of']} · {view['mode']} mode · profile {view['profile']} · "
        f"data {'delayed' if view['data_freshness']['delayed'] else 'real-time'}",
        "",
        f"**Regime:** {view['regime']}",
        "",
        "## Key levels",
        "",
        "| Price | Level |",
        "|---:|---|",
    ]
    for level in sorted(view["key_levels"], key=lambda lvl: lvl["price"], reverse=True):
        lines.append(f"| {level['price']:,.2f} | {level['kind']} |")
    lines += ["", "## Scenarios", "", "| Case | If | Then |", "|---|---|---|"]
    for scenario in view["scenarios"]:
        lines.append(f"| {scenario['name']} | {scenario['condition']} | {scenario['path']} |")
    lines += ["", f"_{view['disclaimer']}_", "", view["made_with"], ""]
    return "\n".join(lines)
