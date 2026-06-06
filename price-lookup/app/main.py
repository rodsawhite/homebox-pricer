"""FastAPI app for the price-lookup sidecar."""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from urllib.parse import quote_plus

from fastapi import FastAPI, Form, HTTPException, Request, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from . import __version__
from .amazon import match_orders_to_items, parse_amazon_csv

logger = logging.getLogger(__name__)
from .config import get_settings
from .db import (
    count_amazon_by_status,
    count_by_status,
    get_amazon_order,
    get_candidate,
    init_db,
    list_amazon_orders,
    list_candidates,
    refresh_candidate,
    set_amazon_match,
    set_amazon_status,
    set_candidate_status,
    update_candidate_price,
    upsert_amazon_orders,
)
from .homebox import HomeboxClient, HomeboxError
from .logging_config import configure_logging
from .pricing import lookup_price
from .scheduler import build_search_query, last_sweep, run_sweep, scheduler_loop

configure_logging(get_settings().log_format)

_TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


@asynccontextmanager
async def _lifespan(app: FastAPI):  # noqa: ARG001
    init_db()
    task = asyncio.create_task(scheduler_loop())
    yield
    task.cancel()


app = FastAPI(title="price-lookup", version=__version__, lifespan=_lifespan)


# ---------------------------------------------------------------------------
# Liveness / status
# ---------------------------------------------------------------------------


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "version": __version__}


@app.get("/status")
def status() -> dict[str, object]:
    settings = get_settings()
    sweep = last_sweep()
    return {
        "version": __version__,
        "homebox_url": settings.homebox_url,
        "price_text_model": settings.price_text_model,
        "check_interval": settings.check_interval,
        "last_sweep": sweep.isoformat() if sweep else None,
        "counts": count_by_status(),
    }


# ---------------------------------------------------------------------------
# Review queue page (HTML)
# ---------------------------------------------------------------------------


@app.get("/", response_class=HTMLResponse)
def queue_page(
    request: Request,
    flash: str = "",
    flash_type: str = "ok",
    include_all: bool = False,
) -> Any:
    # Hide sub-threshold candidates by default; ?include_all=true reveals them.
    settings = get_settings()
    min_conf = None if include_all else settings.price_min_confidence
    candidates_raw = list_candidates(status="pending", min_confidence=min_conf)
    candidates = [dict(row) for row in candidates_raw]

    # How many pending candidates are hidden by the confidence filter?
    total_pending = len(list_candidates(status="pending"))
    hidden = total_pending - len(candidates)

    sweep = last_sweep()
    return _TEMPLATES.TemplateResponse(
        request,
        "queue.html",
        {
            "candidates": candidates,
            "counts": count_by_status(),
            "last_sweep": sweep.strftime("%Y-%m-%d %H:%M UTC") if sweep else None,
            "flash": {"type": flash_type, "message": flash} if flash else None,
            "min_confidence": min_conf,
            "include_all": include_all,
            "hidden_count": hidden,
        },
    )


# ---------------------------------------------------------------------------
# Edit form (HTML form POST → redirect back to queue)
# ---------------------------------------------------------------------------


@app.post("/candidates/{candidate_id}/edit")
def web_edit(
    candidate_id: int,
    price: float = Form(...),
    source_url: str = Form(""),
) -> RedirectResponse:
    row = get_candidate(candidate_id)
    if not row:
        return _flash_redirect("/", "Candidate not found", ok=False)
    if row["status"] != "pending":
        return _flash_redirect("/", f"Already {row['status']}", ok=False)
    update_candidate_price(candidate_id, price, source_url or None)
    return _flash_redirect("/", "Price updated")


# ---------------------------------------------------------------------------
# Candidates API (JSON)
# ---------------------------------------------------------------------------


@app.get("/api/candidates")
def api_list_candidates(
    status: str | None = None, include_all: bool = False
) -> list[dict[str, Any]]:
    # Apply the confidence pre-filter by default (matching the queue page);
    # ?include_all=true returns everything, including sub-threshold candidates.
    min_conf = None if include_all else get_settings().price_min_confidence
    return [
        dict(row)
        for row in list_candidates(status=status, min_confidence=min_conf)
    ]


@app.post("/api/candidates/{candidate_id}/approve")
def api_approve(candidate_id: int, request: Request) -> Any:
    row, resp = _guard_pending(candidate_id, request)
    if resp:
        return resp
    if row["price"] is None:
        return _fail(
            request, "/", "Set a price before approving", 400,
            "Cannot approve a candidate with no price — edit it first",
        )

    try:
        HomeboxClient().apply_price(row["homebox_id"], row["price"])
    except HomeboxError as exc:
        return _fail(request, "/", f"Homebox error: {exc}", 502)

    set_candidate_status(candidate_id, "applied")
    if _wants_html(request):
        return _flash_redirect("/", "Price applied to Homebox")
    return {"result": "applied"}


@app.post("/api/candidates/{candidate_id}/reject")
def api_reject(candidate_id: int, request: Request) -> Any:
    _row, resp = _guard_pending(candidate_id, request)
    if resp:
        return resp
    set_candidate_status(candidate_id, "rejected")
    if _wants_html(request):
        return _flash_redirect("/", "Candidate rejected")
    return {"result": "rejected"}


@app.post("/api/candidates/{candidate_id}/relookup")
def api_relookup(
    candidate_id: int,
    request: Request,
    query: str | None = Form(None),
) -> Any:
    """Run a fresh price lookup and update the candidate in the DB.

    If a ``query`` form field is supplied (the editable search box in the queue),
    that text is used verbatim — handy when Homebox metadata is messy. Otherwise
    the item is re-fetched from Homebox and the query is rebuilt from its current
    name / manufacturer / model.
    """
    row, resp = _guard_pending(candidate_id, request)
    if resp:
        return resp

    settings = get_settings()
    custom = (query or "").strip()
    if custom:
        # Use the edited query verbatim — no Homebox round-trip.
        name = row["item_name"]
        search_query = custom
    else:
        try:
            item = HomeboxClient().get_item(row["homebox_id"])
        except HomeboxError as exc:
            return _fail(request, "/", f"Homebox error: {exc}", 502)
        name = item.get("name", "") or row["item_name"]
        search_query = build_search_query(item, settings.price_currency)

    result = lookup_price(search_query)
    refresh_candidate(
        candidate_id=candidate_id,
        item_name=name,
        query=search_query,
        price=result["price"],
        currency=result.get("currency", settings.price_currency),
        source_url=result["source_url"],
        confidence=result["confidence"],
        reason=result["reason"],
    )

    if _wants_html(request):
        return _flash_redirect("/", "Re-lookup complete")
    return {"result": "updated", **result}


class CandidateEdit(BaseModel):
    price: float
    source_url: str | None = None


@app.post("/api/candidates/{candidate_id}")
def api_edit(candidate_id: int, body: CandidateEdit) -> dict[str, str]:
    row = get_candidate(candidate_id)
    if not row:
        raise HTTPException(404, "Candidate not found")
    if row["status"] != "pending":
        raise HTTPException(400, f"Candidate is already {row['status']}")
    update_candidate_price(candidate_id, body.price, body.source_url)
    return {"result": "updated"}


def _wants_html(request: Request) -> bool:
    accept = request.headers.get("accept", "")
    return "text/html" in accept and "application/json" not in accept


def _flash_redirect(path: str, message: str, ok: bool = True) -> RedirectResponse:
    """303 redirect carrying a flash message (the PRG pattern used everywhere)."""
    ftype = "ok" if ok else "err"
    return RedirectResponse(
        f"{path}?flash={quote_plus(message)}&flash_type={ftype}", status_code=303
    )


def _fail(
    request: Request, path: str, message: str, status: int, detail: str | None = None
) -> RedirectResponse:
    """Content-negotiated failure: HTML clients get a flash redirect, JSON
    clients get an HTTPException (which is raised, not returned)."""
    if _wants_html(request):
        return _flash_redirect(path, message, ok=False)
    raise HTTPException(status, detail or message)


def _guard_pending(
    candidate_id: int, request: Request, path: str = "/"
) -> tuple[Any, RedirectResponse | None]:
    """Fetch a candidate and require it to be pending.

    Returns ``(row, None)`` on success, or ``(None, response)`` for HTML callers
    to return. For JSON callers the failure path raises HTTPException directly.
    """
    row = get_candidate(candidate_id)
    if not row:
        return None, _fail(request, path, "Candidate not found", 404)
    if row["status"] != "pending":
        return None, _fail(
            request, path, f"Already {row['status']}", 400,
            f"Candidate is already {row['status']}",
        )
    return row, None


# ---------------------------------------------------------------------------
# Ad-hoc lookup
# ---------------------------------------------------------------------------


class LookupRequest(BaseModel):
    query: str


@app.post("/api/lookup")
def api_lookup(body: LookupRequest) -> dict[str, Any]:
    """One-off price lookup. Searches DDG + Ollama; does not write to the DB."""
    if not body.query.strip():
        raise HTTPException(400, "query must not be empty")
    result = lookup_price(body.query.strip())
    return {"query": body.query.strip(), **result}


# ---------------------------------------------------------------------------
# Amazon import tab
# ---------------------------------------------------------------------------


@app.get("/amazon", response_class=HTMLResponse)
def amazon_page(
    request: Request,
    status: str | None = None,
    flash: str = "",
    flash_type: str = "ok",
) -> Any:
    orders_raw = list_amazon_orders(status=status)
    counts = count_amazon_by_status()

    # Enrich each row with the Homebox item name (not stored in DB — look up
    # from a single list_items call so we avoid N API calls).
    hb_names: dict[str, str] = {}
    if any(o["homebox_id"] for o in orders_raw):
        try:
            items = HomeboxClient().list_items()
            hb_names = {i["id"]: i.get("name", "") for i in items}
        except HomeboxError:
            pass

    orders = []
    for o in orders_raw:
        d = dict(o)
        d["homebox_name"] = hb_names.get(o["homebox_id"], "") if o["homebox_id"] else ""
        orders.append(d)

    return _TEMPLATES.TemplateResponse(
        request,
        "amazon.html",
        {
            "orders": orders,
            "counts": counts,
            "total": sum(counts.values()),
            "status_filter": status,
            "flash": {"type": flash_type, "message": flash} if flash else None,
        },
    )


@app.post("/api/amazon/import")
async def amazon_import(request: Request, file: UploadFile = File(...)) -> Any:
    """Parse an uploaded Amazon CSV, insert new rows, run fuzzy matching."""
    content = await file.read()
    try:
        orders = parse_amazon_csv(content)
    except ValueError as exc:
        return _fail(request, "/amazon", str(exc)[:120], 400, str(exc))

    inserted, skipped = upsert_amazon_orders(orders)

    # Run fuzzy matching only for newly-inserted rows (status=unmatched).
    unmatched = [dict(o) for o in list_amazon_orders(status="unmatched")]
    if unmatched:
        try:
            hb_items = HomeboxClient().list_items()
        except HomeboxError as exc:
            logger.warning("Homebox unavailable during Amazon import matching: %s", exc)
            hb_items = []

        if hb_items:
            matched_rows = match_orders_to_items(unmatched, hb_items)
            for order_dict, best_item, score in matched_rows:
                if best_item:
                    set_amazon_match(order_dict["id"], best_item["id"], score)

    if _wants_html(request):
        return _flash_redirect(
            "/amazon", f"Imported {inserted} new order(s); {skipped} already known."
        )
    return {"inserted": inserted, "skipped": skipped}


@app.post("/api/amazon/{order_id}/apply")
def amazon_apply(order_id: int, request: Request) -> Any:
    """Write purchasePrice, purchaseFrom, purchaseDate to the matched Homebox item."""
    row = get_amazon_order(order_id)
    if not row:
        raise HTTPException(404, "Order not found")
    if row["status"] not in ("matched", "unmatched"):
        raise HTTPException(400, f"Order is already {row['status']}")
    if not row["homebox_id"]:
        raise HTTPException(400, "No Homebox item matched to this order")
    if not row["unit_price"]:
        raise HTTPException(400, "No price on this order row")

    try:
        HomeboxClient().apply_item_purchase(
            row["homebox_id"], float(row["unit_price"]),
            row["seller"] or "Amazon", row["order_date"],
        )
    except HomeboxError as exc:
        return _fail(request, "/amazon", f"Homebox error: {str(exc)[:80]}", 502, str(exc))

    set_amazon_status(order_id, "applied")
    logger.info(
        "Amazon import applied: order %s → homebox %s price=%.2f",
        row["order_id"], row["homebox_id"], row["unit_price"],
    )

    if _wants_html(request):
        return _flash_redirect("/amazon", "Purchase data applied")
    return {"result": "applied"}


@app.post("/api/amazon/{order_id}/skip")
def amazon_skip(order_id: int, request: Request) -> Any:
    row = get_amazon_order(order_id)
    if not row:
        raise HTTPException(404, "Order not found")
    set_amazon_status(order_id, "skipped")
    if _wants_html(request):
        return _flash_redirect("/amazon", "Order skipped")
    return {"result": "skipped"}


def _parse_id_list(ids: str) -> list[int]:
    return [int(i) for i in (ids or "").split(",") if i.strip().isdigit()]


def _bulk_nothing_selected(request: Request) -> Any:
    """Graceful no-op when a bulk action is submitted with nothing selected."""
    if _wants_html(request):
        return _flash_redirect("/amazon", "Nothing selected", ok=False)
    return {"result": "ok", "count": 0}


@app.post("/api/amazon/bulk-skip")
def amazon_bulk_skip(request: Request, ids: str = Form("")) -> Any:
    """Skip all order IDs in the comma-separated ``ids`` field."""
    order_ids = _parse_id_list(ids)
    if not order_ids:
        return _bulk_nothing_selected(request)
    for oid in order_ids:
        set_amazon_status(oid, "skipped")
    if _wants_html(request):
        return _flash_redirect("/amazon", f"Skipped {len(order_ids)} order(s)")
    return {"result": "skipped", "count": len(order_ids)}


@app.post("/api/amazon/bulk-apply")
def amazon_bulk_apply(request: Request, ids: str = Form("")) -> Any:
    """Apply price to all matched+priced orders in the comma-separated ``ids`` field."""
    if not _parse_id_list(ids):
        return _bulk_nothing_selected(request)
    applied = skipped = 0
    hb = HomeboxClient()
    for oid in _parse_id_list(ids):
        row = get_amazon_order(oid)
        if not row or not row["homebox_id"] or not row["unit_price"]:
            skipped += 1
            continue
        try:
            hb.apply_item_purchase(
                row["homebox_id"], float(row["unit_price"]),
                row["seller"] or "Amazon", row["order_date"],
            )
            set_amazon_status(oid, "applied")
            applied += 1
        except HomeboxError as exc:
            logger.error("bulk-apply failed for order %d: %s", oid, exc)
            skipped += 1
    if _wants_html(request):
        msg = f"Applied {applied} order(s)" + (f", {skipped} skipped" if skipped else "")
        return _flash_redirect("/amazon", msg)
    return {"result": "ok", "applied": applied, "skipped": skipped}


@app.post("/api/amazon/bulk-create")
def amazon_bulk_create(request: Request, ids: str = Form("")) -> Any:
    """Create unmatched orders as new Homebox items in an "Amazon Imports" location.

    Homebox items require a real location (a null locationId 500s), so we route
    new items into a dedicated "Amazon Imports" location, created on first use.
    The user can re-file them into proper locations afterwards. Purchase price /
    seller / date are set with a follow-up PUT (ItemCreate doesn't accept them).
    """
    if not _parse_id_list(ids):
        return _bulk_nothing_selected(request)
    created = skipped = 0
    hb = HomeboxClient()
    try:
        location_id = hb.get_or_create_location("Amazon Imports")
    except HomeboxError as exc:
        logger.error("bulk-create could not resolve location: %s", exc)
        return _fail(request, "/amazon", f"Homebox error: {str(exc)[:80]}", 502, str(exc))

    for oid in _parse_id_list(ids):
        row = get_amazon_order(oid)
        if not row or row["homebox_id"]:
            skipped += 1
            continue
        try:
            new_item = hb.create_item(
                row["title"], location_id, quantity=row["quantity"] or 1
            )
            if row["unit_price"]:
                hb.apply_item_purchase(
                    new_item["id"], float(row["unit_price"]),
                    row["seller"] or "Amazon", row["order_date"],
                )
            set_amazon_match(oid, new_item["id"], 1.0)
            set_amazon_status(oid, "applied")
            created += 1
        except HomeboxError as exc:
            logger.error("bulk-create failed for order %d: %s", oid, exc)
            skipped += 1
    if _wants_html(request):
        msg = f"Created {created} item(s) in Homebox" + (f", {skipped} failed" if skipped else "")
        return _flash_redirect("/amazon", msg)
    return {"result": "ok", "created": created, "skipped": skipped}


# ---------------------------------------------------------------------------
# Sweep trigger
# ---------------------------------------------------------------------------


@app.post("/api/sweep")
async def api_sweep(request: Request) -> Any:
    counts = await run_sweep()
    if _wants_html(request):
        return _flash_redirect("/", "Sweep complete")
    return {"result": "ok", **counts}
