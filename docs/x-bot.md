# X Reply Bot

`socagents x` answers mentions on X: it polls the mention timeline, runs the single-agent
`ask` loop over the same market tools the desk uses, and replies in the thread. It drafts by
default and needs two separate switches before anything reaches X.

Status: implemented and tested against a mock API. It has not been run against a funded X
account, so treat the first live cycle as the real test.

## What it costs

X moved to pay-per-use in February 2026. The legacy Free tier is write-only: it cannot read a
mention timeline, so a reply bot cannot run on it at any volume. Enable pay-per-use billing
first, or every read returns 403.

| Item | Rate | 50 mentions/day |
|---|---|---|
| Post read | $0.005 | $0.25 |
| Post created | $0.015 | $0.75 |
| Post created with a link | $0.20 | $10.00 |
| Model calls | depends on the model | $0.50–1.50 |

The link rate is why [`compose.py`](../socagents/social/x/compose.py) strips every URL out of a
reply. Put the SocSwift link in the account bio, where it is free. `socagents x once` prints an
estimate of what X billed for that cycle, using the rates in
[`client.py`](../socagents/social/x/client.py) — update them when X changes them.

## Setup

1. In the X developer portal, set the app's user authentication to **Read and Write**, then
   regenerate its tokens. Tokens issued before the change keep the old scope.
2. Get an OAuth 2.0 refresh token with the scopes
   `tweet.read tweet.write users.read offline.access`.
3. Store the credentials:

```bash
socagents x login          # client id, optional secret, refresh token → OS keychain
socagents x status         # where each credential came from, and whether posting is on
```

`X_CLIENT_ID`, `X_CLIENT_SECRET` and `X_REFRESH_TOKEN` work too and take priority over the
keychain. Refresh tokens rotate on every use: the rotated one goes to the keychain, so an
environment variable holding the old token goes stale after the first refresh and the run
says so. For a read-only dry run, a short-lived `X_ACCESS_TOKEN` on its own is enough.

Use a separate account for the bot, not your main one.

## Dry run

```bash
socagents x once --provider community -m anthropic/<model>
```

It reads new mentions, decides on each one, drafts the reply, prints it with its character
count, and posts nothing. Everything else behaves exactly like a live cycle, caps included: a
drafted reply consumes a reply slot, so a dry run tells you how many replies a real day would
have sent. `--json` prints the whole cycle for a log.

## Going live

```bash
AGENT_SOCIAL=1 socagents x once --post --max-replies 10 --symbols SPY,QQQ,SPX
```

Both are required. `--post` alone fails with `social_disabled`, because a flag left in a shell
script should not be enough to start tweeting.

From cron, every five minutes:

```cron
*/5 * * * * cd /path/to/SOCAgents && AGENT_SOCIAL=1 .venv/bin/socagents x once --post >> ~/x-bot.log 2>&1
```

One-shot beats a daemon here: a crash costs one cycle, and stopping the bot is removing a line.

## What it refuses to answer

Checked in [`policy.py`](../socagents/social/x/policy.py) before any model runs, so a skip is
free:

| Reason | Meaning |
|---|---|
| `self`, `retweet`, `already_handled` | not a question aimed at the bot, or already seen |
| `too_old` | older than `--max-age-hours` (default 6) |
| `injection` | the text tries to instruct the agent; it is dropped, not answered |
| `no_symbol`, `unsupported_symbol` | no ticker, or one outside `--symbols` |
| `daily_cap`, `author_cap`, `budget_cap` | rolling-24h caps on replies, replies per author, and model spend |

The per-author cap matters most: without it one person can mention the bot a hundred times and
spend your money. Defaults are 10 replies/day, 3 per author, $2.00/day of model spend.

Mention text reaches the model only through `x_reply_user_message`, wrapped in markers and
labelled untrusted, and the reply prompt forbids trade recommendations, price targets, links
and disclaimers of its own. The delay label and "Not investment advice." are appended by code
after the model, so they cannot be prompted away.

## Replies

Every reply is plain text, at most two tweets, URL-free, and ends with how delayed the data
was. Snapshot ids are stripped from the public text — `[snp_0a1b2c]` means nothing to a
reader — but the run id and its snapshots stay on the local row, so any reply can be traced
back to the data it came from with `socagents report` and the store.

Character counting uses X's weighted length, where most Latin text counts as one per character
and everything else as two.

## State and audit

Two tables in the usual SQLite store: `x_state` (mention cursor, bot account) and `x_mentions`
(one row per mention seen, with the decision, the reason, the run id, the reply, and the cost).

```bash
socagents x status --limit 20
```

The cursor advances past every mention the bot has recorded, including failures. A mention that
failed is not retried; its row holds the error.

## Rules to follow

- Label the account as automated in its profile, as X's automation rules require.
- Reply only to mentions. No unsolicited replies, no automated DMs, no repeated identical posts.
- Community data is delayed, so replies are educational. Keep the disclaimer.

## Known gaps

- Polling only. X's Account Activity webhooks are enterprise-tier.
- Only the mentions timeline. Quote tweets that do not mention the bot are invisible to it.
- One symbol set per reply, and no memory of an earlier exchange with the same person.
- No image or chart replies.
