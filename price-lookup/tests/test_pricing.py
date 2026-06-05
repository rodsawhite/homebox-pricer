"""Tests for pricing.py — no DDG, no Ollama, no network."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
import app.pricing as _pricing_module

import httpx


@pytest.fixture(autouse=True)
def reset_pa_last_call():
    """Reset PricesAPI rate-limit timestamp between tests so no test bleeds into the next."""
    _pricing_module._pa_last_call = 0.0
    yield
    _pricing_module._pa_last_call = 0.0

from app.pricing import (
    _coerce_confidence,
    _coerce_price,
    _extract_structured_price,
    _format_results,
    _null_result,
    _parse_model_output,
    _parse_staticice,
    _pricesapi_lookup,
    _staticice_lookup,
    lookup_price,
)
from app.config import Settings


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
        patch("app.pricing._staticice_lookup", return_value=None),
        patch("app.pricing.DDGS") as mock_ddgs,
        patch("app.pricing.httpx.post") as mock_post,
        # Skip page scraping by failing the fetch — model result stands.
        patch("app.pricing.httpx.get", side_effect=httpx.ConnectError("no net")),
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
        patch("app.pricing._staticice_lookup", return_value=None),
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
        patch("app.pricing._staticice_lookup", return_value=None),
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


# ---------------------------------------------------------------------------
# _extract_structured_price — JSON-LD and meta tags
# ---------------------------------------------------------------------------

def test_extract_price_from_jsonld():
    html = """
    <html><head>
    <script type="application/ld+json">
    {"@type": "Product", "name": "Rice Cooker",
     "offers": {"@type": "Offer", "price": "129.00", "priceCurrency": "AUD"}}
    </script></head></html>
    """
    price, currency = _extract_structured_price(html)
    assert price == 129.0
    assert currency == "AUD"


def test_extract_price_from_jsonld_graph_and_list_offers():
    html = """
    <script type="application/ld+json">
    {"@graph": [{"@type": "Product",
      "offers": [{"@type": "Offer", "price": 89.95, "priceCurrency": "AUD"}]}]}
    </script>
    """
    price, currency = _extract_structured_price(html)
    assert price == 89.95
    assert currency == "AUD"


def test_extract_price_from_og_meta():
    html = '<meta property="og:price:amount" content="1,299.00">' \
           '<meta property="og:price:currency" content="AUD">'
    price, currency = _extract_structured_price(html)
    assert price == 1299.0
    assert currency == "AUD"


def test_extract_price_none_when_absent():
    price, currency = _extract_structured_price("<html><body>no price here</body></html>")
    assert price is None
    assert currency is None


def test_lookup_price_scrape_fallback_when_model_null():
    """Model returns null, but a page scrape yields an AU price — use it."""
    html = ('<script type="application/ld+json">'
            '{"@type":"Product","offers":{"price":"467.96","priceCurrency":"AUD"}}'
            '</script>')
    null_ollama = {"response": '{"price": null, "currency": "AUD", "source": "", "confidence": "low", "reason": "none"}'}
    with (
        patch("app.pricing._staticice_lookup", return_value=None),
        patch("app.pricing.DDGS") as mock_ddgs,
        patch("app.pricing.httpx.post") as mock_post,
        patch("app.pricing.httpx.get") as mock_get,
        patch("app.pricing.time.sleep"),
    ):
        ctx = MagicMock()
        ctx.__enter__ = MagicMock(return_value=ctx)
        ctx.__exit__ = MagicMock(return_value=False)
        ctx.text.return_value = [
            {"title": "Rice Cooker", "body": "no price in snippet",
             "href": "https://www.house.com.au/products/rice-cooker"},
        ]
        mock_ddgs.return_value = ctx

        post_resp = MagicMock()
        post_resp.json.return_value = null_ollama
        post_resp.raise_for_status = MagicMock()
        mock_post.return_value = post_resp

        get_resp = MagicMock()
        get_resp.text = html
        get_resp.raise_for_status = MagicMock()
        mock_get.return_value = get_resp

        result = lookup_price("Breville Rice Master")

    assert result["price"] == 467.96
    assert result["currency"] == "AUD"
    assert "house.com.au" in result["source_url"]


def test_lookup_price_ollama_down_returns_null():
    import httpx as _httpx

    with (
        patch("app.pricing._staticice_lookup", return_value=None),
        patch("app.pricing.DDGS") as mock_ddgs,
        patch("app.pricing.httpx.post", side_effect=_httpx.ConnectError("refused")),
        patch("app.pricing.httpx.get", side_effect=_httpx.ConnectError("no net")),
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


# ---------------------------------------------------------------------------
# staticICE parsing + lookup
# ---------------------------------------------------------------------------

# Representative staticICE results markup: several retailers, one price each,
# plus an unrelated accessory and some page-chrome "$" noise.
_STATICICE_HTML = """
<html><body>
<div class="banner">Compare prices and save $5 today!</div>
<table>
 <tr><td><a href="/cgi-bin/redirect.cgi?u=jbhifi">$549.00</a></td>
     <td>Sony WH-1000XM5 Wireless Noise Cancelling Headphones - JB Hi-Fi</td></tr>
 <tr><td><a href="/cgi-bin/redirect.cgi?u=harvey">$558.00</a></td>
     <td>Sony WH1000XM5 Headphones Black - Harvey Norman</td></tr>
 <tr><td><a href="/cgi-bin/redirect.cgi?u=bing">$539.00</a></td>
     <td>Sony WH-1000XM5 Over Ear Headphones - Bing Lee</td></tr>
 <tr><td><a href="/cgi-bin/redirect.cgi?u=cable">$19.95</a></td>
     <td>Replacement USB-C charging cable for headphones</td></tr>
</table>
</body></html>
"""


def test_parse_staticice_extracts_listings():
    listings = _parse_staticice(_STATICICE_HTML)
    prices = {l["price"] for l in listings}
    # The three headphone listings + the cable; banner "$5" is sub-dollar noise
    # only if < 1 — $5 is >= 1 so it is captured, but lacks query tokens.
    assert 549.0 in prices
    assert 558.0 in prices
    assert 539.0 in prices
    # First listing carries an absolute retailer redirect URL.
    first = next(l for l in listings if l["price"] == 549.0)
    assert first["url"].startswith("https://www.staticice.com.au/")
    assert "Sony" in first["description"]


def test_staticice_lookup_medians_relevant_listings():
    with patch("app.pricing.httpx.get") as mock_get:
        resp = MagicMock()
        resp.text = _STATICICE_HTML
        resp.raise_for_status = MagicMock()
        mock_get.return_value = resp

        result = _staticice_lookup("Sony WH-1000XM5 headphones", timeout=8.0)

    # Median of the three relevant headphone prices (539, 549, 558) = 549.0.
    # The $19.95 cable and "$5" banner lack query tokens and are excluded.
    assert result["price"] == 549.0
    assert result["currency"] == "AUD"
    assert result["confidence"] == "high"  # >= 3 corroborating listings
    assert "staticice" in result["source_url"].lower()


def test_staticice_lookup_returns_none_on_http_error():
    with patch("app.pricing.httpx.get", side_effect=httpx.ConnectError("no net")):
        assert _staticice_lookup("anything", timeout=8.0) is None


def test_staticice_lookup_returns_none_when_empty():
    with patch("app.pricing.httpx.get") as mock_get:
        resp = MagicMock()
        resp.text = "<html><body>no results found</body></html>"
        resp.raise_for_status = MagicMock()
        mock_get.return_value = resp
        assert _staticice_lookup("obscure xyz", timeout=8.0) is None


# ---------------------------------------------------------------------------
# PricesAPI.io (second tier)
# ---------------------------------------------------------------------------

# Exact confirmed shape from PricesAPI docs.
# Each offer has exactly: seller, seller_url, price, currency, shipping, condition, url.
_PRICESAPI_JSON = {
    "success": True,
    "data": {
        "query": "Breville rice cooker",
        "country": "au",
        "products": [
            {
                "position": 1,
                "pid": 12345,
                "title": "Breville Rice Master 7 Cup Rice Cooker",
                "price": 129.0,
                "currency": "AUD",
                "source": "The Good Guys",
                "offerCount": 3,
                "offers": [
                    {"seller": "The Good Guys", "seller_url": "https://thegoodguys.com.au",
                     "price": 129.0, "currency": "AUD", "shipping": 0, "condition": "New",
                     "url": "https://www.thegoodguys.com.au/x"},
                    {"seller": "JB Hi-Fi", "seller_url": "https://www.jbhifi.com.au",
                     "price": 135.0, "currency": "AUD", "shipping": 0, "condition": "New",
                     "url": "https://www.jbhifi.com.au/y"},
                    {"seller": "Bunnings", "seller_url": "https://www.bunnings.com.au",
                     "price": 119.0, "currency": "AUD", "shipping": None, "condition": "New",
                     "url": "https://www.bunnings.com.au/z"},
                ],
            },
            {"position": 2, "title": "Rice cooker spare lid", "price": 9.95,
             "currency": "AUD", "offerCount": 0, "offers": []},
        ],
    },
    "meta": {"latency_ms": 41000, "cache_source": "miss"},
}


def _pa_settings():
    return Settings(prices_api_key="pricesapi_test", prices_api_timeout=90.0)


def test_pricesapi_lookup_medians_best_match_offers():
    with patch("app.pricing.httpx.get") as mock_get:
        resp = MagicMock()
        resp.json.return_value = _PRICESAPI_JSON
        resp.raise_for_status = MagicMock()
        mock_get.return_value = resp

        result = _pricesapi_lookup("Breville Rice Master rice cooker", _pa_settings())

    # Median of 119/129/135 from the best-matching product (not the spare lid).
    assert result["price"] == 129.0
    assert result["currency"] == "AUD"
    assert result["confidence"] == "high"
    assert result["source_url"].startswith("https://")

    # Auth header must be Bearer, and offers_limit must be requested.
    call_kwargs = mock_get.call_args
    assert call_kwargs.kwargs["headers"]["Authorization"].startswith("Bearer pricesapi_")
    assert call_kwargs.kwargs["params"]["offers_limit"] == 20


def test_pricesapi_lookup_falls_back_to_headline_price_when_offers_empty():
    """degraded=true response: offers[] empty, headline price still usable."""
    data = {
        "success": True,
        "data": {"products": [{"title": "Breville Rice Master", "price": 129.0,
                                "currency": "AUD", "offerCount": 0, "offers": []}]},
        "meta": {"degraded": True, "cache_source": "miss"},
    }
    with patch("app.pricing.httpx.get") as mock_get:
        resp = MagicMock()
        resp.json.return_value = data
        resp.raise_for_status = MagicMock()
        mock_get.return_value = resp
        result = _pricesapi_lookup("Breville Rice Master", _pa_settings())
    assert result["price"] == 129.0
    assert result["confidence"] == "medium"  # only 1 price point


def test_pricesapi_lookup_none_on_http_error():
    with patch("app.pricing.httpx.get", side_effect=httpx.ConnectError("no net")):
        assert _pricesapi_lookup("anything", _pa_settings()) is None


def test_pricesapi_lookup_rate_limit_wait():
    """A second call within the window triggers a sleep for the remaining gap."""
    import app.pricing as _pricing

    # Monotonic always returns 1000.0; last call was 3s ago → expect ~7s sleep.
    fake_now = 1000.0
    with (
        patch("app.pricing.time.monotonic", return_value=fake_now),
        patch("app.pricing.time.sleep") as mock_sleep,
        patch("app.pricing.httpx.get") as mock_get,
    ):
        _pricing._pa_last_call = fake_now - 3.0  # 3s ago, inside the 10s window

        resp = MagicMock()
        resp.json.return_value = {"data": {"products": []}}
        resp.raise_for_status = MagicMock()
        mock_get.return_value = resp

        _pricesapi_lookup("anything", _pa_settings())

    mock_sleep.assert_called_once()
    wait = mock_sleep.call_args[0][0]
    assert abs(wait - 7.0) < 0.1  # _PA_MIN_INTERVAL(10) - elapsed(3)


def test_pricesapi_lookup_none_when_no_results():
    with patch("app.pricing.httpx.get") as mock_get:
        resp = MagicMock()
        resp.json.return_value = {"success": True, "data": {"results": []}}
        resp.raise_for_status = MagicMock()
        mock_get.return_value = resp
        assert _pricesapi_lookup("obscure", _pa_settings()) is None


def test_pricesapi_lookup_none_when_offers_unparseable():
    """Unknown offer field names → no price → fall through (returns None)."""
    with patch("app.pricing.httpx.get") as mock_get:
        resp = MagicMock()
        resp.json.return_value = {
            "data": {"results": [{"title": "Widget", "offers": [{"cost_in_cents": 1299}]}]}
        }
        resp.raise_for_status = MagicMock()
        mock_get.return_value = resp
        assert _pricesapi_lookup("Widget", _pa_settings()) is None


def test_lookup_price_uses_pricesapi_when_staticice_misses():
    """staticICE returns nothing, a key is set → PricesAPI is used, model skipped."""
    with (
        patch("app.pricing.get_settings", return_value=_pa_settings()),
        patch("app.pricing._staticice_lookup", return_value=None),
        patch("app.pricing.httpx.get") as mock_get,
        patch("app.pricing.DDGS") as mock_ddgs,
        patch("app.pricing.httpx.post") as mock_post,
        patch("app.pricing.time.sleep"),
    ):
        resp = MagicMock()
        resp.json.return_value = _PRICESAPI_JSON
        resp.raise_for_status = MagicMock()
        mock_get.return_value = resp

        result = lookup_price("Breville Rice Master rice cooker")

    assert result["price"] == 129.0
    mock_ddgs.assert_not_called()
    mock_post.assert_not_called()


def test_lookup_price_skips_pricesapi_without_key():
    """No key configured → PricesAPI never called, falls straight to DDG path."""
    with (
        patch("app.pricing.get_settings", return_value=Settings(prices_api_key="")),
        patch("app.pricing._staticice_lookup", return_value=None),
        patch("app.pricing._pricesapi_lookup") as mock_pa,
        patch("app.pricing.DDGS") as mock_ddgs,
        patch("app.pricing.httpx.post", side_effect=httpx.ConnectError("x")),
        patch("app.pricing.httpx.get", side_effect=httpx.ConnectError("x")),
        patch("app.pricing.time.sleep"),
    ):
        ctx = MagicMock()
        ctx.__enter__ = MagicMock(return_value=ctx)
        ctx.__exit__ = MagicMock(return_value=False)
        ctx.text.return_value = _MOCK_HITS
        mock_ddgs.return_value = ctx

        lookup_price("Sony WH-1000XM5")

    mock_pa.assert_not_called()


def test_lookup_price_uses_staticice_first_skipping_model():
    """When staticICE yields a price, DDG and Ollama are never touched."""
    with (
        patch("app.pricing.httpx.get") as mock_get,
        patch("app.pricing.DDGS") as mock_ddgs,
        patch("app.pricing.httpx.post") as mock_post,
        patch("app.pricing.time.sleep"),
    ):
        resp = MagicMock()
        resp.text = _STATICICE_HTML
        resp.raise_for_status = MagicMock()
        mock_get.return_value = resp

        result = lookup_price("Sony WH-1000XM5 headphones")

    assert result["price"] == 549.0
    assert result["confidence"] == "high"
    # The model/search fallback must not have been invoked.
    mock_ddgs.assert_not_called()
    mock_post.assert_not_called()
