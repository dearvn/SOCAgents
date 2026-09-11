# Security Policy

SOCAgents can connect to brokerage-related systems through SocSwift. Security reports are taken seriously.

## Reporting a Vulnerability

- Use GitHub private vulnerability reporting: the **Security** tab of this repository → **Report a vulnerability**.
- Do not open public issues for vulnerabilities.
- Include steps to reproduce, affected version or commit, and impact.
- If private reporting is unavailable, open an issue titled "Security contact request" with no details, and a maintainer will contact you privately.

## Supported Versions

SOCAgents is pre-alpha. Security fixes go to the latest commit on `main`. After v0.1, the latest release is supported.

## What to Expect

| Step | Target |
|---|---|
| Acknowledgement | within 3 business days |
| Initial assessment | within 7 business days |
| Fix or mitigation plan | depends on severity; critical issues first |

## In Scope

- Anything that lets an agent place, modify, or cancel an order without a valid approval
- Bypasses of the deterministic risk engine or kill switches
- Leaks of API keys, tokens, or SocSwift member data to non-members
- Prompt injection through data, external MCP servers, or skills that changes agent behavior beyond its policy
- Cross-user data access in the hosted service

## Out of Scope

- Losses from trading decisions
- Model output quality or incorrect market analysis without a security impact
- Vulnerabilities in third-party services that SOCAgents does not control

## Safe Harbor

Good-faith research that respects user privacy, avoids live accounts you do not own, and does not degrade service is welcome.
