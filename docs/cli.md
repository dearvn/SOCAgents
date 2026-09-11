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
| `socagents desk SYMBOL [--profile lite\|standard\|deep] [--rounds N] [-m ...] [--provider ...] [--json] [--no-live]` | run SOC Desk with a live view |
| `socagents ask "QUESTION" [--symbol SYMBOL]` | single-agent answer |
| `socagents brief --symbols A,B` | pre-market briefing |
| `socagents report list` / `report show REF [--json]` | list and view stored Desk Reports (`REF` is an id or a unique prefix) |
| `socagents report export REF [--format md\|json] [--out FILE]` | export a Community report: regime, key levels, and scenarios only |
| `socagents login` / `logout` / `whoami` | connect a SocSwift member API key (stored in the OS keychain) |
| `socagents config list\|get KEY\|set KEY VALUE` | `default_model`, `default_provider`, `default_profile`, `upsell`, `telemetry` |
| `socagents mcp serve` | local MCP server over stdio |
| `socagents doctor` | environment check |

Planned:

| Command | Purpose |
|---|---|
| `socagents replay DESK_RUN_ID [-m ...]` | re-run a desk on stored snapshots |
| `socagents mcp add NAME -- COMMAND` / `mcp allow NAME TOOL` / `mcp list` / `mcp remove NAME` | external MCP servers as read-only tools |
| `socagents skills list\|enable\|disable\|add PATH` | manage skills |

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

## External MCP Servers (planned)

```bash
socagents mcp add mybroker -- <broker-mcp-command>   # use read-only credentials
socagents mcp list                                    # shows tools and their read-only annotations
socagents mcp allow mybroker get_positions
```

Order-placing tools are blocked even if allowed. See [Security](security.md#external-mcp-servers).
