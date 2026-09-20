# X Reply Bot

`socagents x` answers mentions on X: it polls the mention timeline, runs the single-agent
`ask` loop over the same market tools the desk uses, and replies in the thread. It drafts by
default and needs two separate switches before anything reaches X.

Status: implemented and tested against a mock API. It has not been run against a funded X
account, so treat the first live cycle as the real test.

## What it costs

X moved to pay-per-use in February 2026. Enable billing on the Project before anything
else. A legacy Free project is documented as write-only — no mention timeline, so no reply
bot — but in practice a project without billing has been seen returning 403 on *every* v2
endpoint, posting included, with the misleading message "you must use keys and tokens from a
developer App that is attached to a Project". Do not read that as a credential problem until
you have ruled billing out.

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

Enable pay-per-use billing on the project first. Then pick a credential.

| | OAuth 1.0a | OAuth 2.0 |
|---|---|---|
| Where | the app's "Keys and tokens" tab | the PKCE browser flow |
| Command | `socagents x login` | `socagents x auth` |
| Expiry | never | access token ~2h, refresh token rotates every use |
| Posts as | the account that owns the app | whichever account authorizes it |
| Setup | paste four values | register a callback URI, sign in, approve |

When both are stored, OAuth 1.0a wins: nothing about it can go stale halfway through a cron
schedule. `socagents x status` shows which one is active.

### OAuth 1.0a

On the app, set **User authentication settings** to Read and Write, then **regenerate** the
access token: a token created before the permission was raised keeps the old scope, and the
tab still shows the permission it was created with. Then:

```bash
socagents x login          # API Key, API Key Secret, Access Token, Access Token Secret
socagents x status
```

The access token belongs to the account that owns the app, so the bot tweets as that
account. That is fine for a first test and wrong for a long-lived bot on its own handle.

### OAuth 2.0

Use this to run the bot on a separate account. Add `http://127.0.0.1:8723/callback` as a
callback URI on the app, then, in a browser signed in as the bot account:

```bash
socagents x auth           # PKCE flow → refresh token in the OS keychain
```

Pass `--redirect-uri` if you registered a different one.

### Environment variables

`X_API_KEY`, `X_API_SECRET`, `X_ACCESS_TOKEN`, `X_ACCESS_TOKEN_SECRET` for OAuth 1.0a;
`X_CLIENT_ID`, `X_CLIENT_SECRET`, `X_REFRESH_TOKEN` for OAuth 2.0. They take priority over
the keychain. An OAuth 2.0 refresh token rotates on every use and the new one goes to the
keychain, so an environment variable holding the old one goes stale after the first refresh
and the run says so. For a read-only dry run, a short-lived `X_OAUTH2_ACCESS_TOKEN` alone is
enough.

## Rehearse without X

[`scripts/fake_x_api.py`](../scripts/fake_x_api.py) answers the three endpoints the bot uses
with canned mentions, so the whole loop can be exercised before an account exists. It costs
nothing and needs no credentials or billing.

```bash
python scripts/fake_x_api.py                 # one shell

export X_API_URL=http://127.0.0.1:8799/2     # another shell
export X_API_KEY=fake X_API_SECRET=fake X_ACCESS_TOKEN=fake X_ACCESS_TOKEN_SECRET=fake
export SOCAGENTS_HOME=/tmp/socagents-rehearsal
socagents x once --provider fixture
```

Two canned mentions come back: a question about SPY, which becomes a draft, and a prompt
injection, which is skipped without calling a model. Fixture market data is synthetic, so
the numbers in the draft are meaningless — rehearse with it, never post it.

Unset `X_API_URL` and `SOCAGENTS_HOME` before touching the real API.

## Test the write path on its own

Before running the reply loop, prove the account can post at all. One tweet, $0.015:

```bash
AGENT_SOCIAL=1 socagents x post "Test post from the SOCAgents desk bot."
```

It shows the weighted character count and the price, warns if the text contains a link, and
asks before sending. There is no agent behind this command: what you type is what goes out.

A 403 here is usually the App's permission or its tokens — set user authentication to Read
and Write, then regenerate the API key and secret followed by the access token and secret,
in that order, because the access token is derived from the consumer key. If freshly
regenerated keys from the right App still fail, the Project's billing is the cause, not the
credentials.

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
