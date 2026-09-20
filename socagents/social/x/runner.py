"""One poll cycle: read new mentions, answer the ones that pass policy, reply in the thread.

The cycle is deliberately one-shot. A cron entry calling ``socagents x once`` every few
minutes is cheaper and easier to stop than a long-lived process, and a crash costs at most
one cycle.

Dry run is the default everywhere. Posting needs both ``--post`` and ``AGENT_SOCIAL=1``,
because one flag is easy to leave in a shell script by accident and two are not.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from socagents.agents.ask import run_ask
from socagents.core.config import Settings
from socagents.core.errors import ConfigError, SocAgentsError
from socagents.core.timeutil import iso
from socagents.db.store import Store
from socagents.runtime.states import RunStatus
from socagents.social.x.auth import load_authorizer
from socagents.social.x.client import POST_USD, READ_USD, Mention, XClient, XRateLimited, XUser
from socagents.social.x.compose import compose_reply
from socagents.social.x.policy import XPolicy, decide
from socagents.social.x.storage import BOT_USER_ID, BOT_USERNAME, SINCE_ID, XStore
from socagents.templates.x import x_reply_user_message


@dataclass
class Outcome:
    mention: Mention
    decision: str  # replied | dry_run | skipped | failed
    reason: str = ""
    symbols: list[str] = field(default_factory=list)
    tweets: list[str] = field(default_factory=list)
    run_id: str | None = None
    cost_usd: float | None = None
    reply_tweet_id: str | None = None
    error: str | None = None


@dataclass
class CycleReport:
    bot: XUser
    fetched: int
    outcomes: list[Outcome]
    dry_run: bool
    since_id: str | None = None
    notices: list[str] = field(default_factory=list)

    @property
    def posted_tweets(self) -> int:
        return sum(len(o.tweets) for o in self.outcomes if o.decision == "replied")

    @property
    def drafted_tweets(self) -> int:
        return sum(len(o.tweets) for o in self.outcomes if o.decision == "dry_run")

    @property
    def model_cost_usd(self) -> float:
        return sum(o.cost_usd or 0.0 for o in self.outcomes)

    @property
    def api_cost_usd(self) -> float:
        """What X billed for this cycle: posts read plus tweets posted. Zero in a dry run."""
        return self.fetched * READ_USD + self.posted_tweets * POST_USD


async def run_once(
    *,
    settings: Settings,
    policy: XPolicy | None = None,
    post: bool = False,
    limit: int = 25,
    provider_name: str | None = None,
    model_spec: str | None = None,
    max_steps: int = 4,
    max_tweets: int = 2,
    client: XClient | None = None,
    now: datetime | None = None,
) -> CycleReport:
    policy = policy or XPolicy()
    if post and not settings.kill_switches.agent_social:
        raise ConfigError(
            "Posting to X is off. Set AGENT_SOCIAL=1 to let this machine post, or drop "
            "--post to draft replies without sending them.",
            code="social_disabled",
        )
    owned = client is None
    if client is None:
        client = XClient(load_authorizer())
    store = Store(settings.db_path)
    x_store = XStore(store)
    try:
        bot = await _identify(client, x_store)
        mentions = await client.mentions(
            bot.id, since_id=x_store.get_state(SINCE_ID), max_results=limit
        )
        report = CycleReport(bot=bot, fetched=len(mentions), outcomes=[], dry_run=not post)
        for mention in mentions:
            outcome = await _handle(
                mention,
                bot=bot,
                client=client,
                x_store=x_store,
                policy=policy,
                settings=settings,
                provider_name=provider_name,
                model_spec=model_spec,
                max_steps=max_steps,
                max_tweets=max_tweets,
                post=post,
                now=now,
            )
            report.outcomes.append(outcome)
            _record(x_store, outcome)
            x_store.set_state(SINCE_ID, mention.id)
            report.since_id = mention.id
            if outcome.reason == "rate_limited":
                report.notices.append("X rate limit reached; the rest of the cycle was skipped.")
                break
        report.notices.extend(client.notices)
        return report
    finally:
        store.close()
        if owned:
            await client.aclose()


async def _identify(client: XClient, x_store: XStore) -> XUser:
    """The bot's own account. Cached, because every lookup is a billable call."""
    user_id = x_store.get_state(BOT_USER_ID)
    username = x_store.get_state(BOT_USERNAME)
    if user_id and username:
        return XUser(id=user_id, username=username)
    bot = await client.me()
    x_store.set_state(BOT_USER_ID, bot.id)
    x_store.set_state(BOT_USERNAME, bot.username)
    return bot


async def _handle(
    mention: Mention,
    *,
    bot: XUser,
    client: XClient,
    x_store: XStore,
    policy: XPolicy,
    settings: Settings,
    provider_name: str | None,
    model_spec: str | None,
    max_steps: int,
    max_tweets: int,
    post: bool,
    now: datetime | None,
) -> Outcome:
    verdict = decide(mention, store=x_store, policy=policy, bot_user_id=bot.id, now=now)
    if not verdict.ok:
        return Outcome(mention, "skipped", verdict.reason, verdict.symbols)

    try:
        result = await run_ask(
            question=mention.text,
            symbols=verdict.symbols,
            provider_name=provider_name,
            model_spec=model_spec,
            settings=settings,
            max_steps=max_steps,
            kind="x_reply",
            user_message=x_reply_user_message(
                mention.text, verdict.symbols, author=mention.username
            ),
        )
    except SocAgentsError as exc:
        return Outcome(mention, "failed", exc.code, verdict.symbols, error=str(exc))

    if result.status is not RunStatus.COMPLETED:
        code = result.error.code if result.error else result.status.value
        message = result.error.message if result.error else result.status.value
        return Outcome(
            mention, "failed", code, verdict.symbols, run_id=result.run_id, error=message
        )

    tweets = compose_reply(result.answer, delay_sec=result.data_delay_sec, max_tweets=max_tweets)
    outcome = Outcome(
        mention,
        "dry_run",
        symbols=verdict.symbols,
        tweets=tweets,
        run_id=result.run_id,
        cost_usd=result.cost_usd,
    )
    if not tweets:
        return Outcome(
            mention,
            "skipped",
            "empty_answer",
            verdict.symbols,
            run_id=result.run_id,
            cost_usd=result.cost_usd,
        )
    if not post:
        return outcome
    return await _post_thread(outcome, client=client)


async def _post_thread(outcome: Outcome, *, client: XClient) -> Outcome:
    reply_to = outcome.mention.id
    posted: list[str] = []
    for text in outcome.tweets:
        try:
            reply_to = await client.post(text, in_reply_to=reply_to)
        except XRateLimited as exc:
            outcome.error = str(exc)
            outcome.reason = "rate_limited"
            break
        except SocAgentsError as exc:
            outcome.error = str(exc)
            outcome.reason = exc.code
            break
        posted.append(reply_to)
    if not posted:
        outcome.decision = "failed"
        return outcome
    outcome.decision = "replied"
    outcome.reply_tweet_id = posted[0]
    outcome.tweets = outcome.tweets[: len(posted)]
    return outcome


def _record(x_store: XStore, outcome: Outcome) -> None:
    mention = outcome.mention
    x_store.record(
        tweet_id=mention.id,
        author_id=mention.author_id,
        username=mention.username,
        conversation_id=mention.conversation_id,
        text=mention.text,
        symbols=outcome.symbols,
        decision=outcome.decision,
        reason=outcome.error or outcome.reason or None,
        run_id=outcome.run_id,
        reply_text="\n---\n".join(outcome.tweets) or None,
        reply_tweet_id=outcome.reply_tweet_id,
        cost_usd=outcome.cost_usd,
        tweeted_at=iso(mention.created_at),
    )
