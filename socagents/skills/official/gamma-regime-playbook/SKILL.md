---
name: gamma-regime-playbook
title: Gamma regime playbook
description: Match single-leg idea structure to the dealer gamma regime, the walls, and the zero-gamma flip.
version: 1
applies_to: [strategist, desk_lead]
tools: [get_gex_estimate, get_option_quote]
---
# Gamma regime playbook

Use the Dealer Positioning report to frame every idea.

Positive gamma (spot above the zero-gamma flip):
- Dealer hedging tends to dampen moves. Expect a range between the put wall and the call wall.
- Prefer ideas that trigger on a rejection near a wall, with a target back toward the middle of the range.
- Keep targets modest. The walls are natural limits.
- If spot sits in the middle of the range with no clear trigger, "no trade" is a valid answer.

Negative gamma (spot below the flip):
- Dealer hedging can extend moves, and a break of the put wall can run.
- Prefer ideas that trigger on a confirmed break, with the invalidation back on the other side of the level that broke.
- Expect wider swings. Say so in the rationale, and keep qty at 1.

Near the flip (within about 0.3% of spot):
- The regime can change within the session. Say so, and prefer waiting for a close beyond the flip before any idea triggers.

Always:
- Tie every trigger, invalidation, and target to a level from the reports, with its evidence id.
- Levels from delayed public data are estimates. Say "estimate" when a level came from one.
