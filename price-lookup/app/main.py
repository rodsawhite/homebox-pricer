"""FastAPI app for the price-lookup sidecar."""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

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
        return RedirectResponse("/?flash=Candidate+not+found&flash_type=err", status_code=303)
    if row["status"] != "pending":
        return RedirectResponse(
            f"/?flash=Already+{row['status']}&flash_type=err", status_code=303
        )
    update_candidate_price(candidate_id, price, source_url or None)
    return RedirectResponse("/?flash=Price+updated&flash_type=ok", status_code=303)


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
    row = get_candidate(candidate_id)
    if not row:
        if _wants_html(request):
            return RedirectResponse("/?flash=Candidate+not+found&flash_type=err", status_code=303)
        raise HTTPException(404, "Candidate not found")
    if row["status"] != "pending":
        if _wants_html(request):
            return RedirectResponse(
                f"/?flash=Already+{row['status']}&flash_type=err", status_code=303
            )
        raise HTTPException(400, f"Candidate is already {row['status']}")
    if row["price"] is None:
        if _wants_html(request):
            return RedirectResponse(
                "/?flash=Set+a+price+before+approving&flash_type=err", status_code=303
            )
        raise HTTPException(400, "Cannot approve a candidate with no price — edit it first")

    try:
        HomeboxClient().apply_price(row["homebox_id"], row["price"])
    except HomeboxError as exc:
        if _wants_html(request):
            return RedirectResponse(
                f"/?flash=Homebox+error:+{exc}&flash_type=err", status_code=303
            )
        raise HTTPException(502, f"Homebox error: {exc}") from exc

    set_candidate_status(candidate_id, "applied")
    if _wants_html(request):
        return RedirectResponse("/?flash=Price+applied+to+Homebox&flash_type=ok", status_code=303)
    return {"result": "applied"}


@app.post("/api/candidates/{candidate_id}/reject")
def api_reject(candidate_id: int, request: Request) -> Any:
    row = get_candidate(candidate_id)
    if not row:
        if _wants_html(request):
            return RedirectResponse("/?flash=Candidate+not+found&flash_type=err", status_code=303)
        raise HTTPException(404, "Candidate not found")
    if row["status"] != "pending":
        if _wants_html(request):
            return RedirectResponse(
                f"/?flash=Already+{row['status']}&flash_type=err", status_code=303
            )
        raise HTTPException(400, f"Candidate is already {row['status']}")
    set_candidate_status(candidate_id, "rejected")
    if _wants_html(request):
        return RedirectResponse("/?flash=Candidate+rejected&flash_type=ok", status_code=303)
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
    row = get_candidate(candidate_id)
    if not row:
        if _wants_html(request):
            return RedirectResponse("/?flash=Candidate+not+found&flash_type=err", status_code=303)
        raise HTTPException(404, "Candidate not found")
    if row["status"] != "pending":
        if _wants_html(request):
            return RedirectResponse(
                f"/?flash=Already+{row['status']}&flash_type=err", status_code=303
            )
        raise HTTPException(400, f"Candidate is already {row['status']}")

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
            if _wants_html(request):
                return RedirectResponse(
                    f"/?flash=Homebox+error:+{exc}&flash_type=err", status_code=303
                )
            raise HTTPException(502, f"Homebox error: {exc}") from exc
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
        return RedirectResponse("/?flash=Re-lookup+complete&flash_type=ok", status_code=303)
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
        if _wants_html(request):
            return RedirectResponse(
                f"/amazon?flash={str(exc)[:120]}&flash_type=err", status_code=303
            )
        raise HTTPException(400, str(exc)) from exc

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

    msg = f"Imported {inserted} new order(s); {skipped} already known."
    if _wants_html(request):
        return RedirectResponse(
            f"/amazon?flash={msg}&flash_type=ok", status_code=303
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
        client = HomeboxClient()
        item = client.get_item(row["homebox_id"])
        from .homebox import _build_put_payload
        payload = _build_put_payload(item)
        payload["purchasePrice"] = row["unit_price"]
        payload["purchaseFrom"] = row["seller"] or "Amazon"
        if row["order_date"]:
            payload["purchaseTime"] = row["order_date"] + "T00:00:00Z"
        client.put_item(row["homebox_id"], payload)
    except HomeboxError as exc:
        if _wants_html(request):
            return RedirectResponse(
                f"/amazon?flash=Homebox+error:+{str(exc)[:80]}&flash_type=err", status_code=303
            )
        raise HTTPException(502, str(exc)) from exc

    set_amazon_status(order_id, "applied")
    logger.info(
        "Amazon import applied: order %s → homebox %s price=%.2f",
        row["order_id"], row["homebox_id"], row["unit_price"],
    )

    if _wants_html(request):
        return RedirectResponse("/amazon?flash=Purchase+data+applied&flash_type=ok", status_code=303)
    return {"result": "applied"}


@app.post("/api/amazon/{order_id}/skip")
def amazon_skip(order_id: int, request: Request) -> Any:
    row = get_amazon_order(order_id)
    if not row:
        raise HTTPException(404, "Order not found")
    set_amazon_status(order_id, "skipped")
    if _wants_html(request):
        return RedirectResponse("/amazon", status_code=303)
    return {"result": "skipped"}


# ---------------------------------------------------------------------------
# Sweep trigger
# ---------------------------------------------------------------------------


@app.post("/api/sweep")
async def api_sweep(request: Request) -> Any:
    counts = await run_sweep()
    if _wants_html(request):
        return RedirectResponse("/?flash=Sweep+complete&flash_type=ok", status_code=303)
    return {"result": "ok", **counts}
