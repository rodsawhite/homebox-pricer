"""Tests for pricing.py — no DDG, no Ollama, no network."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from app.pricing import (
    _coerce_confidence,
    _coerce_price,
    _format_results,
    _null_result,
    _parse_model_output,
    lookup_price,
)


# ---------------------------------------------------------------------------
# _coerce_price
# ---------------------------------------------------------------------------

def test_coerce_price_number():
    assert _coerce_price(549.0) == 549.0

def test_coerce_price_string_number():
    assert _coerce_price("299.99") == 299.99

def test_coerce_price_none():
    assert _coerce_price(None) is None

def test_coerce_price_zero():
    assert _coerce_price(0) is None

def test_coerce_price_negative():
    assert _coerce_price(-10) is None

def test_coerce_price_garbage():
    assert _coerce_price("not a number") is None


# ---------------------------------------------------------------------------
# _coerce_confidence — AUD disambiguation
# ---------------------------------------------------------------------------

def test_confidence_au_domain_preserved():
    assert _coerce_confidence("high", "https://www.jbhifi.com.au/products/x") == "high"

def test_confidence_aud_in_url_preserved():
    assert _coerce_confidence("medium", "https://example.com?currency=AUD") == "medium"

def test_confidence_no_au_signal_downgraded():
    # A high confidence but US/generic source should be downgraded.
    assert _coerce_confidence("high", "https://www.amazon.com/dp/xxx") == "low"

def test_confidence_medium_no_au_signal_downgraded():
    assert _coerce_confidence("medium", "https://bestbuy.com/product") == "low"

def test_confidence_low_stays_low():
    assert _coerce_confidence("low", "https://www.jbhifi.com.au/products/x") == "low"

def test_confidence_unknown_value_normalised():
    # Unknown values are conservatively normalised to "low" regardless of source.
    assert _coerce_confidence("very_high", "https://example.com.au/") == "low"

def test_confidence_none_defaults_low():
    assert _coerce_confidence(None, "") == "low"


# ---------------------------------------------------------------------------
# _parse_model_output
# ---------------------------------------------------------------------------

def test_parse_clean_json():
    raw = '{"price": 549.0, "currency": "AUD", "source": "https://jbhifi.com.au/", "confidence": "high", "reason": "Listed at JB Hi-Fi"}'
    result = _parse_model_output(raw)
    assert result["price"] == 549.0
    assert result["confidence"] == "high"

def test_parse_with_code_fence():
    raw = '```json\n{"price": 299, "currency": "AUD", "source": "", "confidence": "medium", "reason": "found on example.com"}\n```'
    result = _parse_model_output(raw)
    assert result["price"] == 299.0

def test_parse_with_preamble():
    raw = 'Sure, here is the result:\n{"price": null, "currency": "AUD", "source": "", "confidence": "low", "reason": "none found"}'
    result = _parse_model_output(raw)
    assert result["price"] is None

def test_parse_null_price():
    raw = '{"price": null, "currency": "AUD", "source": "", "confidence": "low", "reason": "no clear price"}'
    result = _parse_model_output(raw)
    assert result["price"] is None
    assert result["confidence"] == "low"

def test_parse_no_json_returns_null():
    result = _parse_model_output("I cannot find a price for this item.")
    assert result["price"] is None
    assert result["confidence"] == "low"

def test_parse_confidence_downgraded_for_non_au_source():
    # Model says high but source has no AU signal — should come back low.
    raw = '{"price": 499, "currency": "AUD", "source": "https://amazon.com/dp/x", "confidence": "high", "reason": "found on Amazon"}'
    result = _parse_model_output(raw)
    assert result["confidence"] == "low"

def test_parse_reason_truncated():
    long_reason = "x" * 600
    raw = f'{{"price": 100, "currency": "AUD", "source": "https://site.com.au/", "confidence": "high", "reason": "{long_reason}"}}'
    result = _parse_model_output(raw)
    assert len(result["reason"]) <= 500


# ---------------------------------------------------------------------------
# _format_results
# ---------------------------------------------------------------------------

def test_format_results_numbering():
    hits = [
        {"title": "Sony WH-1000XM5", "body": "Price $549", "href": "https://jbhifi.com.au/"},
        {"title": "Sony headphones", "body": "Compare prices", "href": "https://getpricelist.com.au/"},
    ]
    out = _format_results(hits)
    assert out.startswith("1.")
    assert "2." in out
    assert "jbhifi.com.au" in out


# ---------------------------------------------------------------------------
# lookup_price — full pipeline (mocked)
# ---------------------------------------------------------------------------

_MOCK_HITS = [
    {"title": "Sony WH-1000XM5 Headphones", "body": "Now $549 at JB Hi-Fi", "href": "https://www.jbhifi.com.au/products/sony-wh"},
]

_MOCK_OLLAMA_RESPONSE = {
    "response": '{"price": 549.0, "currency": "AUD", "source": "https://www.jbhifi.com.au/products/sony-wh", "confidence": "high", "reason": "Listed at JB Hi-Fi AU"}'
}


def test_lookup_price_happy_path():
    with (
        patch("app.pricing.DDGS") as mock_ddgs,
        patch("app.pricing.httpx.post") as mock_post,
        patch("app.pricing.time.sleep"),
    ):
        # DDG mock
        ctx = MagicMock()
        ctx.__enter__ = MagicMock(return_value=ctx)
        ctx.__exit__ = MagicMock(return_value=False)
        ctx.text.return_value = _MOCK_HITS
        mock_ddgs.return_value = ctx

        # Ollama mock
        mock_resp = MagicMock()
        mock_resp.json.return_value = _MOCK_OLLAMA_RESPONSE
        mock_resp.raise_for_status = MagicMock()
        mock_post.return_value = mock_resp

        result = lookup_price("Sony WH-1000XM5")

    assert result["price"] == 549.0
    assert result["confidence"] == "high"
    assert "jbhifi.com.au" in result["source_url"]


def test_lookup_price_ddg_failure_returns_null():
    with (
        patch("app.pricing.DDGS") as mock_ddgs,
        patch("app.pricing.time.sleep"),
    ):
        ctx = MagicMock()
        ctx.__enter__ = MagicMock(return_value=ctx)
        ctx.__exit__ = MagicMock(return_value=False)
        ctx.text.side_effect = Exception("rate limited")
        mock_ddgs.return_value = ctx

        result = lookup_price("Some Product")

    assert result["price"] is None
    assert result["confidence"] == "low"
    assert "search failed" in result["reason"]


def test_lookup_price_no_results_returns_null():
    with (
        patch("app.pricing.DDGS") as mock_ddgs,
        patch("app.pricing.time.sleep"),
    ):
        ctx = MagicMock()
        ctx.__enter__ = MagicMock(return_value=ctx)
        ctx.__exit__ = MagicMock(return_value=False)
        ctx.text.return_value = []
        mock_ddgs.return_value = ctx

        result = lookup_price("Obscure Item XYZ")

    assert result["price"] is None
    assert "no search results" in result["reason"]


def test_lookup_price_ollama_down_returns_null():
    import httpx as _httpx

    with (
        patch("app.pricing.DDGS") as mock_ddgs,
        patch("app.pricing.httpx.post", side_effect=_httpx.ConnectError("refused")),
        patch("app.pricing.time.sleep"),
    ):
        ctx = MagicMock()
        ctx.__enter__ = MagicMock(return_value=ctx)
        ctx.__exit__ = MagicMock(return_value=False)
        ctx.text.return_value = _MOCK_HITS
        mock_ddgs.return_value = ctx

        result = lookup_price("Sony WH-1000XM5")

    assert result["price"] is None
    assert result["confidence"] == "low"
