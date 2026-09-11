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
                   Runtime (native loop / graph runtime)
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

- A runtime protocol with two implementations: a native tool-use loop for single agents, and a graph runtime (LangGraph) for SOC Desk.
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
    name: Literal["community", "socswift"]
    async def quote(self, symbols: list[str]) -> list[Quote]: ...
    async def option_chain(self, symbol: str, expiration: date | None) -> OptionChain: ...
    async def bars(self, symbol: str, interval: str, lookback: int) -> list[Bar]: ...
```

- **Community provider**: pluggable free sources, run on your own machine for personal use. Respect each source's terms. The GEX value is an estimate from open interest and is always labeled "estimate, delayed".
- **SocSwift provider**: an API client for members. No SocSwift logic ships in this repository.
- In Community mode, a member-only tool returns `requires_membership` with one line on what the data would add.

## Data Snapshots

- Every tool result is stored as an immutable snapshot with `as_of`, `mode`, `trust`, and `delayed`.
- Reports cite snapshot ids, which makes every run replayable.
- Member snapshots stored locally follow the retention rule in [Security](security.md#member-data-in-the-cli).

## Storage

- Local: SQLite.
- A hosted version for SocSwift members runs the same package with a server extra inside the SocSwift app.
