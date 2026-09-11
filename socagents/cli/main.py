"""``socagents`` CLI entry point."""

from __future__ import annotations

import asyncio
import os
import sys
from typing import Annotated

import httpx
import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from socagents import __version__
from socagents.agents.ask import run_ask
from socagents.core.config import Settings
from socagents.core.errors import SocAgentsError
from socagents.core.timeutil import fmt_delay, fmt_et
from socagents.providers.fixture import FixtureProvider
from socagents.runtime.native import RunResult
from socagents.runtime.states import RunStatus

app = typer.Typer(
    name="socagents",
    help="SOCAgents: a multi-agent desk for options and futures flow. Not investment advice.",
    no_args_is_help=True,
    add_completion=False,
)
console = Console()
err_console = Console(stderr=True)


def _version(value: bool) -> None:
    if value:
        console.print(__version__)
        raise typer.Exit()


@app.callback()
def root(
    version: Annotated[
        bool,
        typer.Option("--version", callback=_version, is_eager=True, help="Show the version."),
    ] = False,
) -> None:
    """SOCAgents command line."""


def _settings() -> Settings:
    try:
        return Settings.from_env()
    except ValueError as exc:
        err_console.print(f"[red]error[/red] config_error: {exc}")
        raise typer.Exit(2) from exc


@app.command()
def ask(
    question: Annotated[str, typer.Argument(help='Question, e.g. "Where is SPY positioned?"')],
    symbol: Annotated[
        list[str] | None,
        typer.Option(
            "--symbol", "-s", help="Symbol to analyze. Repeatable. Default: from the question."
        ),
    ] = None,
    provider: Annotated[
        str,
        typer.Option(help="Data provider. v0.0 ships only 'fixture' (recorded synthetic data)."),
    ] = "fixture",
    model: Annotated[
        str | None,
        typer.Option(
            help="PROVIDER/MODEL, e.g. anthropic/<model> or ollama/<model>. "
            "Default with the fixture provider: fixture/scripted (offline)."
        ),
    ] = None,
    max_steps: Annotated[int, typer.Option(min=1, max=20, help="Step budget.")] = 6,
    as_json: Annotated[bool, typer.Option("--json", help="Print the run result as JSON.")] = False,
) -> None:
    """Ask a single agent about a symbol. Every number cites a data snapshot."""
    settings = _settings()
    try:
        result = asyncio.run(
            run_ask(
                question=question,
                symbols=symbol or [],
                provider_name=provider,
                model_spec=model,
                settings=settings,
                max_steps=max_steps,
            )
        )
    except SocAgentsError as exc:
        err_console.print(f"[red]error[/red] {exc.code}: {exc}")
        raise typer.Exit(2) from exc

    if as_json:
        typer.echo(result.model_dump_json(indent=2))
    else:
        _render(result, provider)
    if result.status is not RunStatus.COMPLETED:
        raise typer.Exit(1)


def _render(result: RunResult, provider: str) -> None:
    delay = fmt_delay(result.data_delay_sec)
    title = f"ask · {provider} data · {result.mode} mode · data {delay}"
    if result.status is RunStatus.COMPLETED:
        console.print(Panel(Text(result.answer or ""), title=title, title_align="left"))
    else:
        message = result.error.message if result.error else "unknown error"
        code = result.error.code if result.error else result.status.value
        console.print(
            Panel(
                Text(f"{code}: {message}", style="red"),
                title=f"{title} · {result.status.value}",
                title_align="left",
            )
        )

    if result.snapshots:
        table = Table(title="Sources", title_justify="left", show_edge=False, pad_edge=False)
        table.add_column("snapshot")
        table.add_column("tool")
        table.add_column("source")
        table.add_column("as of")
        table.add_column("delay")
        for snap in result.snapshots:
            table.add_row(
                snap.id,
                snap.tool,
                snap.source or "",
                fmt_et(snap.as_of) if snap.as_of else "",
                fmt_delay(snap.delayed_sec),
            )
        console.print(table)

    cost = "unknown" if result.cost_usd is None else f"${result.cost_usd:.4f}"
    console.print(
        f"[dim]model {result.model} · {result.steps} steps · tokens {result.input_tokens} in / "
        f"{result.output_tokens} out · cost {cost} · run {result.run_id}[/dim]"
    )
    console.print("[dim]Not investment advice.[/dim]")


@app.command()
def doctor() -> None:
    """Check the environment: Python, storage, kill switches, keys, and local models."""
    settings = _settings()
    table = Table(show_header=False, show_edge=False, pad_edge=False)
    table.add_column("check", style="bold")
    table.add_column("status")

    py_ok = sys.version_info >= (3, 12)
    table.add_row("Python", f"{sys.version.split()[0]}" + ("" if py_ok else " (needs 3.12+)"))
    table.add_row("SOCAgents", __version__)
    try:
        settings.home.mkdir(parents=True, exist_ok=True)
        probe = settings.home / ".write-test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        table.add_row("Storage", f"{settings.db_path} (writable)")
    except OSError as exc:
        table.add_row("Storage", f"[red]{settings.home} not writable: {exc}[/red]")

    ks = settings.kill_switches
    switches = ", ".join(
        f"{name}={'on' if value else 'off'}"
        for name, value in (
            ("AGENTS", ks.agents),
            ("AGENT_ORDERS", ks.agent_orders),
            ("AGENT_LIVE", ks.agent_live),
            ("AGENT_SCHEDULER", ks.agent_scheduler),
        )
    )
    table.add_row("Kill switches", switches)

    for label, names in (
        ("Anthropic key", ["ANTHROPIC_API_KEY"]),
        ("OpenAI key", ["OPENAI_API_KEY"]),
        ("Google key", ["GOOGLE_API_KEY", "GEMINI_API_KEY"]),
    ):
        table.add_row(label, "set" if any(os.environ.get(n) for n in names) else "not set")

    try:
        response = httpx.get(f"{settings.ollama_base_url.rstrip('/')}/models", timeout=1.0)
        count = len(response.json().get("data", [])) if response.status_code == 200 else None
        status = (
            f"reachable ({count} models)" if count is not None else f"HTTP {response.status_code}"
        )
    except (httpx.HTTPError, ValueError):
        status = "not reachable"
    table.add_row("Ollama", f"{status} at {settings.ollama_base_url}")
    table.add_row("Fixture data", ", ".join(FixtureProvider().available_symbols()) + " (synthetic)")
    console.print(table)


def main() -> None:
    app()


if __name__ == "__main__":
    main()
