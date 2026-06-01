"""Smoke + endpoint tests for the Phase 1 service.

These run anywhere — no Docker daemon, no Homebox, no Ollama, no secrets.
They confirm the app imports/compiles and the HTTP surface returns the
expected shapes.
"""

from fastapi.testclient import TestClient

from app import __version__
from app.main import app

client = TestClient(app)


def test_app_imports_and_has_version():
    assert isinstance(__version__, str) and __version__


def test_health_returns_ok():
    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["version"] == __version__


def test_status_shape():
    resp = client.get("/status")
    assert resp.status_code == 200
    body = resp.json()
    # Stable keys the review UI / later phases depend on.
    assert set(body["counts"]) == {"pending", "applied", "rejected"}
    assert body["counts"]["pending"] == 0
    assert body["last_sweep"] is None
    assert "homebox_url" in body
    assert "price_text_model" in body
