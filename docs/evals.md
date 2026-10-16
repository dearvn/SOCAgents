# Skill Evals

A skill says how a job should be done. An eval checks whether a model actually does it that way. `socagents eval` runs the Strategist on fixed market situations written for each official skill and scores its answers with deterministic checks.

```bash
socagents eval list                                   # the cases
socagents eval check                                  # validate every case
socagents eval run -m anthropic/<model> --trials 5    # score a model
socagents eval run -m ollama/<model> --ablate         # also run each case without its skill
socagents eval run -m openai/<model> --judge anthropic/<model> --out result.json
```

## What a Case Is

One JSON file under `socagents/evals/cases/<skill>/<id>.json`:

| Field | Meaning |
|---|---|
| `skill`, `title`, `expect` | the skill the case was written from, and whether the right answer is an `idea` or `no_trade` |
| `symbol`, `as_of`, `spot`, `reports`, `debate` | the Strategist's context, shaped like the one the desk builds |
| `quotes` | option contracts served when the Strategist calls `get_option_quote` (nearest strike, like the live tool) |
| `facts` | numbers some checks need, such as `flip`, `put_wall`, `call_wall`, `event` |
| `checks` | the deterministic checks, with optional `params` |
| `rubric` | Must-do, Must-avoid, and Best-practice lines taken from the skill |
| `oracle` | a reference answer that passes every check |
| `counter` | for a no-trade case: a plausible idea that must fail |

All case data is synthetic. Snapshot ids look like `snp_case_*`. Member data is never used in a case.

## How a Run Works

- The Strategist gets the production system prompt and context, the case's skill (unless the run is the `--ablate` arm without it), and one tool: `get_option_quote`, answered from the case. No network, no desk database, no risk check for execution.
- Each answer is scored by the case's checks. **Pass** means every check passed. **Score** is the share of checks passed (partial credit).
- `--trials` (default 5) answers per case. The table shows pass@1 (share of passing trials), the mean score, and its spread. With 15 cases, read a difference under about 10 points as noise.
- `--ablate` runs each case with and without its skill, which shows whether the skill changes the answer at all.

## Checks

| Check | Fails when |
|---|---|
| `expectation` | an idea case gets no idea, or a no-trade case gets an idea or no reason |
| `evidence_resolves` | an idea cites no snapshot, or one the case does not define |
| `quote_matches` | an option idea does not cite the quote for its own contract, or its premium is not that quote's mid |
| `qty_one`, `complete_plan` | qty is not 1; stop, target, or invalidation is missing |
| `trigger_on_level` | the entry is at market, or the trigger is not within a few cents of a level in the reports |
| `stop_within_half_premium` | a long option's stop risks more than half the premium, or is not below the entry |
| `late_entry` | after 14:30 ET (`after_et`) the trigger is farther than `max_trigger_pct` (0.15) from spot |
| `near_money` | the strike is farther than `max_pct` (0.5) from spot |
| `waits_beyond_flip` | near the flip, a call does not wait for a break above it or a put for a break below it |
| `break_matches_direction` | in negative gamma, a call does not trigger on a break out or a put on a break down |
| `wall_rejection` | in positive gamma, a call does not trigger near the put wall or a put near the call wall |
| `names_event`, `mentions_iv_drop`, `waits_for_release` | the rationale does not name the release, mention the volatility drop for long premium, or wait for the first 5-minute close after it |

A check that looks at ideas fails on an idea case with no ideas, so an empty answer scores zero there.

## Case Acceptance

`socagents eval check` (and every `eval run`) rejects a case unless:

- the oracle passes every check;
- on an idea case, the empty answer fails every check;
- on a no-trade case, the empty answer fails at least one check, and the `counter` answer fails `expectation`;
- the skill is an official skill that lints clean, every check name is known, and every snapshot id the oracle cites is defined by the case.

Write the checks first, then the oracle. If the oracle fails, fix the oracle, not the check.

## Rubric Judge

`--judge PROVIDER/MODEL` asks a second model to grade each answer from −5 to 5 against the rubric and a fixed constitution (fabricated numbers = −5; a trade where the playbook says stand aside is a material violation; a clear no-trade answer is good work). The grade is shown next to the checks and never changes pass or fail. The judge must be a different model from the one under test: a model that grades its own answers measures nothing.

## Adding a Case

1. Copy a case of the same skill, give it a new `id` (the file name), and change the situation.
2. Write `checks` and `rubric` from the skill text.
3. Write the `oracle` (and a `counter` for a no-trade case).
4. Run `socagents eval check`.

Not investment advice. A passing eval says the answer follows the playbook, not that the trade would have made money.
