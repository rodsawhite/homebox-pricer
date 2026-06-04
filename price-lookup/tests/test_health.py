"""Smoke + endpoint tests for the price-lookup service.

These run anywhere — no Docker daemon, no Homebox, no Ollama, no secrets.
The scheduler loop and Homebox thumbnail fetch are mocked.
"""

from __future__ import annotations

import importlib
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("HOMEBOX_URL", "http://homebox.test")
    monkeypatch.setenv("HOMEBOX_TOKEN", "test-token")


@pytest.fixture()
def client(_isolated_db):
    with (
        patch("app.scheduler.scheduler_loop", new_callable=AsyncMock),
        # Thumbnail fetch hits Homebox — stub it out so tests need no network.
        patch("app.main._thumbnail_url", return_value=None),
    ):
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


def test_queue_page_empty_state(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert "Price Review Queue" in resp.text
    assert "No pending candidates" in resp.text


def test_queue_page_shows_candidate(client):
    from app import db
    db.upsert_candidate(
        homebox_id="hb-1",
        item_name="Sony WH-1000XM5",
        query="Sony WH-1000XM5 price AUD",
        price=549.0,
        currency="AUD",
        source_url="https://www.jbhifi.com.au/products/sony-wh",
        confidence="high",
        reason="Listed at JB Hi-Fi",
    )
    resp = client.get("/")
    assert resp.status_code == 200
    assert "Sony WH-1000XM5" in resp.text
    assert "549" in resp.text
    assert "high" in resp.text


def test_queue_page_escapes_untrusted_names(client):
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


def test_candidates_empty(client):
    resp = client.get("/api/candidates")
    assert resp.status_code == 200
    assert resp.json() == []


def test_candidates_list(client):
    from app import db
    db.upsert_candidate(
        homebox_id="hb-2", item_name="Test Item", query="test",
        price=99.0, currency="AUD", source_url=None, confidence="low", reason="r",
    )
    resp = client.get("/api/candidates?status=pending")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]["item_name"] == "Test Item"


def test_approve_missing(client):
    resp = client.post("/api/candidates/9999/approve")
    assert resp.status_code == 404


def test_reject_missing(client):
    resp = client.post("/api/candidates/9999/reject")
    assert resp.status_code == 404


def test_reject_sets_status(client):
    from app import db
    db.upsert_candidate(
        homebox_id="hb-3", item_name="Reject Me", query="x",
        price=50.0, currency="AUD", source_url=None, confidence="low", reason="r",
    )
    rows = db.list_candidates(status="pending")
    cid = rows[0]["id"]
    resp = client.post(f"/api/candidates/{cid}/reject")
    assert resp.status_code == 200
    assert resp.json()["result"] == "rejected"
    updated = db.get_candidate(cid)
    assert updated["status"] == "rejected"


def test_edit_candidate(client):
    from app import db
    db.upsert_candidate(
        homebox_id="hb-4", item_name="Edit Me", query="x",
        price=None, currency="AUD", source_url=None, confidence="low", reason="r",
    )
    rows = db.list_candidates(status="pending")
    cid = rows[0]["id"]
    resp = client.post(
        f"/api/candidates/{cid}",
        json={"price": 299.99, "source_url": "https://example.com.au/"},
    )
    assert resp.status_code == 200
    updated = db.get_candidate(cid)
    assert updated["price"] == 299.99
    assert updated["source_url"] == "https://example.com.au/"


def test_approve_no_price_rejected(client):
    from app import db
    db.upsert_candidate(
        homebox_id="hb-5", item_name="No Price", query="x",
        price=None, currency="AUD", source_url=None, confidence="low", reason="r",
    )
    rows = db.list_candidates(status="pending")
    cid = rows[0]["id"]
    resp = client.post(f"/api/candidates/{cid}/approve")
    assert resp.status_code == 400


def test_lookup_empty_query(client):
    resp = client.post("/api/lookup", json={"query": "  "})
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Confidence pre-filter (queue page + API escape hatch)
# ---------------------------------------------------------------------------

def _seed_low_and_high(db):
    db.upsert_candidate(
        homebox_id="lo", item_name="Cheap Low", query="x", price=5.0,
        currency="AUD", source_url=None, confidence="low", reason="r",
    )
    db.upsert_candidate(
        homebox_id="hi", item_name="Solid High", query="x", price=50.0,
        currency="AUD", source_url=None, confidence="high", reason="r",
    )


def test_api_candidates_filters_below_threshold(client, monkeypatch):
    # Raise the floor to "high" — the low-confidence candidate is hidden.
    monkeypatch.setenv("PRICE_MIN_CONFIDENCE", "high")
    from app import db
    _seed_low_and_high(db)

    resp = client.get("/api/candidates?status=pending")
    names = {c["item_name"] for c in resp.json()}
    assert names == {"Solid High"}

    # Escape hatch returns everything.
    resp_all = client.get("/api/candidates?status=pending&include_all=true")
    names_all = {c["item_name"] for c in resp_all.json()}
    assert names_all == {"Solid High", "Cheap Low"}


def test_queue_page_hides_low_confidence(client, monkeypatch):
    monkeypatch.setenv("PRICE_MIN_CONFIDENCE", "high")
    from app import db
    _seed_low_and_high(db)

    resp = client.get("/")
    assert "Solid High" in resp.text
    assert "Cheap Low" not in resp.text
    assert "hidden below" in resp.text  # the filter notice

    resp_all = client.get("/?include_all=true")
    assert "Cheap Low" in resp_all.text
