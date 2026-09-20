"""``socagents`` CLI entry point."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import sys
import webbrowser
from collections.abc import Callable
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
from socagents.agents.desk import run_desk, run_replay
from socagents.analytics.regime import REGIME_MODEL_FILENAME, RegimeClassifier
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
from socagents.desk.models import DeskReport
from socagents.desk.render import render_report, shareable_markdown
from socagents.desk.replay import compare_reports
from socagents.desk.roles import PROFILES, profile_roles
from socagents.desk.storage import DeskStore
from socagents.external_mcp import (
    ServerConfig,
    add_server,
    allow_tool,
    clean_description,
    deny_tool,
    load_servers,
    remove_server,
    tool_status,
    verify_server,
)
from socagents.growth import attributed_url
from socagents.providers.fixture import FixtureProvider
from socagents.runtime.native import RunResult
from socagents.runtime.states import RunStatus
from socagents.session import purge_member_data
from socagents.skills import Skill, add_user_skill, discover, lint, remove_user_skill
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
    help="Local MCP server for Claude Desktop and other clients, and external MCP servers as "
    "read-only desk tools.",
    no_args_is_help=True,
)
skills_app = typer.Typer(
    help="Strategy playbooks (SKILL.md) that guide desk roles.", no_args_is_help=True
)
regime_app = typer.Typer(
    help="Research track: the online regime classifier's learning status.",
    no_args_is_help=True,
)
app.add_typer(report_app, name="report")
app.add_typer(config_app, name="config")
app.add_typer(mcp_app, name="mcp")
app.add_typer(skills_app, name="skills")
app.add_typer(regime_app, name="regime")

console = Console()
err_console = Console(stderr=True)

ProviderOption = Annotated[
    str | None,
    typer.Option(
        help="community (free, delayed), fixture (offline sample), or socswift "
        "(members). Default: SocSwift when logged in, else Community."
    ),
]
SkillOption = Annotated[
    list[str] | None,
    typer.Option(
        "--skill",
        help="Apply a skill for this run, on top of enabled ones. Repeatable. User skills must "
        "be enabled first.",
    ),
]
FullOption = Annotated[
    bool,
    typer.Option(
        "--full", help="Show the analysts, debate, evidence ids, and the Risk Officer's notes."
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
    skill: SkillOption = None,
    as_json: Annotated[bool, typer.Option("--json", help="Print the full report as JSON.")] = False,
    live: Annotated[bool, typer.Option("--live/--no-live", help="Live view while running.")] = True,
    full: FullOption = False,
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
            skills=skill or [],
        )

    try:
        if live and not as_json and console.is_terminal:
            with Live(view, console=console, refresh_per_second=8, transient=True):
                report = asyncio.run(go())
        else:
            report = asyncio.run(go())
    except SocAgentsError as exc:
        _fail(exc)
    assert isinstance(report, DeskReport)
    if as_json:
        typer.echo(report.model_dump_json(indent=2))
    else:
        console.print(render_report(report, full=full))


@app.command()
def replay(
    ref: Annotated[str, typer.Argument(help="Desk Report id, desk run id, or a unique prefix.")],
    model: Annotated[
        list[str] | None,
        typer.Option(
            "--model",
            "-m",
            help="PROVIDER/MODEL for every role, or ROLE=PROVIDER/MODEL for one role. Roles "
            "you do not set keep the original run's model.",
        ),
    ] = None,
    profile: Annotated[
        str | None, typer.Option(help="Profile for the replay. Default: the original's.")
    ] = None,
    rounds: Annotated[
        int | None, typer.Option(min=0, max=3, help="Debate rounds. Default: the original's.")
    ] = None,
    skill: Annotated[
        list[str] | None,
        typer.Option(
            "--skill",
            help="Skills for the replay, replacing the original's. Repeatable. "
            "`--skill none` replays without skills.",
        ),
    ] = None,
    as_json: Annotated[bool, typer.Option("--json", help="Print the result as JSON.")] = False,
) -> None:
    """Re-run a desk on the data recorded for an earlier run, e.g. with another model."""
    settings = _settings()

    async def go() -> tuple[DeskReport, DeskReport]:
        return await run_replay(
            ref=ref,
            model_args=model or [],
            settings=settings,
            profile_name=profile,
            rounds=rounds,
            skills=skill,
        )

    try:
        if not as_json and console.is_terminal:
            with console.status("Replaying on recorded data…"):
                original, result = asyncio.run(go())
        else:
            original, result = asyncio.run(go())
    except SocAgentsError as exc:
        _fail(exc)
    rows = compare_reports(original, result)
    if as_json:
        comparison = [{"field": f, "original": a, "replay": b} for f, a, b in rows]
        payload = {
            "original": original.id,
            "replay": result.model_dump(mode="json"),
            "comparison": comparison,
        }
        typer.echo(json.dumps(payload, indent=2))
        return
    console.print(render_report(result))
    table = Table(
        title=f"Replay of {original.id}", title_justify="left", show_edge=False, pad_edge=False
    )
    table.add_column("", no_wrap=True)
    table.add_column("original")
    table.add_column("replay")
    for field_name, before, after in rows:
        table.add_row(field_name, escape(before), escape(after))
    console.print(table)


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
    full: FullOption = False,
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
        console.print(render_report(report, full=full))


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


# skills


def _skill(settings: Settings, name: str) -> Skill:
    available, _ = discover(settings.home)
    skill = available.get(name)
    if skill is None:
        _fail(ConfigError(f"Unknown skill {name!r}. See `socagents skills list`."), 2)
    return skill


@skills_app.command("list")
def skills_list() -> None:
    """List official and user skills, and which are enabled."""
    settings = _settings()
    enabled = _config(settings).skills
    available, problems = discover(settings.home)
    table = Table(show_edge=False, pad_edge=False)
    for column in ("skill", "source", "enabled", "applies to", "status"):
        table.add_column(column, no_wrap=column == "skill")
    for skill in available.values():
        issues = lint(skill)
        table.add_row(
            skill.name,
            skill.source,
            "yes" if skill.name in enabled else "no",
            ", ".join(skill.manifest.applies_to),
            "rejected: " + "; ".join(issues) if issues else "ok",
        )
    console.print(table)
    for problem in problems:
        console.print(Text(problem, style="yellow"))


@skills_app.command("show")
def skills_show(name: str) -> None:
    """Show a skill's manifest and guidance."""
    skill = _skill(_settings(), name)
    manifest = skill.manifest
    header = (
        f"{manifest.title} · {skill.source} · v{manifest.version} · "
        f"sha256:{skill.content_hash[:12]}\n{manifest.description}\n"
        f"Applies to: {', '.join(manifest.applies_to)}. "
        f"Tools referenced: {', '.join(manifest.tools) or 'none'}.\n\n"
    )
    console.print(Panel(Text(header + skill.body), title=skill.name, title_align="left"))


@skills_app.command("enable")
def skills_enable(name: str) -> None:
    """Enable a skill for every desk run."""
    settings = _settings()
    skill = _skill(settings, name)
    issues = lint(skill)
    if issues:
        _fail(ConfigError(f"Skill {name} was rejected: {'; '.join(issues)}."), 2)
    config = _config(settings)
    if name not in config.skills:
        config = config.model_copy(update={"skills": [*config.skills, name]})
        save_user_config(settings.home, config)
    if skill.source == "user":
        console.print(
            "[yellow]User skill: not reviewed by SOCAgents maintainers. It is guidance only, "
            "and the risk engine ignores it.[/yellow]"
        )
    console.print(f"Enabled {name}. It applies to: {', '.join(skill.manifest.applies_to)}.")


@skills_app.command("disable")
def skills_disable(name: str) -> None:
    """Stop applying a skill."""
    settings = _settings()
    config = _config(settings)
    if name in config.skills:
        config = config.model_copy(update={"skills": [s for s in config.skills if s != name]})
        save_user_config(settings.home, config)
    console.print(f"Disabled {name}.")


@skills_app.command("add")
def skills_add(
    path: Annotated[Path, typer.Argument(help="A SKILL.md file, or a folder containing one.")],
) -> None:
    """Add a user skill from a local file. It stays disabled until you enable it."""
    settings = _settings()
    try:
        skill = add_user_skill(settings.home, path)
    except SocAgentsError as exc:
        _fail(exc, 2)
    console.print(
        f"Added user skill {skill.name} (disabled). Review it with `socagents skills show "
        f"{skill.name}`, then `socagents skills enable {skill.name}`."
    )


@skills_app.command("remove")
def skills_remove(name: str) -> None:
    """Delete a user skill."""
    settings = _settings()
    try:
        removed = remove_user_skill(settings.home, name)
    except SocAgentsError as exc:
        _fail(exc, 2)
    if not removed:
        _fail(ConfigError(f"No user skill named {name!r}."), 2)
    config = _config(settings)
    if name in config.skills:
        save_user_config(
            settings.home,
            config.model_copy(update={"skills": [s for s in config.skills if s != name]}),
        )
    console.print(f"Removed {name}.")


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


def _print_tools(server: ServerConfig) -> None:
    table = Table(
        title=f"{server.name} tools", title_justify="left", show_edge=False, pad_edge=False
    )
    for column in ("tool", "allowed", "status", "description"):
        table.add_column(column, no_wrap=column == "tool")
    for tool in server.tools:
        table.add_row(
            escape(tool.name),
            "yes" if tool.name in server.allowlist else "no",
            tool_status(tool),
            escape(clean_description(tool.description)[:80]),
        )
    console.print(table)


@mcp_app.command("add")
def mcp_add(
    name: Annotated[str, typer.Argument(help="Short name for the server, e.g. mybroker.")],
    command: Annotated[
        list[str], typer.Argument(help="The command that starts the server, after --.")
    ],
    env: Annotated[
        list[str] | None,
        typer.Option(
            "--env",
            help="Environment variable to pass to the server, by name. Its value is read when "
            "the server starts and never stored. Repeatable.",
        ),
    ] = None,
    role: Annotated[
        list[str] | None,
        typer.Option(
            "--role", help="Desk role that gets the allowed tools. Repeatable. Default: strategist."
        ),
    ] = None,
    allow_unpinned: Annotated[
        bool,
        typer.Option("--allow-unpinned", help="Accept a package not pinned to an exact version."),
    ] = False,
) -> None:
    """Add an external MCP server over stdio. No tool is exposed until you allow it."""
    settings = _settings()
    try:
        server = asyncio.run(
            add_server(
                settings.home,
                name,
                command[0],
                command[1:],
                env or [],
                role,
                allow_unpinned=allow_unpinned,
            )
        )
    except SocAgentsError as exc:
        _fail(exc)
    _print_tools(server)
    console.print(
        f"Added {name} ({escape(server.package_ref)}). No tool is exposed yet. Allow read tools "
        f"by name: socagents mcp allow {name} TOOL"
    )
    console.print(
        "[yellow]Use read-only credentials for broker servers wherever the broker supports "
        "them.[/yellow]"
    )


@mcp_app.command("list")
def mcp_list() -> None:
    """List external MCP servers and their allowed tools."""
    try:
        servers = load_servers(_settings().home)
    except SocAgentsError as exc:
        _fail(exc, 2)
    if not servers:
        console.print("No external MCP servers. Add one: socagents mcp add NAME -- COMMAND")
        return
    table = Table(show_edge=False, pad_edge=False)
    for column in ("server", "status", "package", "roles", "allowed tools"):
        table.add_column(column, no_wrap=column == "server")
    for server in servers.values():
        package = server.package_ref + ("" if server.pinned else " (unpinned)")
        table.add_row(
            server.name,
            server.status,
            escape(package),
            ", ".join(server.roles),
            ", ".join(server.allowlist) or "none",
        )
    console.print(table)


def _servers_call[T](fn: Callable[[], T]) -> T:
    try:
        return fn()
    except SocAgentsError as exc:
        _fail(exc)


@mcp_app.command("show")
def mcp_show(name: str) -> None:
    """Show a server's tools, their annotations, and which are allowed."""
    servers = _servers_call(lambda: load_servers(_settings().home))
    if name not in servers:
        _fail(ConfigError(f"No external MCP server named {name!r}."), 2)
    _print_tools(servers[name])


@mcp_app.command("allow")
def mcp_allow(
    name: str,
    tool: str,
    yes_not_read_only: Annotated[
        bool,
        typer.Option(
            "--yes-not-read-only",
            help="Confirm a tool that does not declare itself read-only.",
        ),
    ] = False,
) -> None:
    """Expose one tool of a server to the desk. Order tools are always blocked."""
    home = _settings().home
    _servers_call(lambda: allow_tool(home, name, tool, confirm=yes_not_read_only))
    console.print(f"Allowed {name}.{tool}. Its output is treated as untrusted data.")


@mcp_app.command("deny")
def mcp_deny(name: str, tool: str) -> None:
    """Stop exposing a tool."""
    home = _settings().home
    _servers_call(lambda: deny_tool(home, name, tool))
    console.print(f"Removed {name}.{tool} from the allowlist.")


@mcp_app.command("verify")
def mcp_verify(
    name: str,
    approve: Annotated[
        bool, typer.Option("--approve", help="Accept the server's current tool definitions.")
    ] = False,
) -> None:
    """Compare a server's tools with the approved definitions."""
    home = _settings().home
    try:
        server, changes = asyncio.run(verify_server(home, name, approve=approve))
    except SocAgentsError as exc:
        _fail(exc)
    if not changes:
        console.print(f"{name}: tool definitions unchanged. Status: {server.status}.")
        return
    for line in changes:
        console.print(escape(line))
    if approve:
        console.print(
            f"Approved the new definitions. Changed tools left the allowlist; allow them again "
            f"if you still want them. Status: {server.status}."
        )
    else:
        console.print(
            f"[yellow]{name} is disabled until you review and approve: "
            f"socagents mcp verify {name} --approve[/yellow]"
        )


@mcp_app.command("remove")
def mcp_remove(name: str) -> None:
    """Remove an external MCP server."""
    home = _settings().home
    _servers_call(lambda: remove_server(home, name))
    console.print(f"Removed {name}.")


# doctor


@regime_app.command("status")
def regime_status() -> None:
    """Learning status of the online regime classifier: examples learned, rolling accuracy,
    and outcomes still awaiting resolution. Research track: see README's "Research Track:
    Continual Learning"."""
    settings = _settings()
    path = settings.home / REGIME_MODEL_FILENAME
    if not path.is_file():
        console.print(
            "No regime model yet. It's created the first time a desk analyst calls "
            "get_regime_estimate (e.g. `socagents desk SPY`)."
        )
        return
    model = RegimeClassifier.load(path)
    table = Table(show_header=False, show_edge=False, pad_edge=False)
    table.add_column("check", style="bold")
    table.add_column("value")
    table.add_row("Model file", str(path))
    table.add_row("Examples learned", str(model.n_learned))
    outcomes = model.recent_outcomes
    accuracy = model.accuracy
    table.add_row(
        "Rolling accuracy",
        "not enough resolved outcomes yet"
        if accuracy is None
        else f"{accuracy:.0%} over the last {len(outcomes)} outcomes",
    )
    if outcomes:
        sparkline = Text()
        for i, outcome in enumerate(outcomes):
            if i:
                sparkline.append(" ")
            mark, style = ("✓", "green") if outcome.correct else ("✗", "red")
            sparkline.append(mark, style=style)
        table.add_row("Recent outcomes", sparkline)
    pending = model.pending_symbols
    table.add_row("Awaiting outcome", ", ".join(pending) if pending else "none")
    console.print(table)
    console.print(
        "[dim]Research track: an early, unvalidated signal, not a substitute for the "
        "gamma regime call.[/dim]"
    )


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
