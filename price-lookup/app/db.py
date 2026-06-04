"""SQLite access layer for price candidates.

The DB file is created on first use. The schema is kept minimal: one table
holding all price candidates across their lifecycle (pending → applied/rejected).
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Generator

from .config import get_settings

# Confidence levels, lowest to highest. Used to pre-filter the review queue:
# a candidate is shown only if its rank >= the configured threshold's rank.
_CONFIDENCE_ORDER = {"low": 0, "medium": 1, "high": 2}


def confidence_rank(level: str | None) -> int:
    """Map a confidence label to its rank; unknown/None sorts as lowest."""
    return _CONFIDENCE_ORDER.get((level or "").lower(), 0)


_CREATE_SQL = """
CREATE TABLE IF NOT EXISTS price_candidates (
    id          INTEGER PRIMARY KEY,
    homebox_id  TEXT NOT NULL,
    item_name   TEXT NOT NULL,
    query       TEXT NOT NULL,
    price       REAL,
    currency    TEXT DEFAULT 'AUD',
    source_url  TEXT,
    confidence  TEXT,
    reason      TEXT,
    status      TEXT DEFAULT 'pending',
    created_at  TEXT,
    decided_at  TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_pending_item
    ON price_candidates(homebox_id)
    WHERE status = 'pending';
"""


def _db_path() -> str:
    return get_settings().db_path


@contextmanager
def get_conn() -> Generator[sqlite3.Connection, None, None]:
    conn = sqlite3.connect(_db_path())
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    with get_conn() as conn:
        conn.executescript(_CREATE_SQL)


def upsert_candidate(
    homebox_id: str,
    item_name: str,
    query: str,
    price: float | None,
    currency: str,
    source_url: str | None,
    confidence: str,
    reason: str,
) -> int:
    """Insert a new pending candidate. Skips silently if one is already pending."""
    now = datetime.now(timezone.utc).isoformat()
    with get_conn() as conn:
        cur = conn.execute(
            """
            INSERT OR IGNORE INTO price_candidates
                (homebox_id, item_name, query, price, currency,
                 source_url, confidence, reason, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)
            """,
            (homebox_id, item_name, query, price, currency,
             source_url, confidence, reason, now),
        )
        return cur.lastrowid or 0


def get_candidate(candidate_id: int) -> sqlite3.Row | None:
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM price_candidates WHERE id = ?", (candidate_id,)
        ).fetchone()


def list_candidates(
    status: str | None = None, min_confidence: str | None = None
) -> list[sqlite3.Row]:
    """List candidates, newest first.

    ``min_confidence`` hides candidates whose confidence ranks below the given
    threshold (low < medium < high). Candidates are always *stored* regardless
    of confidence — this filter is purely a view concern so nothing is lost.
    Confidence labels are text, so the threshold is applied in Python rather
    than SQL.
    """
    with get_conn() as conn:
        if status:
            rows = conn.execute(
                "SELECT * FROM price_candidates WHERE status = ? ORDER BY created_at DESC",
                (status,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM price_candidates ORDER BY created_at DESC"
            ).fetchall()
    if min_confidence:
        floor = confidence_rank(min_confidence)
        rows = [r for r in rows if confidence_rank(r["confidence"]) >= floor]
    return rows


def set_candidate_status(
    candidate_id: int, status: str, decided_at: str | None = None
) -> None:
    decided_at = decided_at or datetime.now(timezone.utc).isoformat()
    with get_conn() as conn:
        conn.execute(
            "UPDATE price_candidates SET status = ?, decided_at = ? WHERE id = ?",
            (status, decided_at, candidate_id),
        )


def update_candidate_price(
    candidate_id: int, price: float, source_url: str | None
) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE price_candidates SET price = ?, source_url = ? WHERE id = ?",
            (price, source_url, candidate_id),
        )


def count_by_status() -> dict[str, int]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT status, COUNT(*) as n FROM price_candidates GROUP BY status"
        ).fetchall()
    counts: dict[str, int] = {"pending": 0, "applied": 0, "rejected": 0}
    for row in rows:
        counts[row["status"]] = row["n"]
    return counts


def pending_homebox_ids() -> set[str]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT homebox_id FROM price_candidates WHERE status = 'pending'"
        ).fetchall()
    return {row["homebox_id"] for row in rows}
