# CLI and MCP

Planned v0.1 interface.

## Install and Keys

```bash
pip install socagents
export ANTHROPIC_API_KEY=...        # or OPENAI_API_KEY / GOOGLE_API_KEY
socagents doctor                    # check keys, providers, network, data delay
```

Local models through Ollama work with every command:

```bash
socagents desk SPY --model ollama/<model> --profile lite
```

## Model Selection

- `--model PROVIDER/MODEL` sets every role.
- `--model ROLE=PROVIDER/MODEL` sets one role and can be repeated, for example `--model desk_lead=anthropic/<model>`.

## Commands

| Command | Purpose |
|---|---|
| `socagents desk SYMBOL [--profile lite\|standard\|deep] [--rounds N] [--model ...] [--skill NAME] [--json\|--md]` | run SOC Desk with a live view; export the report |
| `socagents ask "QUESTION" [--symbol SYMBOL]` | single-agent answer |
| `socagents brief --symbols A,B` | market briefing |
| `socagents replay DESK_RUN_ID [--model ...]` | re-run a desk on stored snapshots |
| `socagents report show\|export\|share DESK_REPORT_ID` | view, export, or share (Community only, ideas stripped) |
| `socagents login` / `logout` / `whoami` | connect a SocSwift member API key (stored in the OS keychain) |
| `socagents mcp serve` | local MCP server |
| `socagents mcp add NAME -- COMMAND` / `mcp allow NAME TOOL` / `mcp list` / `mcp remove NAME` | external MCP servers as read-only tools |
| `socagents skills list\|enable\|disable\|add PATH` | manage skills |
| `socagents config set KEY VALUE` | models, budgets, `upsell`, telemetry opt-in |

## MCP Server

```json
{
  "mcpServers": {
    "socagents": {
      "command": "socagents",
      "args": ["mcp", "serve"],
      "env": { "ANTHROPIC_API_KEY": "..." }
    }
  }
}
```

| MCP item | Needs a provider key in `env` | Notes |
|---|---|---|
| `run_desk(symbol, profile)` | yes | the multi-agent desk makes its own model calls |
| `desk` prompt | no | your assistant's own model runs a single-model desk with the data tools |
| Community data tools | no | quotes, option chain, GEX estimate, technicals |
| Member data tools | no, but needs `socagents login` | SocSwift data for members |

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

```bash
socagents mcp add mybroker -- <broker-mcp-command>   # use read-only credentials
socagents mcp list                                    # shows tools and their read-only annotations
socagents mcp allow mybroker get_positions
```

Order-placing tools are blocked even if allowed. See [Security](security.md#external-mcp-servers).
