"""Number verification: every key level must match a number in the snapshots it cites."""

from __future__ import annotations

from typing import Any

from socagents.desk.models import KeyLevel

REL_TOLERANCE = 0.0025
ABS_TOLERANCE = 0.01


def numbers_in(value: Any) -> list[float]:
    if isinstance(value, bool):
        return []
    if isinstance(value, int | float):
        return [float(value)]
    if isinstance(value, dict):
        return [n for v in value.values() for n in numbers_in(v)]
    if isinstance(value, list):
        return [n for v in value for n in numbers_in(v)]
    return []


def matches(price: float, numbers: list[float]) -> bool:
    tolerance = max(abs(price) * REL_TOLERANCE, ABS_TOLERANCE)
    return any(abs(n - price) <= tolerance for n in numbers)


def verify_levels(
    levels: list[KeyLevel], snapshot_numbers: dict[str, list[float]], *, owner: str
) -> tuple[list[KeyLevel], list[str]]:
    """Keep levels whose price appears in a cited, known snapshot. Return (kept, removed)."""
    kept: list[KeyLevel] = []
    removed: list[str] = []
    for level in levels:
        evidence = [e for e in level.evidence if e in snapshot_numbers]
        if not evidence:
            removed.append(f"{owner}: {level.kind} {level.price:g} (no valid evidence)")
            continue
        if not matches(level.price, [n for e in evidence for n in snapshot_numbers[e]]):
            removed.append(f"{owner}: {level.kind} {level.price:g} (not found in cited data)")
            continue
        kept.append(level.model_copy(update={"evidence": evidence}))
    return kept, removed


def known_evidence(ids: list[str], known: set[str]) -> list[str]:
    return [i for i in ids if i in known]
