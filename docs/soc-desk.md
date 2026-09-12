# SOC Desk

A team of specialized agents that analyze a symbol, argue both sides, and deliver an evidence-cited Desk Report: regime, key levels, scenarios, and trade ideas with invalidation.

## Roles

| Role | Community inputs | Member inputs | Output |
|---|---|---|---|
| Dealer Positioning Analyst | open-interest GEX estimate from a delayed chain | SocSwift GEX engine, regime, expected range | gamma regime, walls, zero-gamma flip |
| Flow Analyst | volume vs open interest changes | live institutional options flow | where money is positioning |
| Futures Hedge Analyst | not available | dealer hedge flow for ES/NQ (`deep` profile) | dealer hedging pressure |
| Technical Analyst | free bars, VWAP, levels | intraday bars | trend, levels, momentum |
| Event and News Analyst | economic calendar, headlines (untrusted) | same | event risk for the session |
| Bull and Bear Researchers | analyst reports | analyst reports | strongest case for each side |
| Strategist | debate and skills | debate and skills | trade ideas (educational in Community mode) |
| Risk Officer | educational check only (delayed data) | deterministic risk engine, then LLM critique | allow or deny with reasons, shown as pass / fix / reject |
| Desk Lead | everything above | everything above | the Desk Report |

## Flow

```text
                 ┌─ Dealer Positioning ─┐
                 ├─ Flow                ┤
trigger ────────>├─ Futures Hedge *     ├──> Bull ⇄ Bear (rounds by profile) ──> Strategist
                 ├─ Technical           ┤                                           │
                 └─ Event and News ─────┘                                           v
                                                              Risk Officer (deterministic engine first)
                                                                                    │
                                                                                    v
                                                                        Desk Lead ──> Desk Report
```

\* `deep` profile, members.

- Analysts run in parallel, each with its own tools, model, and token budget.
- Every analyst returns a structured report: stance, key levels, signals, confidence, evidence.
- The risk engine runs before any LLM critique. LLM critique is advisory.

## Profiles

| Profile | Roles | Debate rounds | Model calls | Use |
|---|---|---|---|---|
| `lite` | Dealer Positioning, Technical, Desk Lead; risk engine only | 0 | 3–4 | local models, quick checks |
| `standard` | four analysts, Bull, Bear, Strategist, Risk Officer, Desk Lead | 1 | about 10 | default |
| `deep` | `standard` plus Futures Hedge and a second-opinion pass | 2 | 14–18 | members |

`--rounds N` overrides the profile's debate rounds. Each role can use a different model, for example a local model for analysts and a cloud model for the Desk Lead.

## Desk Report

```json
{
  "symbol": "SPX",
  "as_of": "2026-09-10T17:45:00Z",
  "market": { "status": "open", "last_session": "2026-09-10", "last_trade": "2026-09-10T17:45:00Z" },
  "mode": "community",
  "regime": "positive gamma (estimate), mean-reverting",
  "key_levels": [
    { "price": 5850, "kind": "call OI wall", "evidence": ["snp_…"] },
    { "price": 5790, "kind": "estimated zero-gamma", "evidence": ["snp_…"] }
  ],
  "scenarios": [
    { "name": "base", "condition": "holds above 5790", "path": "range 5800–5850", "evidence": ["snp_…"] },
    { "name": "bear", "condition": "3m close below 5790", "path": "expansion toward 5750", "evidence": ["snp_…"] }
  ],
  "ideas": [
    {
      "structure": "long_call",
      "contract": "SPX 2026-09-10 5820C",
      "entry": { "condition": "break_out", "trigger_price": 5812 },
      "price_basis": "premium",
      "est_entry_premium": 4.20,
      "stop": 2.10,
      "target": 6.50,
      "max_loss_usd": 210,
      "invalidation": "3m close below 5800",
      "risk_check": { "mode": "educational", "decision": "n/a" },
      "convertible": false
    }
  ],
  "dissent": "Bear case: put volume rising into the close.",
  "data_freshness": { "oldest_snapshot_sec": 840, "delayed": true },
  "disclaimer": "Not investment advice. AI can be wrong."
}
```

Numbers are illustrative.

The terminal shows a one-screen summary: regime, analyst stances, key levels, scenarios, ideas, and dissent. `--full` adds the analyst reports, the debate, evidence ids, and the Risk Officer's notes.

`as_of` is the time of the last trade in the data. When the market is closed (after hours, weekends, holidays), the report says so, and roles describe the last session and frame their scenarios for the next one.

## Units and Visibility Rules

- For options, `stop` and `target` are option premium prices. `max_loss_usd = (est_entry_premium − stop) × 100 × qty`. Equity ideas use underlying prices.
- Community ideas are built on delayed data. They are labeled "educational, delayed data, not risk-checked for execution" and can never become orders.
- Multi-leg structures such as verticals may appear as analysis but cannot become orders in v0.1.
- Exported, shared, and public reports contain regime, key levels, and scenarios only. Trade ideas are stripped.

## Agentic Capabilities

Available now:

- Parallel tool-using agents and a structured bull/bear debate.
- Deterministic guardrails: the risk engine decides; LLMs propose and critique.
- Key levels checked against the numbers in the snapshots they cite.
- MCP in both directions: `run_desk` as an MCP tool, and external MCP servers as read-only desk tools.
- Skills: strategy playbooks as `SKILL.md` files that can reference existing read-only tools but cannot add tools or code.
- Live terminal view of the desk; replay on recorded data to compare models, prompts, or skills.

Planned:

- Memory and reflection: past desk calls per symbol are stored with realized outcomes.

## Relation to TradingAgents

SOC Desk follows the public design idea of [TradingAgents](https://github.com/TauricResearch/TradingAgents) (analyst team, debate, trader, risk, portfolio manager) with an independent implementation. It specializes in options and futures flow and dealer positioning, uses a deterministic risk engine instead of an LLM-only risk team, and supports MCP in both directions.
