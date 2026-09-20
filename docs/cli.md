# CLI and MCP

## Install and Keys

```bash
pip install -e ".[mcp]"             # from a clone; drop [mcp] if you do not need the MCP server
export ANTHROPIC_API_KEY=...        # or OPENAI_API_KEY / GOOGLE_API_KEY
socagents doctor                    # check keys, storage, kill switches, and data sources
```

Local models through Ollama work with every command:

```bash
socagents desk SPY -m ollama/<model> --profile lite
```

## Model Selection

- `-m/--model PROVIDER/MODEL` sets every role.
- `-m ROLE=PROVIDER/MODEL` sets one role and can be repeated, for example `-m desk_lead=anthropic/<model>`.
- Without `-m`: `SOCAGENTS_MODEL`, then `config default_model`. The offline `fixture` provider uses a scripted model and needs no key.

## Data Provider

`--provider community|fixture|socswift`. The default is SocSwift when a member key is stored, otherwise Community (free, delayed). `fixture` is bundled synthetic data for offline demos.

## Commands

Available now:

| Command | Purpose |
|---|---|
| `socagents desk SYMBOL [--profile lite\|standard\|deep] [--rounds N] [-m ...] [--provider ...] [--skill NAME] [--full] [--json] [--no-live]` | run SOC Desk with a live view |
| `socagents replay REF [-m ...] [--profile ...] [--rounds N] [--skill NAME\|none] [--json]` | re-run a stored desk on its recorded data and compare it with the original |
| `socagents ask "QUESTION" [--symbol SYMBOL]` | single-agent answer |
| `socagents brief --symbols A,B` | pre-market briefing |
| `socagents report list` / `report show REF [--full] [--json]` | list and view stored Desk Reports (`REF` is an id or a unique prefix) |
| `socagents report export REF [--format md\|json] [--out FILE]` | export a Community report: regime, key levels, and scenarios only |
| `socagents login` / `logout` / `whoami` | connect a SocSwift member API key (stored in the OS keychain) |
| `socagents config list\|get KEY\|set KEY VALUE` | `default_model`, `default_provider`, `default_profile`, `upsell`, `telemetry` |
| `socagents mcp serve` | local MCP server over stdio |
| `socagents doctor` | environment check |
| `socagents x once [--post] [--symbols A,B] [--max-replies N] [--json]` | one X reply-bot cycle; drafts unless `--post` and `AGENT_SOCIAL=1` (see [X reply bot](x-bot.md)) |
| `socagents x login` / `auth` / `logout` / `status` | X credentials — OAuth 1.0a keys (`login`) or the OAuth 2.0 browser flow (`auth`) — plus cursor and the last 24h |
| `socagents x post "TEXT" [--reply-to ID]` | post one tweet to test the write path; needs `AGENT_SOCIAL=1` |
| `socagents skills list\|show\|enable\|disable\|add PATH\|remove` | manage skills |
| `socagents mcp add NAME -- COMMAND` / `mcp list\|show\|allow\|deny\|verify\|remove` | external MCP servers as read-only desk tools |

## Profiles

| Profile | Analysts | Debate rounds | Strategist and risk review | Who |
|---|---|---|---|---|
| `lite` | dealer positioning, technical | 0 | no | everyone |
| `standard` | dealer positioning, flow, technical, event and news | 1 | yes | everyone |
| `deep` | standard plus futures hedge | 2 | yes | members |

## Economic Calendar

The Community provider has no free calendar source. To give the Event and News analyst scheduled events, keep a list in `~/.socagents/calendar.json`:

```json
[{ "time": "2026-09-11T12:30:00Z", "name": "CPI", "importance": "high" }]
```

## Replay

`socagents replay REF` re-runs a stored desk on the data recorded in the original run, so a different model, profile, or skill sees exactly the same market data. Roles keep the original models unless you set them with `-m`.

```bash
socagents replay rpt_01a08e -m strategist=anthropic/<model>   # one role on another model
socagents replay rpt_01a08e --skill none                       # the same run without skills
```

The output shows the new report and a side-by-side comparison: regime, analyst stances, key levels, ideas, dissent, model calls, and cost.

- A call the original run did not make returns `not_recorded`. Replays never fetch new data.
- Replay ideas are labeled "Replay: recorded data, not risk-checked for execution" and are never convertible.
- Replaying a member run needs an active membership. Replays do not reconnect external MCP servers.

## Skills

A skill is a `SKILL.md` playbook: front matter plus Markdown guidance.

```markdown
---
name: my-playbook
title: My playbook
description: Wait for a retest of the level before any idea triggers.
applies_to: [strategist]
tools: [get_option_quote]
---
Prefer a trigger on a retest of the level, with the invalidation just beyond it.
```

- `applies_to`: roles that receive the skill (`bull`, `bear`, `strategist`, `risk_officer`, `desk_lead`). `tools`: existing read-only tools the skill refers to; skills cannot add tools.
- Official skills ship with the package: `gamma-regime-playbook`, `event-risk-checklist`, `zero-dte-long-premium`. Use one for a run with `--skill NAME`, or for every run with `skills enable NAME`.
- `socagents skills add PATH` copies a user skill to `~/.socagents/skills/`. It stays off until `skills enable NAME`.
- Skills over 8 KB, or with instruction-like text, attempts to override risk rules or sizing, or performance claims, are rejected.
- Reports list the skills used with their SHA-256, and each run records them in the audit log.

## MCP Server

```json
{
  "mcpServers": {
    "socagents": {
      "command": "socagents",
      "args": ["mcp", "serve"],
      "env": { "ANTHROPIC_API_KEY": "...", "SOCAGENTS_MODEL": "anthropic/<model>" }
    }
  }
}
```

| MCP item | Needs a provider key in `env` | Notes |
|---|---|---|
| `run_desk(symbol, profile)` | yes, plus `SOCAGENTS_MODEL` | the multi-agent desk makes its own model calls |
| `desk` prompt | no | your assistant's own model runs a single-model desk with the data tools |
| Community data tools | no | `community_quote`, `community_option_chain`, `community_gex_estimate`, `community_technicals`, `community_flow_estimate`, `community_option_quote`, `community_headlines`, `community_event_calendar` |
| Member data tools | no, but needs `socagents login` | `socswift_gex_snapshot`, `socswift_options_flow`, `socswift_hedge_flow` |

Every tool is read-only. Headlines come back marked as untrusted data.

## Terminal View (Community mode, illustrative)

```text
SOC Desk · SPX · standard · Community mode (data delayed 15m)          tokens 38.2k · $0.31
────────────────────────────────────────────────────────────────────────────────────────────
✓ Dealer Positioning   neutral  0.60   walls 5850C / 5750P · est. zero-gamma 5790
✓ Flow                 bullish  0.55   call volume/OI rising at 5820–5850
✓ Technical            bullish  0.50   above VWAP, higher lows since 11:00
✓ Event and News       neutral  0.40   no scheduled data; 1 untrusted headline ignored
────────────────────────────────────────────────────────────────────────────────────────────
Debate · round 1
  Bull  price above est. flip, call interest building under the 5850 wall
  Bear  put volume rising into the close; wall caps upside
────────────────────────────────────────────────────────────────────────────────────────────
Strategist   educational idea: long SPX 5820C 0DTE on break above 5812
             entry ≈ 4.20 · stop 2.10 · target 6.50 (option premium) · max loss ≈ $210
Risk Officer educational check only: data delayed 14m → not risk-checked for execution
────────────────────────────────────────────────────────────────────────────────────────────
Desk Lead ▸ writing report…
```

Trade ideas appear only in your own terminal. Exported, shared, and public reports never include them.

## External MCP Servers

Give the desk read-only tools from your own MCP servers, for example your broker's positions.

```bash
socagents mcp add mybroker --env BROKER_TOKEN -- npx -y @broker/mcp@1.2.3   # pinned version
socagents mcp show mybroker                  # tools, annotations, and which are blocked
socagents mcp allow mybroker get_positions   # nothing is exposed until you allow it
socagents mcp verify mybroker                # compare with the approved definitions
```

- Use read-only credentials wherever your broker supports them.
- Package runners (`npx`, `uvx`, `pipx run`, `docker`) must be pinned to an exact version or digest, unless you pass `--allow-unpinned`.
- Tools whose name starts with an order verb (`place`, `submit`, `cancel`, `buy`, `sell`, `trade`, and others) are blocked even if you allow them. Tools that do not declare themselves read-only need `--yes-not-read-only`.
- Allowed tools go to the Strategist by default (`--role` to choose). Arguments are limited to symbols, dates, and account aliases, and output is untrusted data.
- If a server changes its tool definitions, it is disabled until you run `socagents mcp verify NAME --approve`. Changed tools must be allowed again.
- Servers get a minimal environment plus the variables named with `--env`. Their stderr goes to `~/.socagents/logs/mcp-NAME.log`, and the configuration to `~/.socagents/mcp_servers.json`.

See [Security](security.md#external-mcp-servers).
