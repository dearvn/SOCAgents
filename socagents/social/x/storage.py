"""X bot tables in the local SQLite store: the mention cursor and one row per mention seen.

Mention text is untrusted input from strangers. It is stored as plain community-mode data,
never sealed, and never read back into a prompt except through
:func:`socagents.templates.x.x_reply_user_message`, which wraps it as data.
"""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any

from socagents.core.timeutil import iso, utcnow
from socagents.db.store import Store

X_SCHEMA = """
CREATE TABLE IF NOT EXISTS x_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS x_mentions (
    tweet_id TEXT PRIMARY KEY,
    author_id TEXT NOT NULL,
    username TEXT,
    conversation_id TEXT,
    text TEXT NOT NULL,
    symbols TEXT NOT NULL,
    decision TEXT NOT NULL,
    reason TEXT,
    run_id TEXT,
    reply_text TEXT,
    reply_tweet_id TEXT,
    cost_usd REAL,
    tweeted_at TEXT,
    handled_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_x_mentions_handled ON x_mentions(handled_at DESC);
CREATE INDEX IF NOT EXISTS idx_x_mentions_author ON x_mentions(author_id, handled_at DESC);
"""

SINCE_ID = "since_id"
BOT_USER_ID = "bot_user_id"
BOT_USERNAME = "bot_username"

# Decisions that consumed a reply slot. A dry run counts: the point of a dry run is to
# behave exactly like the live loop, caps included.
SPENT = ("replied", "dry_run")


class XStore:
    def __init__(self, store: Store) -> None:
        self._store = store
        self._db = store.connection
        self._db.executescript(X_SCHEMA)
        self._db.commit()

    # state

    def get_state(self, key: str) -> str | None:
        row = self._db.execute("SELECT value FROM x_state WHERE key = ?", (key,)).fetchone()
        return str(row["value"]) if row else None

    def set_state(self, key: str, value: str) -> None:
        self._db.execute(
            "INSERT INTO x_state (key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
            "updated_at = excluded.updated_at",
            (key, value, iso(utcnow())),
        )
        self._db.commit()

    # mentions

    def handled(self, tweet_id: str) -> bool:
        row = self._db.execute(
            "SELECT 1 FROM x_mentions WHERE tweet_id = ?", (tweet_id,)
        ).fetchone()
        return row is not None

    def record(
        self,
        *,
        tweet_id: str,
        author_id: str,
        username: str | None,
        conversation_id: str | None,
        text: str,
        symbols: list[str],
        decision: str,
        reason: str | None = None,
        run_id: str | None = None,
        reply_text: str | None = None,
        reply_tweet_id: str | None = None,
        cost_usd: float | None = None,
        tweeted_at: str | None = None,
    ) -> None:
        self._db.execute(
            "INSERT OR REPLACE INTO x_mentions (tweet_id, author_id, username, conversation_id, "
            "text, symbols, decision, reason, run_id, reply_text, reply_tweet_id, cost_usd, "
            "tweeted_at, handled_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                tweet_id,
                author_id,
                username,
                conversation_id,
                text,
                json.dumps(symbols),
                decision,
                reason,
                run_id,
                reply_text,
                reply_tweet_id,
                cost_usd,
                tweeted_at,
                iso(utcnow()),
            ),
        )
        self._db.commit()

    # caps

    def _cutoff(self, hours: int) -> str:
        return iso(utcnow() - timedelta(hours=hours))

    def replies_within(self, hours: int = 24) -> int:
        placeholders = ", ".join("?" * len(SPENT))
        row = self._db.execute(
            f"SELECT COUNT(*) AS n FROM x_mentions WHERE decision IN ({placeholders}) "
            "AND handled_at >= ?",
            (*SPENT, self._cutoff(hours)),
        ).fetchone()
        return int(row["n"])

    def author_replies_within(self, author_id: str, hours: int = 24) -> int:
        placeholders = ", ".join("?" * len(SPENT))
        row = self._db.execute(
            f"SELECT COUNT(*) AS n FROM x_mentions WHERE author_id = ? "
            f"AND decision IN ({placeholders}) AND handled_at >= ?",
            (author_id, *SPENT, self._cutoff(hours)),
        ).fetchone()
        return int(row["n"])

    def spend_within(self, hours: int = 24) -> float:
        row = self._db.execute(
            "SELECT COALESCE(SUM(cost_usd), 0.0) AS total FROM x_mentions WHERE handled_at >= ?",
            (self._cutoff(hours),),
        ).fetchone()
        return float(row["total"])

    def recent(self, limit: int = 20) -> list[dict[str, Any]]:
        rows = self._db.execute(
            "SELECT * FROM x_mentions ORDER BY handled_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]
