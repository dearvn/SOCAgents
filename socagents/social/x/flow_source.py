"""Where Options Flow prints come from.

:class:`PostgresFlowSource` reads the aggregates an options-flow engine has already
broadcast. Give it a connection string for a read-only role: this module only ever selects.
The driver is an optional extra (``pip install 'socagents[flow]'``).

:class:`JsonFileSource` reads the same rows from a file, for rehearsing the format offline.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from socagents.core.errors import ConfigError, ProviderError
from socagents.social.x.flow import FlowPrint

DSN_ENV = "FLOW_DATABASE_URL"

# Broadcast option-flow aggregates only: positioning and 0-1 DTE market pressure stay out.
QUERY = """
SELECT id, symbol, expiration, strike, option_type, direction,
       total_premium AS premium,
       COALESCE(NULLIF(filled_price, 0), mark_price) AS price,
       slug, created_at
FROM market_pressure_aggregates
WHERE flow_type = 'option_flow'
  AND notified = true
  AND created_at > %(since)s
  AND total_premium >= %(min_premium)s
ORDER BY total_premium DESC
LIMIT %(limit)s
"""


def _row_to_print(row: dict[str, Any]) -> FlowPrint:
    expiration = row["expiration"]
    created_at = row["created_at"]
    if isinstance(created_at, str):
        created_at = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=UTC)
    return FlowPrint(
        id=str(row["id"]),
        symbol=str(row["symbol"]),
        expiration=expiration.isoformat() if isinstance(expiration, date) else str(expiration),
        strike=float(row["strike"]),
        option_type=str(row["option_type"]),
        direction=str(row["direction"]),
        premium=float(row["premium"]),
        price=float(row["price"] or 0.0),
        created_at=created_at,
        slug=row.get("slug"),
    )


class PostgresFlowSource:
    def __init__(self, dsn: str | None = None) -> None:
        resolved = dsn or os.environ.get(DSN_ENV)
        if not resolved:
            raise ConfigError(
                f"No flow database. Set {DSN_ENV} to a read-only Postgres connection string, "
                "or rehearse with --from-file.",
                code="flow_source_missing",
            )
        self._dsn: str = resolved

    async def fetch(self, *, since: datetime, min_premium: float, limit: int) -> list[FlowPrint]:
        try:
            import psycopg
            from psycopg.rows import dict_row
        except ImportError as exc:
            raise ConfigError(
                "The Postgres driver is not installed. Run: pip install 'socagents[flow]'",
                code="flow_driver_missing",
            ) from exc
        params = {
            "since": since.astimezone(UTC),
            "min_premium": min_premium,
            "limit": limit,
        }
        try:
            async with await psycopg.AsyncConnection.connect(
                self._dsn, row_factory=dict_row, connect_timeout=10
            ) as conn:
                await conn.set_read_only(True)
                async with conn.cursor() as cur:
                    # The engine writes naive UTC timestamps; a UTC session compares them
                    # correctly whether the column is timestamp or timestamptz.
                    await cur.execute("SET TIME ZONE 'UTC'")
                    await cur.execute(QUERY, params)
                    rows = await cur.fetchall()
        except psycopg.Error as exc:
            raise ProviderError(
                f"The flow database refused the query ({type(exc).__name__}).",
                code="flow_source_unavailable",
            ) from exc
        return [_row_to_print(row) for row in rows]


class JsonFileSource:
    """A list of rows with the same keys as the SQL query returns."""

    def __init__(self, path: Path) -> None:
        self._path = path

    async def fetch(self, *, since: datetime, min_premium: float, limit: int) -> list[FlowPrint]:
        try:
            rows = json.loads(self._path.read_text())
        except (OSError, ValueError) as exc:
            raise ConfigError(f"Cannot read flow rows from {self._path}: {exc}") from exc
        prints = [_row_to_print(row) for row in rows]
        keep = [p for p in prints if p.created_at > since and p.premium >= min_premium]
        return sorted(keep, key=lambda p: p.premium, reverse=True)[:limit]
