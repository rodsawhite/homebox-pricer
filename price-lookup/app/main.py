"""FastAPI app for the price-lookup sidecar."""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from . import __version__
from .config import get_settings
from .db import (
    count_by_status,
    get_candidate,
    init_db,
    list_candidates,
    set_candidate_status,
    update_candidate_price,
)
from .homebox import HomeboxClient, HomeboxError
from .pricing import lookup_price
from .scheduler import last_sweep, run_sweep, scheduler_loop

logging.basicConfig(level=logging.INFO)

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


def _thumbnail_url(homebox_id: str) -> str | None:
    """Return the Homebox attachment thumbnail URL for an item, or None."""
    try:
        item = HomeboxClient().get_item(homebox_id)
        attachments = item.get("attachments") or []
        for att in attachments:
            if att.get("type") == "photo":
                token = att.get("token") or ""
                if token:
                    settings = get_settings()
                    return f"{settings.homebox_url.rstrip('/')}/api/v1/attachments/{token}"
    except Exception:
        pass
    return None


@app.get("/", response_class=HTMLResponse)
def queue_page(request: Request, flash: str = "", flash_type: str = "ok") -> Any:
    candidates_raw = list_candidates(status="pending")
    candidates = []
    for row in candidates_raw:
        c = dict(row)
        c["thumbnail_url"] = _thumbnail_url(c["homebox_id"])
        candidates.append(c)

    sweep = last_sweep()
    return _TEMPLATES.TemplateResponse(
        request,
        "queue.html",
        {
            "candidates": candidates,
            "counts": count_by_status(),
            "last_sweep": sweep.strftime("%Y-%m-%d %H:%M UTC") if sweep else None,
            "flash": {"type": flash_type, "message": flash} if flash else None,
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
def api_list_candidates(status: str | None = None) -> list[dict[str, Any]]:
    return [dict(row) for row in list_candidates(status=status)]


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
# Sweep trigger
# ---------------------------------------------------------------------------


@app.post("/api/sweep")
async def api_sweep(request: Request) -> Any:
    counts = await run_sweep()
    if _wants_html(request):
        return RedirectResponse("/?flash=Sweep+complete&flash_type=ok", status_code=303)
    return {"result": "ok", **counts}
