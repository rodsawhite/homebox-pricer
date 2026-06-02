"""FastAPI app for the price-lookup sidecar."""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from html import escape
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
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
from .scheduler import last_sweep, run_sweep, scheduler_loop

logging.basicConfig(level=logging.INFO)


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
def queue_page() -> str:
    candidates = list_candidates(status="pending")
    # Item names / source URLs originate from Homebox (ultimately from photo
    # capture + AI), so they are untrusted — escape before rendering to HTML.
    rows = "".join(
        f"""<tr>
          <td>{c['id']}</td>
          <td>{escape(str(c['item_name']))}</td>
          <td>{c['price'] if c['price'] is not None else '—'} {escape(str(c['currency']))}</td>
          <td>{escape(str(c['confidence']))}</td>
          <td>{escape(c['source_url']) if c['source_url'] else '—'}</td>
          <td>
            <form method="post" action="/api/candidates/{c['id']}/approve" style="display:inline">
              <button>Approve</button>
            </form>
            <form method="post" action="/api/candidates/{c['id']}/reject" style="display:inline">
              <button>Reject</button>
            </form>
          </td>
        </tr>"""
        for c in candidates
    )
    return f"""<!doctype html>
<html><head><title>Price Review Queue</title></head>
<body>
<h1>Price Review Queue</h1>
<p><a href="/status">Status JSON</a> | <a href="/api/candidates">All candidates (JSON)</a></p>
{"<p>No pending candidates.</p>" if not candidates else f"<table border=1><tr><th>ID</th><th>Item</th><th>Price</th><th>Confidence</th><th>Source</th><th>Actions</th></tr>{rows}</table>"}
</body></html>"""


# ---------------------------------------------------------------------------
# Candidates API
# ---------------------------------------------------------------------------


@app.get("/api/candidates")
def api_list_candidates(status: str | None = None) -> list[dict[str, Any]]:
    return [dict(row) for row in list_candidates(status=status)]


@app.post("/api/candidates/{candidate_id}/approve")
def api_approve(candidate_id: int) -> dict[str, str]:
    row = get_candidate(candidate_id)
    if not row:
        raise HTTPException(404, "Candidate not found")
    if row["status"] != "pending":
        raise HTTPException(400, f"Candidate is already {row['status']}")
    if row["price"] is None:
        raise HTTPException(400, "Cannot approve a candidate with no price — edit it first")

    try:
        HomeboxClient().apply_price(row["homebox_id"], row["price"])
    except HomeboxError as exc:
        raise HTTPException(502, f"Homebox error: {exc}") from exc

    set_candidate_status(candidate_id, "applied")
    return {"result": "applied"}


@app.post("/api/candidates/{candidate_id}/reject")
def api_reject(candidate_id: int) -> dict[str, str]:
    row = get_candidate(candidate_id)
    if not row:
        raise HTTPException(404, "Candidate not found")
    if row["status"] != "pending":
        raise HTTPException(400, f"Candidate is already {row['status']}")
    set_candidate_status(candidate_id, "rejected")
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


# ---------------------------------------------------------------------------
# Sweep trigger
# ---------------------------------------------------------------------------


@app.post("/api/sweep")
async def api_sweep() -> dict[str, Any]:
    counts = await run_sweep()
    return {"result": "ok", **counts}
