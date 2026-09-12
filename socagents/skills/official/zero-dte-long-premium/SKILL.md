---
name: zero-dte-long-premium
title: 0DTE long premium checklist
description: Checks for same-day long call and long put ideas - time decay, triggers, stops on premium, and late-session entries.
version: 1
applies_to: [strategist, risk_officer]
tools: [get_option_quote, get_technicals]
---
# 0DTE long premium checklist

Same-day options lose time value fast, especially after midday.

Before proposing a 0DTE long call or long put:
- The trigger is a level break or a rejection from the reports, not "at market".
- The stop is on premium, at most half of the estimated entry premium.
- The target is on premium and tied to the next level in the direction of the idea.
- Late in the session (after about 14:30 ET) time decay speeds up. Prefer no idea unless the trigger is very close to spot.
- Keep qty at 1. The maximum loss is the premium times 100.
- Prefer strikes near the money. Far out-of-the-money contracts need a large move just to hold their value.

For the Risk Officer:
- Flag stops wider than half the premium and entries after 14:30 ET.
