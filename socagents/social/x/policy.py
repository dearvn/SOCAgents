"""What the bot is allowed to answer, decided before any model is called.

Every check here is a cheap local check that runs before the expensive part. Two of them
are there to stop someone else spending your money: a per-author cap, so one person cannot
pull the bot into a hundred replies, and a daily spend cap on top of the reply count.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from socagents.agents.ask import extract_symbols
from socagents.core.timeutil import utcnow
from socagents.safety import looks_like_injection
from socagents.social.x.client import Mention
from socagents.social.x.compose import mention_body
from socagents.social.x.storage import XStore

# Why a mention was not answered. Stored on the row so `socagents x status` can show it.
REASONS = {
    "self": "the bot's own post",
    "already_handled": "already handled in an earlier cycle",
    "retweet": "a retweet, not a question",
    "too_old": "older than the age limit",
    "injection": "the text tries to give the agent instructions",
    "no_symbol": "no ticker in the text",
    "unsupported_symbol": "ticker outside the allowed list",
    "daily_cap": "daily reply cap reached",
    "author_cap": "per-author daily cap reached",
    "budget_cap": "daily spend cap reached",
    "empty_answer": "the agent produced nothing postable",
}


@dataclass(frozen=True)
class XPolicy:
    max_replies_per_day: int = 10
    max_replies_per_author_per_day: int = 3
    max_cost_usd_per_day: float = 2.00
    max_age_hours: int = 6
    symbols: frozenset[str] | None = None


@dataclass(frozen=True)
class Verdict:
    ok: bool
    reason: str = ""
    symbols: list[str] = field(default_factory=list)


def decide(
    mention: Mention,
    *,
    store: XStore,
    policy: XPolicy,
    bot_user_id: str,
    now: datetime | None = None,
) -> Verdict:
    now = now or utcnow()
    if mention.author_id == bot_user_id:
        return Verdict(False, "self")
    if store.handled(mention.id):
        return Verdict(False, "already_handled")
    if mention.is_retweet:
        return Verdict(False, "retweet")
    if mention.created_at < now - timedelta(hours=policy.max_age_hours):
        return Verdict(False, "too_old")
    if looks_like_injection(mention.text):
        return Verdict(False, "injection")

    symbols = extract_symbols(mention_body(mention.text))
    if not symbols:
        return Verdict(False, "no_symbol")
    if policy.symbols is not None and not set(symbols) <= policy.symbols:
        return Verdict(False, "unsupported_symbol", symbols)

    if store.replies_within(24) >= policy.max_replies_per_day:
        return Verdict(False, "daily_cap", symbols)
    if store.author_replies_within(mention.author_id, 24) >= policy.max_replies_per_author_per_day:
        return Verdict(False, "author_cap", symbols)
    if store.spend_within(24) >= policy.max_cost_usd_per_day:
        return Verdict(False, "budget_cap", symbols)
    return Verdict(True, symbols=symbols)
