"""Smoke + endpoint tests for the price-lookup service.

These run anywhere — no Docker daemon, no Homebox, no Ollama, no secrets.
The scheduler loop is mocked so no background tasks actually run.
"""

from __future__ import annotations

import os
import importlib
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    """Point the DB at a temp file and stub out the scheduler loop."""
    monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("HOMEBOX_URL", "http://homebox.test")
    monkeypatch.setenv("HOMEBOX_TOKEN", "test-token")
    # Clear the settings cache so each test gets fresh env
    import app.config as cfg_mod
    cfg_mod.get_settings.cache_clear() if hasattr(cfg_mod.get_settings, "cache_clear") else None


@pytest.fixture()
def client(_isolated_db):
    with patch("app.scheduler.scheduler_loop", new_callable=AsyncMock):
        import app.main as main_mod
        importlib.reload(main_mod)
        with TestClient(main_mod.app) as c:
            yield c


def test_app_imports_and_has_version():
    from app import __version__
    assert isinstance(__version__, str) and __version__


def test_health_returns_ok(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert "version" in body


def test_status_shape(client):
    resp = client.get("/status")
    assert resp.status_code == 200
    body = resp.json()
    assert set(body["counts"]) == {"pending", "applied", "rejected"}
    assert body["counts"]["pending"] == 0
    assert body["last_sweep"] is None
    assert "homebox_url" in body
    assert "price_text_model" in body


def test_queue_page(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert "Price Review Queue" in resp.text


def test_candidates_empty(client):
    resp = client.get("/api/candidates")
    assert resp.status_code == 200
    assert resp.json() == []


def test_queue_page_escapes_untrusted_names(client):
    """An item name with HTML must be escaped, not rendered as markup."""
    from app import db

    db.upsert_candidate(
        homebox_id="evil-1",
        item_name="<script>alert(1)</script>",
        query="x",
        price=10.0,
        currency="AUD",
        source_url="http://x/?a=1&b=2",
        confidence="low",
        reason="test",
    )
    resp = client.get("/")
    assert resp.status_code == 200
    assert "<script>alert(1)</script>" not in resp.text
    assert "&lt;script&gt;" in resp.text


def test_approve_missing(client):
    resp = client.post("/api/candidates/9999/approve")
    assert resp.status_code == 404


def test_reject_missing(client):
    resp = client.post("/api/candidates/9999/reject")
    assert resp.status_code == 404
