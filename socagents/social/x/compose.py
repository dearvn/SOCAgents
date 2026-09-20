"""Turn an agent answer into tweets.

Three things happen here, and each one exists for a reason:

* Links are removed. X bills a post with a link at more than ten times the rate of one
  without, so the bot never puts a URL in a reply. The account bio carries the link.
* Snapshot ids are removed from the body. ``[snp_0123abcd]`` means nothing to a reader who
  cannot query this machine's store. The ids stay on the run row, so a reply is still
  auditable here; it just does not spend characters saying so in public.
* Every reply ends with how delayed the data was and "Not investment advice."
"""

from __future__ import annotations

import re

from socagents.core.timeutil import fmt_delay

X_MAX_WEIGHT = 280
DISCLAIMER = "Not investment advice."

# X counts most Latin text as one unit per character and everything else as two.
_LIGHT_RANGES = ((0, 4351), (8192, 8205), (8208, 8223), (8242, 8247))

_MD_LINK_RE = re.compile(r"\[([^\]]+)\]\(\s*<?(?:https?://|www\.)[^)]*>?\s*\)")
_SNAPSHOT_RE = re.compile(r"[ \t]*\[(?:snp_[0-9a-f]+)(?:\s*,\s*snp_[0-9a-f]+)*\]")
_URL_RE = re.compile(r"(?:https?://|www\.)\S+")
_HANDLE_RE = re.compile(r"(?<![A-Za-z0-9_])@\w{1,15}")
_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s*", re.MULTILINE)
_BULLET_RE = re.compile(r"^\s{0,3}(?:[-*+]|\d+\.)\s+", re.MULTILINE)
_EMPHASIS_RE = re.compile(r"[*_`]{1,3}")
_SPACE_BEFORE_PUNCT_RE = re.compile(r"\s+([.,;:!?)])")
_WHITESPACE_RE = re.compile(r"\s+")


def weighted_len(text: str) -> int:
    """X's weighted character count, the one the 280 limit is actually measured in."""
    return sum(1 if any(lo <= ord(ch) <= hi for lo, hi in _LIGHT_RANGES) else 2 for ch in text)


def mention_body(text: str) -> str:
    """A mention with handles and links removed, so ticker extraction sees only prose."""
    return _WHITESPACE_RE.sub(" ", _URL_RE.sub(" ", _HANDLE_RE.sub(" ", text))).strip()


def strip_for_x(text: str) -> str:
    """Flatten an agent answer into one plain-text paragraph with no links or markup."""
    out = _MD_LINK_RE.sub(r"\1", text)
    out = _SNAPSHOT_RE.sub("", out)
    out = _URL_RE.sub("", out)
    out = _HEADING_RE.sub("", out)
    out = _BULLET_RE.sub("", out)
    out = _EMPHASIS_RE.sub("", out)
    out = _WHITESPACE_RE.sub(" ", out)
    out = _SPACE_BEFORE_PUNCT_RE.sub(r"\1", out)
    return out.strip()


def tail_for(delay_sec: int | None) -> str:
    if delay_sec is None:
        return DISCLAIMER
    if delay_sec <= 0:
        return f"Real-time data. {DISCLAIMER}"
    return f"Data delayed {fmt_delay(delay_sec)}. {DISCLAIMER}"


def compose_reply(answer: str | None, *, delay_sec: int | None, max_tweets: int = 2) -> list[str]:
    """Split an answer into at most ``max_tweets`` tweets. Empty answer, no tweets.

    Each tweet costs money to post, so the default is two. Anything that does not fit is
    truncated with an ellipsis rather than spilling into a long thread nobody reads.
    """
    body = strip_for_x(answer or "")
    if not body:
        return []
    tail = tail_for(delay_sec)
    single = f"{body} {tail}"
    if weighted_len(single) <= X_MAX_WEIGHT:
        return [single]
    for count in range(2, max(max_tweets, 2) + 1):
        parts = _pack(body, tail, count, truncate=False)
        if parts is not None:
            return parts
    packed = _pack(body, tail, max(max_tweets, 2), truncate=True)
    return packed or [f"{_hard_cut(body, X_MAX_WEIGHT - weighted_len(tail) - 2)}… {tail}"]


def _pack(body: str, tail: str, count: int, *, truncate: bool) -> list[str] | None:
    words = body.split()
    parts: list[str] = []
    for index in range(count):
        last = index == count - 1
        marker = f" ({index + 1}/{count})"
        budget = X_MAX_WEIGHT - weighted_len(marker)
        if last:
            budget -= weighted_len(f" {tail}") + 1  # the 1 is room for a truncation ellipsis
        if budget <= 0:
            return None
        chunk = _take(words, budget)
        if not chunk:
            return None
        if last and words:
            if not truncate:
                return None
            chunk += "…"
        parts.append(f"{chunk} {tail}{marker}" if last else f"{chunk}{marker}")
        if not words and not last:
            return None  # the body fits in fewer tweets; let the caller try a smaller count
    return parts


def _take(words: list[str], budget: int) -> str:
    taken: list[str] = []
    while words and weighted_len(" ".join([*taken, words[0]])) <= budget:
        taken.append(words.pop(0))
    if not taken and words:
        taken.append(_hard_cut(words.pop(0), budget))
    return " ".join(taken)


def _hard_cut(text: str, budget: int) -> str:
    out = text
    while out and weighted_len(out) > max(budget, 1):
        out = out[:-1]
    return out
