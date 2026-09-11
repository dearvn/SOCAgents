"""Time-sortable, prefixed identifiers such as ``run_…`` and ``snp_…``."""

from __future__ import annotations

import secrets
import time


def new_id(prefix: str) -> str:
    millis = time.time_ns() // 1_000_000
    return f"{prefix}_{millis:012x}{secrets.token_hex(5)}"
