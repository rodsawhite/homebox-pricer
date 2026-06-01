"""FastAPI app for the price-lookup sidecar.

Phase 1 scope: a runnable service with a liveness probe and a minimal status
endpoint. The scan/search/queue/review machinery lands in later phases; this
file is the stable entrypoint they hang off.
"""

from __future__ import annotations

from fastapi import FastAPI

from . import __version__
from .config import get_settings

app = FastAPI(title="price-lookup", version=__version__)


@app.get("/health")
def health() -> dict[str, str]:
    """Liveness probe. Returns 200 with a small JSON body when the app is up."""
    return {"status": "ok", "version": __version__}


@app.get("/status")
def status() -> dict[str, object]:
    """Coarse status: config echo + placeholder counts.

    Counts are zeroed until the SQLite queue exists (Phase 2). The shape is
    stable so the review UI and tests can rely on it now.
    """
    settings = get_settings()
    return {
        "version": __version__,
        "homebox_url": settings.homebox_url,
        "price_text_model": settings.price_text_model,
        "check_interval": settings.check_interval,
        "last_sweep": None,
        "counts": {"pending": 0, "applied": 0, "rejected": 0},
    }
