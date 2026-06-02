"""Background sweep scheduler.

The scheduler runs as an asyncio background task. On startup it waits one
check_interval so the service can pass its liveness probe before doing any
work; sweeps thereafter run on the configured interval.

The sweep itself is intentionally simple in Phase 2: it fetches all Homebox
items, filters to those with no purchasePrice that are not already pending,
and enqueues them with null pricing data (Phase 3 fills in real prices).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from .config import get_settings
from .db import init_db, pending_homebox_ids, upsert_candidate
from .homebox import HomeboxClient, HomeboxError

logger = logging.getLogger(__name__)

_last_sweep: datetime | None = None


def last_sweep() -> datetime | None:
    return _last_sweep


async def run_sweep() -> dict[str, int]:
    """Perform one sweep. Returns counts: {scanned, queued, skipped}."""
    global _last_sweep
    settings = get_settings()
    client = HomeboxClient()
    counts = {"scanned": 0, "queued": 0, "skipped": 0}

    try:
        items = client.list_items()
    except HomeboxError as exc:
        logger.error("Sweep aborted — Homebox unreachable: %s", exc)
        return counts

    already_pending = pending_homebox_ids()

    for item in items:
        item_id = item.get("id", "")
        if not item_id:
            continue
        counts["scanned"] += 1

        purchase_price = item.get("purchasePrice") or 0
        if purchase_price > 0:
            counts["skipped"] += 1
            continue

        if item_id in already_pending:
            counts["skipped"] += 1
            continue

        name = item.get("name", "")
        manufacturer = (item.get("manufacturer") or "").strip()
        model = (item.get("modelNumber") or "").strip()

        parts = [p for p in [manufacturer, name, model] if p]
        query = " ".join(parts) + f" price {settings.price_currency}"

        upsert_candidate(
            homebox_id=item_id,
            item_name=name,
            query=query,
            price=None,
            currency=settings.price_currency,
            source_url=None,
            confidence="low",
            reason="queued — pricing pending Phase 3",
        )
        counts["queued"] += 1
        logger.debug("Queued item %s (%s)", item_id, name)

    _last_sweep = datetime.now(timezone.utc)
    logger.info(
        "Sweep complete: scanned=%d queued=%d skipped=%d",
        counts["scanned"], counts["queued"], counts["skipped"],
    )
    return counts


async def scheduler_loop() -> None:
    settings = get_settings()
    init_db()
    logger.info(
        "Scheduler started — first sweep in %ds, then every %ds",
        settings.check_interval, settings.check_interval,
    )
    await asyncio.sleep(settings.check_interval)
    while True:
        try:
            await run_sweep()
        except Exception:
            logger.exception("Unhandled error in sweep — will retry next interval")
        await asyncio.sleep(settings.check_interval)
