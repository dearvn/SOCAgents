# SOCAgents

**An open-source, multi-agent trading desk for options and futures.**

A team of AI agents reads dealer positioning, options flow, and price action, argues the bull and bear case, and hands you an evidence-cited desk report. It runs free on public data. It gets real-time institutional flow and dealer positioning from [SocSwift](https://socswift.com/?ref=socagents&utm_source=github&utm_medium=readme) when you are a member.

> **Status: pre-alpha.** The design is complete and implementation has started. The commands below describe the planned v0.1 interface. Star or watch the repository to follow progress.

<!-- Demo: docs/assets/desk-demo.gif, a 45-second recording of `socagents desk SPX` with analysts working and debating live. -->

## Why SOCAgents

- Index options move around dealer hedging and 0DTE flow. Generic AI agents read neither.
- SOC Desk is built for SPX, SPY, QQQ, ES, NQ, and US equities with listed options.
- **The LLM proposes. Deterministic code disposes.** Nothing trades without your approval, and a code-based risk engine checks every idea.
- Every number in a report cites the data snapshot it came from.

## Quickstart (planned v0.1)

```bash
pip install socagents

# any supported model provider
export ANTHROPIC_API_KEY=...        # or OPENAI_API_KEY / GOOGLE_API_KEY

socagents desk SPX                  # full desk with a live terminal view
socagents desk QQQ --profile lite   # fewer agents, faster, cheaper
socagents ask "Where is SPX dealer gamma flipping today?"
```

Run fully local with Ollama:

```bash
socagents desk SPY --model ollama/<model> --profile lite
```

## How It Works

```text
               ┌─ Dealer Positioning ─┐
               ├─ Flow                ┤
socagents ────>├─ Futures Hedge       ├──> Bull ⇄ Bear ──> Strategist ──> Risk Officer ──> Desk Lead ──> Desk Report
desk SPX       ├─ Technical           ┤                                  (code first,
               └─ Event and News ─────┘                                   LLM second)
```

- Analysts run in parallel, each with its own tools and model.
- Bull and bear researchers argue from the analysts' evidence.
- The Strategist proposes ideas. The Risk Officer runs deterministic limits first. The Desk Lead writes the report.

## Community vs Member

| | Community (free) | Member (SocSwift) |
|---|---|---|
| Data | delayed public quotes and option chains | real-time SocSwift data |
| Dealer positioning | open-interest GEX estimate | SocSwift GEX engine, regime, expected range |
| Options flow | volume vs open interest changes | live 0–1 DTE and 2+ DTE institutional flow with filters |
| Futures hedging | not available | dealer hedge flow for ES/NQ |
| Desk inside SocSwift, schedules, alerts | no | yes |
| Trade plans to your broker (with approval) | no | yes, on eligible plans |
| Model cost | your own key or local model | your own key in CLI/MCP; included in-app within plan quota |

SocSwift data is available only to SocSwift members. [See plans and trial](https://socswift.com/?ref=socagents&utm_source=github&utm_medium=readme).

## Example Desk Report (Community mode, illustrative numbers)

```text
SOC Desk · SPX · Community mode · data delayed · 2026-09-10 13:45 ET

Regime       positive gamma (estimate), mean-reverting
Key levels   5850 call OI wall · 5790 est. zero-gamma · 5750 put OI wall
Base case    holds above 5790 → range 5800–5850
Bear case    3m close below 5790 → expansion toward 5750
Idea         long SPX 5820C 0DTE on a break above 5812
             stop 2.10 · target 6.50 · max loss $210 · risk officer: pass
Dissent      bear researcher: put volume rising into the close
Sources      option chain 13:45 ET (delayed) · 5m bars 13:45 ET

Member data would add live 0DTE institutional flow, SocSwift's GEX regime, and dealer hedge flow.
Not investment advice.
```

## Use It From Claude Desktop or Any MCP Client

```json
{
  "mcpServers": {
    "socagents": {
      "command": "socagents",
      "args": ["mcp", "serve"]
    }
  }
}
```

Then ask your assistant: "Run the SOC Desk on SPX." Members add their SocSwift API key with `socagents login`.

## Planned v0.1 Features

- SOC Desk with lite, standard, and deep profiles
- Live terminal view of the desk
- Community data provider: delayed quotes, option chains, GEX estimate, technicals
- SocSwift data for members through the SocSwift Agent API
- CLI and MCP server
- Any model: Anthropic, OpenAI, Google, or local models through Ollama
- Deterministic risk engine and evidence-cited reports
- Markdown and JSON export of Community reports

## Roadmap

| Phase | Scope |
|---|---|
| P0 | Foundation: runtime, model gateway, tool SDK, data providers |
| P1a | Public launch: SOC Desk, Community mode, member data, CLI, MCP |
| P1b | Ecosystem: hosted MCP, external MCP tools, skills, TradingAgents plugin, replay |
| P2 | Desk inside SocSwift, schedules, alerts, memory |
| P3 | Trade plans to SocSwift pre-orders on SIM, with approval |
| P4–P5 | Supervised SIM automation; live trading with approval after legal review |

## Documentation

User and developer documentation will be published with v0.1.

## Contributing

New data providers, analyst roles, and strategy skills are the most wanted contributions. See [CONTRIBUTING.md](CONTRIBUTING.md).

## Security

Please report vulnerabilities privately. See [SECURITY.md](SECURITY.md).

## Disclaimer

SOCAgents is software for research and education. It is not investment advice, and nothing it produces is a recommendation to buy or sell any security. AI agents can be wrong, use stale data, and behave unexpectedly. Options and futures involve substantial risk of loss. You are responsible for your own trading decisions.

## License

[Apache License 2.0](LICENSE).
