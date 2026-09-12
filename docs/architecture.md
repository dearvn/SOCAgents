# Architecture

## Modes

| Mode | Who | Data | Model |
|---|---|---|---|
| Community | anyone | free public data, delayed; optional read-only broker data | your own provider key or a local model |
| Member | SocSwift members | real-time SocSwift data through the SocSwift Agent API with a member API key | your own key in CLI/MCP; SocSwift-hosted inside the SocSwift app |

SocSwift data is available only to SocSwift members.

## Components

```text
             ┌──────────── entry points ────────────┐
             │  CLI (desk, ask, brief, login, mcp)  │   MCP server (run_desk, data tools, desk prompt)
             └───────────────┬──────────────────────┘
                             v
                  Runtime (native loop / desk orchestrator)
                             │
          ┌──────────────────┼─────────────────────────┐
          v                  v                         v
     SOC Desk graph      Model gateway            Tool gateway ── risk engine (pure functions)
     (roles, debate,     (Anthropic, OpenAI,          │
      report schema)      Google, Ollama)             ├── Community provider (free public data, GEX estimate)
                                                      ├── SocSwift provider (member API key)
                                                      ├── External MCP servers (read-only, allowlisted)
                                                      └── Skills (read-only guidance)
                             │
                             v
                Storage: SQLite (local)
                snapshots, desk runs, reports, audit, usage
```

## Runtime

- A native tool-use loop runs each agent. SOC Desk adds a small asyncio orchestrator on top: analysts in parallel, then debate rounds, strategist, risk review, and desk lead. It has no graph-framework dependency.
- Every run has limits on steps, cost, and wall-clock time, and checkpoints after each step.
- Context is assembled in priority order: fixed safety policy, glossary, template instructions, user instructions, memory, trigger, then tool results wrapped as data.

## Model Gateway

- Providers: Anthropic, OpenAI, Google, and OpenAI-compatible or local endpoints such as Ollama.
- Per-role model selection and per-role token budgets.
- Retries, fallback, token and cost accounting on every run.

## Tool Gateway

Every tool call passes through the same pipeline:

```text
schema validation → kill switches → entitlement (member tools) → policy → risk class
→ risk engine (trade ideas and plans) → approval when required → execute
→ audit → normalize and wrap as trusted or untrusted data → snapshot → back to the agent
```

Every tool declares its input and output schema, risk class, timeout, cache TTL, and snapshot policy.

## Data Providers

```python
class MarketDataProvider(Protocol):
    name: str  # "fixture", "community", or "socswift"
    mode: Literal["community", "member"]

    async def quotes(self, symbols: list[str]) -> QuoteSet: ...
    async def option_chain(self, symbol: str, expiration: date | None = None) -> OptionChain: ...
    async def bars(self, symbol: str, interval: str = "5m", lookback: int = 78) -> BarSeries: ...
    async def headlines(self, symbol: str, limit: int = 10) -> HeadlineSet: ...
    async def events(self, hours: int = 48) -> EventSet: ...
```

- **Fixture provider**: bundled synthetic data for tests, CI, and offline demos.
- **Community provider**: free sources, run on your own machine for personal use: Cboe delayed quotes and option chains, Yahoo Finance bars and headlines, and an optional economic calendar you keep in `~/.socagents/calendar.json`. Respect each source's terms. GEX, flow, and technicals are computed locally, and the GEX value is always labeled "estimate, delayed".
- **SocSwift provider**: an API client for members. No SocSwift logic ships in this repository.
- Provider choice: `--provider`, then `SOCAGENTS_PROVIDER`, then `config default_provider`, then SocSwift when a member key is stored, else Community. If a stored key is no longer valid, the run falls back to Community and says so.
- In Community mode, a member-only tool returns `requires_membership` with one line on what the data would add.

## Data Snapshots

- Every tool result is stored as an immutable snapshot with `as_of`, `mode`, `trust`, and `delayed`.
- Reports cite snapshot ids, which makes every run replayable: `socagents replay` serves the recorded snapshots instead of live data.
- Member snapshots stored locally follow the retention rule in [Security](security.md#member-data-in-the-cli).

## Storage

- Local: SQLite.
- A hosted version for SocSwift members runs the same package with a server extra inside the SocSwift app.
