"""Phase 5 hardening tests: retry/backoff, token expiry, confidence filter,
query building, and structured logging.

All external I/O (httpx, DDGS, sleep) is mocked — no network, no Homebox,
no Ollama, no secrets.
"""

from __future__ import annotations

import json
import logging
from unittest.mock import MagicMock, patch

import httpx
import pytest

import app.homebox as homebox
import app.pricing as pricing
from app.db import confidence_rank, list_candidates, upsert_candidate
from app.homebox import HomeboxAuthError, HomeboxClient, HomeboxError
from app.logging_config import JsonFormatter, configure_logging


# ---------------------------------------------------------------------------
# Fixtures: isolate config + DB so a HomeboxClient has a static token.
# ---------------------------------------------------------------------------

@pytest.fixture()
def static_token_env(monkeypatch):
    monkeypatch.setenv("HOMEBOX_URL", "http://homebox.test")
    monkeypatch.setenv("HOMEBOX_TOKEN", "static-token")
    monkeypatch.setenv("HOMEBOX_USER", "")
    monkeypatch.setenv("HOMEBOX_PASSWORD", "")


@pytest.fixture()
def creds_env(monkeypatch):
    monkeypatch.setenv("HOMEBOX_URL", "http://homebox.test")
    monkeypatch.setenv("HOMEBOX_TOKEN", "warm-token")
    monkeypatch.setenv("HOMEBOX_USER", "me@example.com")
    monkeypatch.setenv("HOMEBOX_PASSWORD", "secret")


@pytest.fixture()
def isolated_db(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "h.db"))
    from app.db import init_db
    init_db()


@pytest.fixture(autouse=True)
def _reset_last_sweep():
    """run_sweep() sets a module global; restore it so we don't leak the
    last-sweep timestamp into other test modules that assert it is None."""
    import app.scheduler as sched
    saved = sched._last_sweep
    yield
    sched._last_sweep = saved


def _resp(status_code: int, json_body=None) -> MagicMock:
    r = MagicMock(spec=httpx.Response)
    r.status_code = status_code
    r.json.return_value = json_body if json_body is not None else {}
    r.text = ""
    return r


# ---------------------------------------------------------------------------
# Retry / backoff (Homebox) — mocked sleep
# ---------------------------------------------------------------------------

def test_retry_succeeds_after_transient_5xx(static_token_env):
    client = HomeboxClient()
    # First call 503, second 200 with a page of items.
    responses = [_resp(503), _resp(200, {"items": [{"id": "a"}]})]
    with (
        patch("app.homebox.httpx.get", side_effect=responses) as mget,
        patch("app.homebox.time.sleep") as msleep,
    ):
        # list_items pages until empty; stub page 2 as empty.
        responses.append(_resp(200, {"items": []}))
        items = client.list_items()
    assert items == [{"id": "a"}]
    assert msleep.called  # backed off on the 503
    assert mget.call_count >= 2


def test_retry_succeeds_after_connection_error(static_token_env):
    client = HomeboxClient()
    side = [httpx.ConnectError("refused"), _resp(200, {"id": "x", "name": "n"})]
    with (
        patch("app.homebox.httpx.get", side_effect=side),
        patch("app.homebox.time.sleep") as msleep,
    ):
        item = client.get_item("x")
    assert item == {"id": "x", "name": "n"}
    assert msleep.call_count == 1


def test_retry_exhausts_and_raises_homeboxerror(static_token_env):
    client = HomeboxClient()
    with (
        patch("app.homebox.httpx.get", side_effect=httpx.ConnectError("down")),
        patch("app.homebox.time.sleep"),
    ):
        with pytest.raises(HomeboxError):
            client.get_item("x")


def test_backoff_delays_are_exponential(static_token_env):
    client = HomeboxClient()
    delays = []
    with (
        patch("app.homebox.httpx.get", side_effect=httpx.ConnectError("down")),
        patch("app.homebox.time.sleep", side_effect=lambda d: delays.append(d)),
    ):
        with pytest.raises(HomeboxError):
            client.get_item("x")
    # 3 attempts => sleeps after attempt 0 and 1: 1s then 2s.
    assert delays == [1.0, 2.0]


def test_4xx_not_retried(static_token_env):
    """A 404 is a hard error — returned immediately, never backed off."""
    client = HomeboxClient()
    with (
        patch("app.homebox.httpx.get", return_value=_resp(404)) as mget,
        patch("app.homebox.time.sleep") as msleep,
    ):
        with pytest.raises(HomeboxError):
            client.get_item("missing")
    assert msleep.call_count == 0
    assert mget.call_count == 1


# ---------------------------------------------------------------------------
# Token expiry handling
# ---------------------------------------------------------------------------

def test_401_no_creds_raises_auth_error(static_token_env):
    client = HomeboxClient()
    with (
        patch("app.homebox.httpx.get", return_value=_resp(401)),
        patch("app.homebox.time.sleep"),
    ):
        with pytest.raises(HomeboxAuthError):
            client.get_item("x")


def test_auth_error_is_homebox_error_subclass():
    assert issubclass(HomeboxAuthError, HomeboxError)


def test_401_with_creds_refreshes_and_retries(creds_env):
    client = HomeboxClient()
    # First GET 401, then after refresh a 200.
    with (
        patch("app.homebox.httpx.get", side_effect=[_resp(401), _resp(200, {"id": "x"})]),
        patch.object(client, "_login") as mlogin,
        patch("app.homebox.time.sleep"),
    ):
        item = client.get_item("x")
    assert item == {"id": "x"}
    assert mlogin.call_count == 1


def test_scheduler_pauses_on_auth_error(static_token_env, isolated_db):
    import asyncio
    from app.scheduler import run_sweep

    with patch("app.scheduler.HomeboxClient") as mclient:
        instance = mclient.return_value
        instance.list_items.side_effect = HomeboxAuthError("401 expired")
        counts = asyncio.run(run_sweep())
    # Graceful pause: no crash, zero work done.
    assert counts == {"scanned": 0, "queued": 0, "skipped": 0}


# ---------------------------------------------------------------------------
# Retry / backoff (DDG search)
# ---------------------------------------------------------------------------

def _ddg_ctx(text_side_effect=None, text_return=None):
    ctx = MagicMock()
    ctx.__enter__ = MagicMock(return_value=ctx)
    ctx.__exit__ = MagicMock(return_value=False)
    if text_side_effect is not None:
        ctx.text.side_effect = text_side_effect
    else:
        ctx.text.return_value = text_return or []
    return ctx


def test_ddg_retry_succeeds_after_blip():
    hits = [{"title": "t", "body": "b", "href": "https://x.com.au/"}]
    ctx = _ddg_ctx(text_side_effect=[Exception("rate limited"), hits])
    delays = []
    with (
        patch("app.pricing.DDGS", return_value=ctx),
        patch("app.pricing.time.sleep", side_effect=lambda d: delays.append(d)),
    ):
        out = pricing._ddg_search("query", "au-en")
    assert out == hits
    assert delays == [1.0]  # one backoff before the successful retry


def test_ddg_retry_exhausts_and_raises():
    ctx = _ddg_ctx(text_side_effect=Exception("blocked"))
    with (
        patch("app.pricing.DDGS", return_value=ctx),
        patch("app.pricing.time.sleep"),
    ):
        with pytest.raises(Exception):
            pricing._ddg_search("query", "au-en")


# ---------------------------------------------------------------------------
# Query building (sweep)
# ---------------------------------------------------------------------------

def test_sweep_builds_query_from_item_fields(static_token_env, isolated_db):
    import asyncio
    from app.scheduler import run_sweep

    item = {
        "id": "item-1", "name": "Headphones",
        "manufacturer": "Sony", "modelNumber": "WH-1000XM5",
        "purchasePrice": 0,
    }
    captured = {}

    def fake_lookup(q):
        captured["query"] = q
        return {"price": 549.0, "currency": "AUD", "source_url": None,
                "confidence": "high", "reason": "ok"}

    with (
        patch("app.scheduler.HomeboxClient") as mclient,
        patch("app.scheduler.lookup_price", side_effect=fake_lookup),
    ):
        mclient.return_value.list_items.return_value = [item]

        asyncio.run(run_sweep())

    # manufacturer, name, model joined, then "price AUD" appended.
    assert captured["query"] == "Sony Headphones WH-1000XM5 price AUD"


def test_sweep_skips_priced_and_pending(static_token_env, isolated_db):
    import asyncio
    from app.scheduler import run_sweep

    items = [
        {"id": "priced", "name": "A", "purchasePrice": 10},
        {"id": "free", "name": "B", "purchasePrice": 0},
    ]
    with (
        patch("app.scheduler.HomeboxClient") as mclient,
        patch("app.scheduler.lookup_price", return_value={
            "price": None, "currency": "AUD", "source_url": None,
            "confidence": "low", "reason": "r"}),
    ):
        mclient.return_value.list_items.return_value = items

        counts = asyncio.run(run_sweep())
    assert counts["scanned"] == 2
    assert counts["queued"] == 1
    assert counts["skipped"] == 1


# ---------------------------------------------------------------------------
# Confidence threshold pre-filter
# ---------------------------------------------------------------------------

def test_confidence_rank_ordering():
    assert confidence_rank("low") < confidence_rank("medium") < confidence_rank("high")
    assert confidence_rank(None) == confidence_rank("low")
    assert confidence_rank("bogus") == 0


def _seed_three(db_isolated=None):
    for hb, conf in (("c-low", "low"), ("c-med", "medium"), ("c-high", "high")):
        upsert_candidate(
            homebox_id=hb, item_name=hb, query="q", price=10.0,
            currency="AUD", source_url=None, confidence=conf, reason="r",
        )


def test_filter_medium_hides_low(isolated_db):
    _seed_three()
    rows = list_candidates(status="pending", min_confidence="medium")
    confs = {r["confidence"] for r in rows}
    assert confs == {"medium", "high"}


def test_filter_high_only(isolated_db):
    _seed_three()
    rows = list_candidates(status="pending", min_confidence="high")
    assert {r["confidence"] for r in rows} == {"high"}


def test_filter_none_returns_all(isolated_db):
    _seed_three()
    rows = list_candidates(status="pending")
    assert len(rows) == 3


def test_filter_low_returns_all(isolated_db):
    _seed_three()
    rows = list_candidates(status="pending", min_confidence="low")
    assert len(rows) == 3


# ---------------------------------------------------------------------------
# Structured logging
# ---------------------------------------------------------------------------

def test_json_formatter_emits_valid_json_with_extras():
    fmt = JsonFormatter()
    record = logging.LogRecord(
        name="app.scheduler", level=logging.INFO, pathname="x", lineno=1,
        msg="Sweep complete", args=(), exc_info=None,
    )
    record.scanned = 5
    record.queued = 2
    line = fmt.format(record)
    payload = json.loads(line)
    assert payload["level"] == "INFO"
    assert payload["logger"] == "app.scheduler"
    assert payload["message"] == "Sweep complete"
    assert payload["scanned"] == 5
    assert payload["queued"] == 2
    assert "ts" in payload


def test_configure_logging_json_installs_json_formatter():
    configure_logging("json")
    root = logging.getLogger()
    assert len(root.handlers) == 1
    assert isinstance(root.handlers[0].formatter, JsonFormatter)
    # Restore plain so we don't leak JSON formatting into other tests.
    configure_logging("plain")


def test_configure_logging_plain_default():
    configure_logging("plain")
    root = logging.getLogger()
    assert len(root.handlers) == 1
    assert not isinstance(root.handlers[0].formatter, JsonFormatter)
