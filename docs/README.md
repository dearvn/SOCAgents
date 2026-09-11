# SOCAgents Design Docs

Public design documents for SOCAgents. The project is pre-alpha, so these describe the planned v0.1 design and may change.

| Document | What it covers |
|---|---|
| [Architecture](architecture.md) | package components, Community and Member modes, data providers, model gateway, tool gateway, storage |
| [SOC Desk](soc-desk.md) | the multi-agent desk: roles, flow, profiles, Desk Report, units and visibility rules |
| [Security](security.md) | threat model, risk engine, prompt injection, external MCP servers, skills, member data handling |
| [CLI and MCP](cli.md) | commands, model selection, MCP server setup, external MCP tools, skills |

Principles that apply everywhere:
- **The LLM proposes. Deterministic code disposes.** A code-based risk engine checks every idea, and nothing trades without approval.
- **Every number cites its data.** Reports link each level and scenario to the data snapshot it came from.
- **Community mode stands on its own.** It is useful with free public data and your own model, with no account.
- **No hidden telemetry.** Any analytics are opt-in.

Not investment advice. See the [disclaimer](../README.md#disclaimer).
