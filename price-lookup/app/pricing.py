"""Pricing pipeline: DDG search → Ollama price extraction.

External calls:
  - DuckDuckGo (DDGS.text) — no auth, rate-limited by a polite delay
  - Ollama HTTP API — local, no auth

Both are called synchronously but are only reached from the background
scheduler (or /api/lookup), never from a hot request path.
"""

from __future__ import annotations

import json
import logging
import re
import time
from typing import Any

import httpx

# The library was renamed duckduckgo_search → ddgs. The old name still imports
# but its backend now returns empty results, so prefer ddgs and fall back only
# for older environments.
try:
    from ddgs import DDGS
except ImportError:  # pragma: no cover - legacy fallback
    from duckduckgo_search import DDGS

from .config import get_settings

logger = logging.getLogger(__name__)

# Seconds to wait between DDG requests to avoid rate-limiting.
_DDG_DELAY = 2.0

# Bounded retry for transient DDG search failures (rate limits, blips).
_DDG_MAX_ATTEMPTS = 3
_DDG_BACKOFF_BASE = 1.0  # seconds: 1s, 2s, 4s

# Prompt template. The model is told to return *only* JSON so the parser
# can be strict. A trailing note about the current AUD bias is included
# because many results default to USD without it.
_PROMPT = """\
You are given web search results for a product. Determine the current NEW retail \
price in Australian dollars (AUD).

Rules:
- Prefer prices from .com.au domains or results that explicitly say AUD or A$.
- A bare "$" without an Australian source is ambiguous — set confidence "low".
- If no clear price is found, set price to null.
- Never invent a price; only report what is in the results.

Return ONLY this JSON object and nothing else:
{{"price": <number or null>, "currency": "AUD", "source": "<url or empty string>", \
"confidence": "high"|"medium"|"low", "reason": "<one sentence>"}}

Product: {query}

Search results:
{results}
"""


def _format_results(hits: list[dict[str, Any]]) -> str:
    lines = []
    for i, h in enumerate(hits, 1):
        title = h.get("title", "")
        body = h.get("body", "")
        url = h.get("href", "")
        block = f"{i}. {title}\n   {body}\n   {url}"
        # If we scraped a price off the actual page, surface it explicitly so
        # the model isn't guessing from a snippet that may omit the price.
        scraped = h.get("scraped_price")
        if scraped is not None:
            cur = h.get("scraped_currency") or ""
            block += f"\n   PAGE PRICE: {cur} {scraped}".rstrip()
        lines.append(block)
    return "\n\n".join(lines)


# --- Structured price scraping ---------------------------------------------
#
# E-commerce pages embed the price in machine-readable form far more reliably
# than DDG puts it in a snippet. We read it from JSON-LD (schema.org Offer) and
# Open Graph / product meta tags using only stdlib regex+json (no HTML parser
# dependency). This is deterministic; the LLM becomes a fallback, not the sole
# extractor.

_JSONLD_RE = re.compile(
    r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
    re.DOTALL | re.IGNORECASE,
)
_META_PRICE_RE = re.compile(
    r'<meta[^>]+(?:property|name|itemprop)=["\']'
    r'(?:og:price:amount|product:price:amount|price)["\']'
    r'[^>]*content=["\']([0-9][0-9,\.]*)["\']',
    re.IGNORECASE,
)
_META_CURRENCY_RE = re.compile(
    r'<meta[^>]+(?:property|name|itemprop)=["\']'
    r'(?:og:price:currency|product:price:currency|priceCurrency)["\']'
    r'[^>]*content=["\']([A-Za-z]{3})["\']',
    re.IGNORECASE,
)


def _price_from_jsonld(html: str) -> tuple[float | None, str | None]:
    """Pull the first schema.org Offer price/currency from JSON-LD blocks."""
    for block in _JSONLD_RE.findall(html):
        try:
            data = json.loads(block.strip())
        except json.JSONDecodeError:
            continue
        for node in _iter_nodes(data):
            if not isinstance(node, dict):
                continue
            offers = node.get("offers")
            for offer in _iter_nodes(offers):
                if not isinstance(offer, dict):
                    continue
                price = offer.get("price") or offer.get("lowPrice")
                if price is not None:
                    return _coerce_price(price), offer.get("priceCurrency")
    return None, None


def _iter_nodes(data: Any) -> list[Any]:
    """Flatten JSON-LD that may be a dict, a list, or use an @graph wrapper."""
    if data is None:
        return []
    if isinstance(data, list):
        out: list[Any] = []
        for d in data:
            out.extend(_iter_nodes(d))
        return out
    if isinstance(data, dict) and isinstance(data.get("@graph"), list):
        return _iter_nodes(data["@graph"])
    return [data]


def _extract_structured_price(html: str) -> tuple[float | None, str | None]:
    """Best-effort price+currency from a product page. Returns (None, None)."""
    price, currency = _price_from_jsonld(html)
    if price is not None:
        return price, currency
    m = _META_PRICE_RE.search(html)
    if m:
        price = _coerce_price(m.group(1).replace(",", ""))
        cur_m = _META_CURRENCY_RE.search(html)
        return price, (cur_m.group(1) if cur_m else None)
    return None, None


def _scrape_prices(hits: list[dict[str, Any]], limit: int, timeout: float) -> None:
    """Fetch the top ``limit`` result pages and annotate hits in-place with
    scraped_price / scraped_currency. Failures are swallowed per-page so a
    dead link never breaks the lookup."""
    for h in hits[:limit]:
        url = h.get("href", "")
        if not url:
            continue
        try:
            resp = httpx.get(
                url, timeout=timeout, follow_redirects=True,
                headers={"User-Agent": "Mozilla/5.0 (homebox-pricer)"},
            )
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            logger.debug("Page fetch failed for %s: %s", url, exc)
            continue
        price, currency = _extract_structured_price(resp.text)
        if price is not None:
            h["scraped_price"] = price
            h["scraped_currency"] = currency
            logger.info("Scraped price %s %s from %s", currency or "", price, url)


def _ollama_extract(prompt: str) -> dict[str, Any]:
    """Send prompt to Ollama and return parsed JSON. Never raises on bad JSON."""
    settings = get_settings()
    url = f"{settings.ollama_url}/api/generate"
    payload = {
        "model": settings.price_text_model,
        "prompt": prompt,
        "stream": False,
        "options": {"num_ctx": 4096, "temperature": 0},
    }
    try:
        resp = httpx.post(url, json=payload, timeout=60)
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        logger.error("Ollama request failed: %s", exc)
        return _null_result("Ollama unreachable")

    raw = resp.json().get("response", "")
    return _parse_model_output(raw)


def _parse_model_output(raw: str) -> dict[str, Any]:
    """Extract JSON from model output, tolerating code fences and stray text."""
    # Strip markdown code fences if present
    text = re.sub(r"```(?:json)?", "", raw).strip()
    # Find the first { ... } block
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        logger.warning("Model returned no JSON: %r", raw[:200])
        return _null_result("model returned no JSON")
    try:
        data = json.loads(match.group())
    except json.JSONDecodeError as exc:
        logger.warning("JSON parse error: %s — raw: %r", exc, raw[:200])
        return _null_result("JSON parse error")

    return {
        "price": _coerce_price(data.get("price")),
        "currency": str(data.get("currency", "AUD")),
        "source_url": str(data.get("source", "")) or None,
        "confidence": _coerce_confidence(data.get("confidence"), data.get("source", "")),
        "reason": str(data.get("reason", ""))[:500],
    }


def _coerce_price(raw: Any) -> float | None:
    if raw is None:
        return None
    try:
        v = float(raw)
        return v if v > 0 else None
    except (TypeError, ValueError):
        return None


def _coerce_confidence(raw: Any, source: str) -> str:
    """Downgrade to 'low' when the source is not clearly Australian."""
    level = str(raw).lower() if raw else "low"
    if level not in ("high", "medium", "low"):
        level = "low"
    # If confidence is high/medium but source has no AU signal, downgrade.
    if level != "low":
        au_signals = (".com.au", "aud", "a$", ".au/")
        src_lower = source.lower()
        if not any(sig in src_lower for sig in au_signals):
            logger.debug(
                "Downgrading confidence %s→low: no AU signal in source %r", level, source
            )
            level = "low"
    return level


def _ddg_search(query: str, region: str) -> list[dict[str, Any]]:
    """Run a DDG text search with bounded exponential backoff.

    Raises the last exception if every attempt fails — the caller turns that
    into a null result so the sweep never crashes.
    """
    last_exc: Exception | None = None
    for attempt in range(_DDG_MAX_ATTEMPTS):
        try:
            with DDGS() as ddgs:
                return list(ddgs.text(query, region=region, max_results=8))
        except Exception as exc:  # DDGS raises a variety of error types
            last_exc = exc
            if attempt == _DDG_MAX_ATTEMPTS - 1:
                raise
            delay = _DDG_BACKOFF_BASE * (2 ** attempt)
            logger.warning(
                "DDG search failed for %r (attempt %d/%d): %s — retrying in %.0fs",
                query, attempt + 1, _DDG_MAX_ATTEMPTS, exc, delay,
            )
            time.sleep(delay)
    raise last_exc  # pragma: no cover — loop either returns or raises above


def _null_result(reason: str) -> dict[str, Any]:
    return {
        "price": None,
        "currency": "AUD",
        "source_url": None,
        "confidence": "low",
        "reason": reason,
    }


def lookup_price(query: str) -> dict[str, Any]:
    """Search DDG and extract a price. Suitable for /api/lookup and the sweep.

    Returns a dict with keys: price, currency, source_url, confidence, reason.
    Never raises — errors are captured in the returned dict.
    """
    settings = get_settings()
    ddg_query = query if settings.price_currency.upper() in query.upper() else (
        query + f" {settings.price_currency}"
    )

    logger.info("DDG search: %r", ddg_query)
    try:
        hits = _ddg_search(ddg_query, settings.price_region)
    except Exception as exc:
        logger.warning("DDG search failed for %r: %s", query, exc)
        return _null_result(f"search failed: {exc}")

    if not hits:
        logger.info("No DDG results for %r", query)
        return _null_result("no search results")

    # Scrape structured prices off the top result pages so the model sees real
    # prices, not just snippets that often omit them.
    if settings.price_fetch_pages > 0:
        try:
            _scrape_prices(hits, settings.price_fetch_pages, settings.price_fetch_timeout)
        except Exception as exc:  # never let scraping break a lookup
            logger.warning("Price scraping error (continuing): %s", exc)

    formatted = _format_results(hits)
    prompt = _PROMPT.format(query=query, results=formatted)
    result = _ollama_extract(prompt)

    # Deterministic fallback: if the model found nothing but we scraped a price
    # off a page, use the best scraped AU price rather than returning null.
    if result["price"] is None:
        fallback = _best_scraped(hits)
        if fallback is not None:
            price, currency, url = fallback
            result = {
                "price": price,
                "currency": currency or settings.price_currency,
                "source_url": url,
                "confidence": _coerce_confidence("medium", url),
                "reason": "Scraped from product page structured data",
            }

    # Polite delay before the next request
    time.sleep(_DDG_DELAY)

    logger.info(
        "Price result for %r: price=%s confidence=%s",
        query, result["price"], result["confidence"],
    )
    return result


def _best_scraped(hits: list[dict[str, Any]]) -> tuple[float, str | None, str] | None:
    """Return (price, currency, url) for the best scraped hit, preferring an
    AU source, else the first scraped price. None if nothing was scraped."""
    scraped = [h for h in hits if h.get("scraped_price") is not None]
    if not scraped:
        return None
    au_signals = (".com.au", ".au/", "aud")
    for h in scraped:
        url = h.get("href", "")
        cur = (h.get("scraped_currency") or "").lower()
        if cur == "aud" or any(sig in url.lower() for sig in au_signals):
            return h["scraped_price"], h.get("scraped_currency"), url
    h = scraped[0]
    return h["scraped_price"], h.get("scraped_currency"), h.get("href", "")
