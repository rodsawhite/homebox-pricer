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
    id            INTEGER PRIMARY KEY,
    homebox_id    TEXT NOT NULL,
    item_name     TEXT NOT NULL,
    query         TEXT NOT NULL,
    price         REAL,
    currency      TEXT DEFAULT 'AUD',
    source_url    TEXT,
    confidence    TEXT,
    reason        TEXT,
    status        TEXT DEFAULT 'pending',
    created_at    TEXT,
    decided_at    TEXT,
    thumbnail_url TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_pending_item
    ON price_candidates(homebox_id)
    WHERE status = 'pending';

CREATE TABLE IF NOT EXISTS amazon_orders (
    id           INTEGER PRIMARY KEY,
    order_id     TEXT NOT NULL,
    order_date   TEXT NOT NULL,
    asin         TEXT,
    title        TEXT NOT NULL,
    quantity     INTEGER DEFAULT 1,
    unit_price   REAL,
    currency     TEXT DEFAULT 'AUD',
    seller       TEXT,
    -- match state
    homebox_id   TEXT,
    match_score  REAL,
    status       TEXT DEFAULT 'unmatched',
    imported_at  TEXT NOT NULL,
    applied_at   TEXT,
    UNIQUE(order_id, asin)
);
"""

# Applied to DBs created before thumbnail_url was added.
_MIGRATIONS = [
    "ALTER TABLE price_candidates ADD COLUMN thumbnail_url TEXT",
]


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
        existing = {
            row[1]
            for row in conn.execute("PRAGMA table_info(price_candidates)").fetchall()
        }
        for stmt in _MIGRATIONS:
            # Derive the column name from "ALTER TABLE … ADD COLUMN <name> …"
            col = stmt.split("ADD COLUMN")[1].strip().split()[0]
            if col not in existing:
                conn.execute(stmt)


def upsert_candidate(
    homebox_id: str,
    item_name: str,
    query: str,
    price: float | None,
    currency: str,
    source_url: str | None,
    confidence: str,
    reason: str,
    thumbnail_url: str | None = None,
) -> int:
    """Insert a new pending candidate. Skips silently if one is already pending."""
    now = datetime.now(timezone.utc).isoformat()
    with get_conn() as conn:
        cur = conn.execute(
            """
            INSERT OR IGNORE INTO price_candidates
                (homebox_id, item_name, query, price, currency,
                 source_url, confidence, reason, status, created_at, thumbnail_url)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)
            """,
            (homebox_id, item_name, query, price, currency,
             source_url, confidence, reason, now, thumbnail_url),
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


def refresh_candidate(
    candidate_id: int,
    item_name: str,
    query: str,
    price: float | None,
    currency: str,
    source_url: str | None,
    confidence: str,
    reason: str,
) -> None:
    """Update a pending candidate with fresh lookup results and new item metadata."""
    with get_conn() as conn:
        conn.execute(
            """
            UPDATE price_candidates
               SET item_name=?, query=?, price=?, currency=?,
                   source_url=?, confidence=?, reason=?
             WHERE id=? AND status='pending'
            """,
            (item_name, query, price, currency, source_url, confidence, reason, candidate_id),
        )


def _count_by_status(table: str, defaults: dict[str, int]) -> dict[str, int]:
    """Group-count rows by their ``status`` column, seeded with zeroed defaults.

    ``table`` is a trusted internal constant (never user input), so interpolating
    it into the query is safe here.
    """
    with get_conn() as conn:
        rows = conn.execute(
            f"SELECT status, COUNT(*) as n FROM {table} GROUP BY status"
        ).fetchall()
    counts = dict(defaults)
    for row in rows:
        counts[row["status"]] = row["n"]
    return counts


def count_by_status() -> dict[str, int]:
    return _count_by_status("price_candidates", {"pending": 0, "applied": 0, "rejected": 0})


def upsert_amazon_orders(orders: list[dict]) -> tuple[int, int]:
    """Insert new Amazon order rows, skipping any already imported (by order_id+asin).
    Returns (inserted, skipped)."""
    now = datetime.now(timezone.utc).isoformat()
    inserted = skipped = 0
    with get_conn() as conn:
        for o in orders:
            cur = conn.execute(
                """
                INSERT OR IGNORE INTO amazon_orders
                    (order_id, order_date, asin, title, quantity,
                     unit_price, currency, seller, imported_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (o["order_id"], o["order_date"], o.get("asin"), o["title"],
                 o.get("quantity", 1), o.get("unit_price"), o.get("currency", "AUD"),
                 o.get("seller"), now),
            )
            if cur.lastrowid and cur.rowcount:
                inserted += 1
            else:
                skipped += 1
    return inserted, skipped


def list_amazon_orders(status: str | None = None) -> list[sqlite3.Row]:
    with get_conn() as conn:
        if status:
            return conn.execute(
                "SELECT * FROM amazon_orders WHERE status=? ORDER BY order_date DESC",
                (status,),
            ).fetchall()
        return conn.execute(
            "SELECT * FROM amazon_orders ORDER BY order_date DESC"
        ).fetchall()


def get_amazon_order(order_id: int) -> sqlite3.Row | None:
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM amazon_orders WHERE id=?", (order_id,)
        ).fetchone()


def set_amazon_match(order_id: int, homebox_id: str, match_score: float) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE amazon_orders SET homebox_id=?, match_score=?, status='matched' WHERE id=?",
            (homebox_id, match_score, order_id),
        )


def set_amazon_status(order_id: int, status: str) -> None:
    now = datetime.now(timezone.utc).isoformat()
    applied_at = now if status == "applied" else None
    with get_conn() as conn:
        conn.execute(
            "UPDATE amazon_orders SET status=?, applied_at=? WHERE id=?",
            (status, applied_at, order_id),
        )


def count_amazon_by_status() -> dict[str, int]:
    return _count_by_status(
        "amazon_orders", {"unmatched": 0, "matched": 0, "applied": 0, "skipped": 0}
    )


def pending_homebox_ids() -> set[str]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT homebox_id FROM price_candidates WHERE status = 'pending'"
        ).fetchall()
    return {row["homebox_id"] for row in rows}
