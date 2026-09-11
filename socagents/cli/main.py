"""``socagents`` CLI entry point."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import sys
import webbrowser
from pathlib import Path
from typing import Annotated, NoReturn

import httpx
import typer
from rich.console import Console
from rich.live import Live
from rich.markup import escape
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from socagents import __version__
from socagents.agents.ask import run_ask
from socagents.agents.desk import run_desk
from socagents.core.config import Settings
from socagents.core.credentials import delete_api_key, key_source, load_api_key, save_api_key
from socagents.core.crypto import PayloadCipher, delete_data_key
from socagents.core.errors import ConfigError, ProviderError, SocAgentsError
from socagents.core.timeutil import fmt_delay, fmt_et
from socagents.core.userconfig import (
    UserConfig,
    load_user_config,
    save_user_config,
    set_config_value,
)
from socagents.db.store import Store
from socagents.desk.live import DeskLiveView
from socagents.desk.render import render_report, shareable_markdown
from socagents.desk.roles import PROFILES, profile_roles
from socagents.desk.storage import DeskStore
from socagents.growth import attributed_url
from socagents.providers.fixture import FixtureProvider
from socagents.runtime.native import RunResult
from socagents.runtime.states import RunStatus
from socagents.session import purge_member_data
from socagents.socswift_client.client import MemberInfo, MembershipError, SocSwiftClient

app = typer.Typer(
    name="socagents",
    help="SOCAgents: a multi-agent desk for options and futures flow. Not investment advice.",
    no_args_is_help=True,
    add_completion=False,
)
report_app = typer.Typer(help="View and export Desk Reports.", no_args_is_help=True)
config_app = typer.Typer(help="Read and change settings.", no_args_is_help=True)
mcp_app = typer.Typer(
    help="Local MCP server for Claude Desktop and other clients.", no_args_is_help=True
)
app.add_typer(report_app, name="report")
app.add_typer(config_app, name="config")
app.add_typer(mcp_app, name="mcp")

console = Console()
err_console = Console(stderr=True)

ProviderOption = Annotated[
    str | None,
    typer.Option(
        help="community (free, delayed), fixture (offline sample), or socswift "
        "(members). Default: SocSwift when logged in, else Community."
    ),
]
ModelOption = Annotated[
    str | None,
    typer.Option(
        help="PROVIDER/MODEL, e.g. anthropic/<model> or ollama/<model>. Default: "
        "SOCAGENTS_MODEL or `config default_model`; fixture/scripted with fixture data."
    ),
]


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


def _fail(exc: SocAgentsError | ValueError, exit_code: int | None = None) -> NoReturn:
    code = exc.code if isinstance(exc, SocAgentsError) else "config_error"
    err_console.print(f"[red]error[/red] {code}: {escape(str(exc))}")
    if exit_code is None:
        exit_code = 2 if isinstance(exc, ConfigError | ValueError) else 1
    raise typer.Exit(exit_code)


def _settings() -> Settings:
    try:
        return Settings.from_env()
    except ValueError as exc:
        _fail(exc, 2)


def _config(settings: Settings) -> UserConfig:
    try:
        return load_user_config(settings.home)
    except SocAgentsError as exc:
        _fail(exc, 2)


# ask and brief


def _render_run(result: RunResult, kind: str) -> None:
    title = f"{kind} · {result.mode} mode · data {fmt_delay(result.data_delay_sec)}"
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
        for column in ("snapshot", "tool", "source", "as of", "delay"):
            table.add_column(column)
        for snap in result.snapshots:
            table.add_row(
                snap.id,
                snap.tool,
                snap.source or "",
                fmt_et(snap.as_of) if snap.as_of else "",
                fmt_delay(snap.delayed_sec),
            )
        console.print(table)
    for notice in result.notices:
        console.print(Text(notice, style="yellow"))
    cost = "unknown" if result.cost_usd is None else f"${result.cost_usd:.4f}"
    console.print(
        f"[dim]model {result.model} · {result.steps} steps · tokens {result.input_tokens} in / "
        f"{result.output_tokens} out · cost {cost} · run {result.run_id}[/dim]"
    )
    console.print("[dim]Not investment advice.[/dim]")


def _run_agent(
    kind: str,
    question: str,
    symbols: list[str],
    provider: str | None,
    model: str | None,
    max_steps: int,
    as_json: bool,
) -> None:
    settings = _settings()
    try:
        result = asyncio.run(
            run_ask(
                question=question,
                symbols=symbols,
                provider_name=provider,
                model_spec=model,
                settings=settings,
                max_steps=max_steps,
                kind=kind,
            )
        )
    except SocAgentsError as exc:
        _fail(exc)
    if as_json:
        typer.echo(result.model_dump_json(indent=2))
    else:
        _render_run(result, kind)
    if result.status is not RunStatus.COMPLETED:
        raise typer.Exit(1)


@app.command()
def ask(
    question: Annotated[str, typer.Argument(help='Question, e.g. "Where is SPY positioned?"')],
    symbol: Annotated[
        list[str] | None,
        typer.Option(
            "--symbol", "-s", help="Symbol to analyze. Repeatable. Default: from the question."
        ),
    ] = None,
    provider: ProviderOption = None,
    model: ModelOption = None,
    max_steps: Annotated[int, typer.Option(min=1, max=20, help="Step budget.")] = 6,
    as_json: Annotated[bool, typer.Option("--json", help="Print the run result as JSON.")] = False,
) -> None:
    """Ask a single agent about a symbol. Every number cites a data snapshot."""
    _run_agent("ask", question, symbol or [], provider, model, max_steps, as_json)


@app.command()
def brief(
    symbols: Annotated[str, typer.Option("--symbols", help="Comma-separated, e.g. SPY,QQQ.")],
    provider: ProviderOption = None,
    model: ModelOption = None,
    as_json: Annotated[bool, typer.Option("--json", help="Print the run result as JSON.")] = False,
) -> None:
    """Pre-market briefing: positioning, key levels, and event risk per symbol."""
    wanted = [s.strip() for s in symbols.split(",") if s.strip()]
    question = "Write a pre-market briefing for each symbol."
    _run_agent("brief", question, wanted, provider, model, 10, as_json)


# desk


@app.command()
def desk(
    symbol: Annotated[str, typer.Argument(help="Symbol, e.g. SPY, QQQ, or SPX.")],
    profile: Annotated[
        str | None, typer.Option(help="lite, standard, or deep. Default: config default_profile.")
    ] = None,
    rounds: Annotated[
        int | None, typer.Option(min=0, max=3, help="Debate rounds. Default: from the profile.")
    ] = None,
    model: Annotated[
        list[str] | None,
        typer.Option(
            "--model",
            "-m",
            help="PROVIDER/MODEL for every role, or ROLE=PROVIDER/MODEL for one role. Repeatable.",
        ),
    ] = None,
    provider: ProviderOption = None,
    as_json: Annotated[bool, typer.Option("--json", help="Print the full report as JSON.")] = False,
    live: Annotated[bool, typer.Option("--live/--no-live", help="Live view while running.")] = True,
) -> None:
    """Run SOC Desk: analysts, bull/bear debate, strategist, risk engine, and desk lead."""
    settings = _settings()
    profile_name = profile or _config(settings).default_profile
    if profile_name not in PROFILES:
        _fail(
            ConfigError(f"Unknown profile {profile_name!r}. Choose one of: {', '.join(PROFILES)}.")
        )
    chosen = PROFILES[profile_name]
    view = DeskLiveView(
        symbol.upper(),
        profile_name,
        profile_roles(chosen, chosen.debate_rounds if rounds is None else rounds),
    )

    async def go() -> object:
        return await run_desk(
            symbol=symbol,
            profile_name=profile_name,
            rounds=rounds,
            model_args=model or [],
            provider_name=provider,
            settings=settings,
            on_event=view.handle,
        )

    try:
        if live and not as_json and console.is_terminal:
            with Live(view, console=console, refresh_per_second=8, transient=True):
                report = asyncio.run(go())
        else:
            report = asyncio.run(go())
    except SocAgentsError as exc:
        _fail(exc)
    from socagents.desk.models import DeskReport

    assert isinstance(report, DeskReport)
    if as_json:
        typer.echo(report.model_dump_json(indent=2))
    else:
        console.print(render_report(report))


# reports


def _desk_store(settings: Settings) -> tuple[Store, DeskStore]:
    store = Store(settings.db_path, cipher=PayloadCipher.from_keyring(create=False))
    return store, DeskStore(store)


@report_app.command("list")
def report_list(limit: Annotated[int, typer.Option(min=1, max=200)] = 20) -> None:
    """List recent Desk Reports."""
    store, desk_store = _desk_store(_settings())
    try:
        rows = desk_store.list_reports(limit)
    finally:
        store.close()
    table = Table(show_edge=False, pad_edge=False)
    table.add_column("report", no_wrap=True)
    for column in ("symbol", "mode", "profile", "data as of", "cost"):
        table.add_column(column)
    for row in rows:
        cost = "" if row["cost_usd"] is None else f"${row['cost_usd']:.4f}"
        table.add_row(row["id"], row["symbol"], row["mode"], row["profile"], row["as_of"], cost)
    console.print(table if rows else "No reports yet. Run `socagents desk SPY`.")


@report_app.command("show")
def report_show(
    ref: Annotated[str, typer.Argument(help="Report id, desk run id, or a unique prefix.")],
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Show a Desk Report."""
    store, desk_store = _desk_store(_settings())
    try:
        report = desk_store.get_report(ref)
    finally:
        store.close()
    if report is None:
        _fail(ConfigError(f"No single report matches {ref!r}.", code="report_not_found"))
    if as_json:
        typer.echo(report.model_dump_json(indent=2))
    else:
        console.print(render_report(report))


@report_app.command("export")
def report_export(
    ref: Annotated[str, typer.Argument(help="Report id, desk run id, or a unique prefix.")],
    fmt: Annotated[str, typer.Option("--format", help="md or json.")] = "md",
    out: Annotated[Path | None, typer.Option(help="Write to a file instead of stdout.")] = None,
) -> None:
    """Export a Community report: regime, key levels, and scenarios only. Ideas are never
    exported, and member reports cannot be exported."""
    if fmt not in {"md", "json"}:
        _fail(ConfigError("--format must be md or json."))
    store, desk_store = _desk_store(_settings())
    try:
        report = desk_store.get_report(ref)
    finally:
        store.close()
    if report is None:
        _fail(ConfigError(f"No single report matches {ref!r}.", code="report_not_found"))
    try:
        view = report.shareable_view()
    except SocAgentsError as exc:
        _fail(exc, 2)
    text = shareable_markdown(view) if fmt == "md" else json.dumps(view, indent=2) + "\n"
    if out is None:
        typer.echo(text, nl=False)
    else:
        out.write_text(text, encoding="utf-8")
        console.print(f"Wrote {out} (regime, levels, and scenarios only).")


# membership


async def _check_key(key: str) -> MemberInfo:
    client = SocSwiftClient(key)
    try:
        return await client.me()
    finally:
        await client.aclose()


@app.command()
def login(
    api_key: Annotated[
        str | None,
        typer.Option(
            "--api-key", help="Key to store. Prefer the prompt: flags end up in shell history."
        ),
    ] = None,
    no_browser: Annotated[bool, typer.Option("--no-browser")] = False,
    verify: Annotated[
        bool, typer.Option("--verify/--no-verify", help="Check the key with SocSwift first.")
    ] = True,
) -> None:
    """Connect a SocSwift member API key (stored in the OS keychain)."""
    url = attributed_url("cli", "login")
    console.print(f"Create a scoped read-only API key in SocSwift: {url}")
    if not no_browser:
        webbrowser.open(url)
    key = api_key or typer.prompt("Paste your SocSwift API key", hide_input=True)
    if verify:
        try:
            member = asyncio.run(_check_key(key))
        except MembershipError as exc:
            _fail(exc, 2)
        except ProviderError as exc:
            _fail(
                ProviderError(f"{exc} Use --no-verify to store the key anyway.", code=exc.code), 1
            )
        console.print(
            f"Verified: plan {member.plan or 'unknown'}, "
            f"entitlements {', '.join(member.entitlements) or 'none'}."
        )
    try:
        save_api_key(key)
    except SocAgentsError as exc:
        _fail(exc, 2)
    console.print("Saved to the OS keychain. Member mode is on.")


@app.command()
def logout() -> None:
    """Remove the SocSwift API key and purge member data stored by the CLI."""
    settings = _settings()
    removed = delete_api_key()
    snapshots, reports = purge_member_data(settings)
    delete_data_key()
    console.print(
        f"{'Removed the API key' if removed else 'No stored API key'}; purged {snapshots} "
        f"member snapshot(s) and {reports} member desk run(s). Community mode is on."
    )
    if key_source() == "env":
        console.print("[yellow]SOCSWIFT_API_KEY is still set in the environment.[/yellow]")


@app.command()
def whoami() -> None:
    """Show the current mode and SocSwift membership."""
    key = load_api_key()
    if not key:
        console.print("Community mode (free, delayed data). Members: `socagents login`.")
        return
    try:
        member = asyncio.run(_check_key(key))
    except SocAgentsError as exc:
        _fail(exc, 1)
    console.print(
        f"Member mode · plan {member.plan or 'unknown'} · entitlements "
        f"{', '.join(member.entitlements) or 'none'} · key from {key_source()}"
    )


# config


@config_app.command("list")
def config_list() -> None:
    """Show all settings."""
    config = _config(_settings())
    for key, value in config.model_dump().items():
        console.print(f"{key} = {value}")


@config_app.command("get")
def config_get(key: str) -> None:
    """Show one setting."""
    config = _config(_settings())
    if key not in UserConfig.model_fields:
        _fail(ConfigError(f"Unknown setting {key!r}."))
    console.print(str(getattr(config, key)))


@config_app.command("set")
def config_set(key: str, value: str) -> None:
    """Change one setting, e.g. `socagents config set upsell false`."""
    settings = _settings()
    try:
        config = set_config_value(_config(settings), key, value)
    except SocAgentsError as exc:
        _fail(exc, 2)
    path = save_user_config(settings.home, config)
    console.print(f"{key} = {getattr(config, key)} ({path})")


# mcp


@mcp_app.command("serve")
def mcp_serve() -> None:
    """Run the local MCP server over stdio."""
    try:
        from socagents.mcp.server import serve
    except ImportError as exc:
        _fail(
            ConfigError(
                f"The MCP server needs the mcp extra: pip install 'socagents[mcp]' ({exc.name})."
            ),
            2,
        )
    serve()


# doctor


@app.command()
def doctor() -> None:
    """Check the environment: Python, storage, kill switches, keys, and data sources."""
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
    table.add_row(
        "Kill switches",
        ", ".join(
            f"{name}={'on' if value else 'off'}"
            for name, value in (
                ("AGENTS", ks.agents),
                ("AGENT_ORDERS", ks.agent_orders),
                ("AGENT_LIVE", ks.agent_live),
                ("AGENT_SCHEDULER", ks.agent_scheduler),
            )
        ),
    )
    for label, names in (
        ("Anthropic key", ["ANTHROPIC_API_KEY"]),
        ("OpenAI key", ["OPENAI_API_KEY"]),
        ("Google key", ["GOOGLE_API_KEY", "GEMINI_API_KEY"]),
    ):
        table.add_row(label, "set" if any(os.environ.get(n) for n in names) else "not set")
    source = key_source()
    table.add_row("SocSwift key", f"stored ({source})" if source else "not set (Community mode)")
    try:
        config = load_user_config(settings.home)
        table.add_row(
            "Default model", config.default_model or os.environ.get("SOCAGENTS_MODEL") or "not set"
        )
    except SocAgentsError as exc:
        table.add_row("Config", f"[red]{escape(str(exc))}[/red]")

    try:
        response = httpx.get(f"{settings.ollama_base_url.rstrip('/')}/models", timeout=1.0)
        count = len(response.json().get("data", [])) if response.status_code == 200 else None
        status = (
            f"reachable ({count} models)" if count is not None else f"HTTP {response.status_code}"
        )
    except (httpx.HTTPError, ValueError):
        status = "not reachable"
    table.add_row("Ollama", f"{status} at {settings.ollama_base_url}")
    table.add_row(
        "MCP extra",
        "installed"
        if importlib.util.find_spec("mcp")
        else "not installed (pip install 'socagents[mcp]')",
    )
    table.add_row("Fixture data", ", ".join(FixtureProvider().available_symbols()) + " (synthetic)")
    console.print(table)


def main() -> None:
    app()


if __name__ == "__main__":
    main()
