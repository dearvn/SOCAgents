# Security Design

To report a vulnerability, see [SECURITY.md](../SECURITY.md).

## Threat Model

We assume:
- prompts, news, social posts, and tool output can contain prompt injection, including attempts to trigger trades;
- models hallucinate symbols, strikes, expirations, and prices;
- data can be stale, delayed, or missing;
- agent loops can run away in steps or cost;
- external MCP servers and community skills can hide instructions or change after approval;
- in a multi-agent desk, one poisoned input can spread through analyst reports and the debate.

## Core Rules

- **The LLM proposes. Deterministic code disposes.** The risk engine is a pure function with no I/O. It returns allow or deny with reasons and suggested fixes. Whether a human approval is needed is decided by a separate policy.
- **Orders only through approved trade plans.** SOCAgents never talks to a broker directly. For members, approved single-leg plans become SocSwift pre-orders; SocSwift's own safety checks still apply.
- **Execution checks need fresh data.** Community ideas run on delayed data and are never marked as passing for execution.
- **Untrusted data stays untrusted.** External text is wrapped as data with provenance flags, and likely injection attempts in headlines are flagged. Ideas must cite at least one trusted data snapshot, or they are rejected.
- **Numbers are verified.** Levels and prices in a report are checked against the latest snapshots; unmatched numbers are removed.
- **Budgets and caps** on steps, tokens, cost, and debate rounds for every run.

## Approvals

- An approval is bound to a hash of the exact plan, is single use, and expires.
- Approvals happen only in the SocSwift app, never through an API key or an agent.
- The app protects approvals against cross-site requests, and live approvals require step-up confirmation.

## External MCP Servers

- **Read-only credentials first.** Connect broker MCP servers with read-only credentials wherever the broker supports them.
- **Default deny.** No tool is exposed until you allow it by name.
- **Annotations.** Tools declaring `readOnlyHint: true` can be allowed normally; others need an extra confirmation. Annotations are self-declared, so they never replace the allowlist.
- **Verb deny list (backstop).** A tool is blocked even if allowed when the leading verb of its name is one of `place, submit, send, cancel, replace, modify, amend, close, flatten, liquidate, transfer, withdraw, deposit, buy, sell, execute, exercise, trade`.
- **Change detection.** Tool definitions are hashed when approved; any change disables the server until you re-approve.
- **Pinning.** Package runners must name an exact version or digest; the CLI records what each server runs.
- **Isolation.** Descriptions and output are treated as untrusted data, size-capped, and never placed in the system policy. Arguments are limited to symbols, dates, and account aliases, so no data can be sent out. Servers run as subprocesses with a minimal environment plus the variables you name.
- **Limits.** Per-call timeouts and a per-run call limit for each server; every call is audited and snapshotted as untrusted.

## Skills

- A skill is a `SKILL.md` playbook. It can reference existing read-only tools but cannot add tools, network access, or code.
- Skills are guidance under the system policy, never system instructions. The risk engine ignores skills.
- Official skills ship inside the package and are reviewed by maintainers. User skills stay local and are off until you enable them. A community gallery with review before listing is planned.
- Every skill is checked when it is loaded: size limit, and rejection of instruction-like text, attempts to override risk rules or sizing, and performance claims. Its SHA-256 is recorded in the report and the audit log of every run.
- Skill text reaches a role's context as data, never its system policy.

## Member Data in the CLI

- SocSwift data is served only to a valid member credential, checked on every request.
- The CLI stores member data encrypted at rest with a key kept in the OS keychain. Without a keychain, member payloads are not stored at all (only a redaction marker).
- Member data is kept for at most 30 days. It is purged on `socagents logout`, and on the next run after SocSwift rejects the stored key (lapsed membership or revoked key). A temporary outage does not purge.
- Reports containing member data cannot be exported or shared.

## Sharing and Public Output

- Only Community reports can be shared, and shared copies contain regime, key levels, and scenarios only.
- No public or shared output contains trade ideas or member data.
- The [X reply bot](x-bot.md) posts nothing unless both `--post` and `AGENT_SOCIAL=1` are set. A mention is a stranger's text: it is wrapped as untrusted data, likely injection attempts are dropped rather than answered, and the reply prompt forbids recommendations, targets, and links. The data-delay note and the disclaimer are appended by code after the model, so a prompt cannot remove them.
- Reply caps are per rolling 24 hours and include a per-author cap, so one person cannot drive the bot's spend.

## Privacy

- No hidden telemetry. Analytics are opt-in.
- Secrets are never logged and never placed in prompts.
