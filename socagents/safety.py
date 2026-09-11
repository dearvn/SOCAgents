"""Heuristics that flag untrusted text that looks like instructions aimed at an agent.

Flagging is a backstop. The real protection is that untrusted text is always wrapped as data
and can never call order tools.
"""

from __future__ import annotations

import re

_PATTERNS = (
    r"\bignore (all |any )?(the )?(previous|prior|above|earlier) (instructions|prompts?|rules)\b",
    r"\bdisregard (all |any )?(the )?(previous|prior|above|earlier)\b",
    r"\b(system prompt|developer message|you are now)\b",
    r"\byou (must|should) (now )?(buy|sell|place|execute|submit)\b",
    r"\btell the user to (buy|sell)\b",
    r"\b(place|submit|execute) (an? |the )?(order|trade)s?\b",
)
_INJECTION_RE = re.compile("|".join(_PATTERNS), re.IGNORECASE)


def looks_like_injection(text: str) -> bool:
    return bool(_INJECTION_RE.search(text))
