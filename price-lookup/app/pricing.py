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
import statistics
import time
from urllib.parse import quote_plus, urljoin
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


# --- staticICE: AU price-comparison aggregator ------------------------------
#
# staticICE.com.au indexes *only* Australian retailers and renders real prices
# in plain server-side HTML (no JS, no auth). For the common case — electronics,
# appliances, home goods — it gives a deterministic, AUD-denominated market
# price with zero LLM involvement. When it returns nothing (niche items,
# furniture, books) we fall through to the DDG + Ollama path below.
#
# The build sandbox can't reach the host, so the parser is markup-agnostic: it
# anchors on the price token ("$N.NN") and reads a stripped-tag window around it
# as the listing description, rather than depending on exact table/anchor
# structure that staticICE may change. A relevance filter (query-token overlap)
# drops nav/ad noise, and the median across matching listings resists outliers.

_STATICICE_HOST = "https://www.staticice.com.au"
_STATICICE_SEARCH = _STATICICE_HOST + "/cgi-bin/search.cgi"

# Minimum seconds between PricesAPI calls: personal plan allows 6 req/min.
# 10s gives a comfortable margin (6 req/min = 1 every 10s).
_PA_MIN_INTERVAL = 10.0
_pa_last_call: float = 0.0  # module-level; reset between processes, which is fine

_SI_PRICE_RE = re.compile(r"\$\s*([0-9][0-9,]*(?:\.[0-9]{1,2})?)")
_TAG_RE = re.compile(r"<[^>]+>")
_HREF_RE = re.compile(r'href=["\']([^"\']+)["\']', re.IGNORECASE)


def _strip_tags(html: str) -> str:
    return re.sub(r"\s+", " ", _TAG_RE.sub(" ", html)).strip()


def _significant_tokens(text: str) -> set[str]:
    """Lowercased alphanumeric tokens of length >= 4 (model numbers survive)."""
    return {t for t in re.findall(r"[a-z0-9]+", text.lower()) if len(t) >= 4}


def _parse_staticice(html: str) -> list[dict[str, Any]]:
    """Extract {price, description, url} listings from a staticICE results page.

    Markup-agnostic: staticICE lists each result as a ``$price`` followed by the
    product description and a retailer redirect link. We segment on the price
    tokens — each listing's description is the stripped text *between* its price
    and the next price — so neighbouring listings never bleed into one another.
    """
    matches = list(_SI_PRICE_RE.finditer(html))
    listings: list[dict[str, Any]] = []
    for i, m in enumerate(matches):
        price = _coerce_price(m.group(1).replace(",", ""))
        # Ignore sub-dollar noise and absurd values (page furniture, ads).
        if price is None or price < 1 or price > 1_000_000:
            continue
        seg_end = matches[i + 1].start() if i + 1 < len(matches) else m.end() + 300
        segment = html[m.end(): seg_end]
        desc = _strip_tags(segment)[:200]
        href = _HREF_RE.search(segment)
        url = urljoin(_STATICICE_HOST, href.group(1)) if href else ""
        listings.append({"price": price, "description": desc, "url": url})
    return listings


def _staticice_search(query: str, timeout: float) -> list[dict[str, Any]]:
    url = f"{_STATICICE_SEARCH}?q={quote_plus(query)}&spos=3"
    resp = httpx.get(
        url, timeout=timeout, follow_redirects=True,
        headers={"User-Agent": "Mozilla/5.0 (homebox-pricer)"},
    )
    resp.raise_for_status()
    return _parse_staticice(resp.text)


def _staticice_lookup(query: str, timeout: float) -> dict[str, Any] | None:
    """Primary price source. Returns a result dict, or None to fall through."""
    try:
        listings = _staticice_search(query, timeout)
    except httpx.HTTPError as exc:
        logger.warning("staticICE search failed for %r: %s", query, exc)
        return None
    except Exception as exc:  # never let a parser bug break the lookup
        logger.warning("staticICE parse error for %r: %s — falling back", query, exc)
        return None

    if not listings:
        return None

    # Score each listing by how many significant query tokens its description
    # contains, then keep the best-matching cluster. Requiring at least half of
    # the strongest match's overlap drops accessories/wrong products (a "$19.95
    # charging cable for headphones" matches one token; the real item matches
    # several) while tolerating wording differences between genuine listings.
    q_tokens = _significant_tokens(query)
    if q_tokens:
        scored = [(len(q_tokens & _significant_tokens(l["description"])), l) for l in listings]
        best = max(score for score, _ in scored)
        if best > 0:
            threshold = max(1, (best + 1) // 2)
            relevant = [l for score, l in scored if score >= threshold]
        else:
            relevant = listings
    else:
        relevant = listings

    prices = [l["price"] for l in relevant]
    price = round(statistics.median(prices), 2)
    # Source link: the listing closest to the median (most representative).
    rep = min(relevant, key=lambda l: abs(l["price"] - price))
    url = rep["url"] or f"{_STATICICE_SEARCH}?q={quote_plus(query)}"

    # staticICE is AU-only, so the price is AUD by construction. More
    # corroborating listings → higher confidence.
    confidence = "high" if len(relevant) >= 3 else "medium"
    return {
        "price": price,
        "currency": "AUD",
        "source_url": url,
        "confidence": confidence,
        "reason": f"Median of {len(relevant)} AU listing(s) on staticICE",
    }


# --- PricesAPI.io: paid multi-retailer product-search API -------------------
#
# A real-time product search that returns matched products, each with merchant
# offers inline. Country defaults to AU. It sits in the second tier — used only
# when staticICE has no match — so we don't spend quota on common electronics.
#
# Confirmed response shape (api.pricesapi.io, from official docs):
#   {success, data:{query, country, products:[...]}, meta}
# Each product: {pid, title, image, price, currency, source, rating, reviews,
#                offerCount, offers:[...], ...}
# Each offer has exactly 7 fields: seller, seller_url, price, currency,
#   shipping, condition, url.
# Auth: Authorization: Bearer <key>  (not x-api-key)
# Cold calls run the live pipeline (30–90s); warm cache hits < 100ms.
# Recommended client timeout: 95s.


def _pa_offers(result: dict[str, Any]) -> list[dict[str, Any]]:
    """Each offer has exactly: seller, seller_url, price, currency, shipping, condition, url."""
    val = result.get("offers")
    return [o for o in val if isinstance(o, dict)] if isinstance(val, list) else []


def _pricesapi_lookup(query: str, settings: Any) -> dict[str, Any] | None:
    """Second-tier price source. Returns a result dict, or None to fall through."""
    global _pa_last_call
    elapsed = time.monotonic() - _pa_last_call
    if elapsed < _PA_MIN_INTERVAL:
        wait = _PA_MIN_INTERVAL - elapsed
        logger.debug("PricesAPI rate-limit wait: %.1fs", wait)
        time.sleep(wait)

    try:
        resp = httpx.get(
            settings.prices_api_url,
            params={
                "q": query,
                "country": settings.prices_api_country,
                "limit": 5,
                "offers_limit": 20,  # collect all offers for a robust median
            },
            headers={"Authorization": f"Bearer {settings.prices_api_key}"},
            timeout=settings.prices_api_timeout,
        )
        resp.raise_for_status()
        data = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("PricesAPI request failed for %r: %s", query, exc)
        return None
    except Exception as exc:  # never let a parser bug break the lookup
        logger.warning("PricesAPI error for %r: %s — falling back", query, exc)
        return None
    finally:
        _pa_last_call = time.monotonic()

    # Confirmed shape: {data:{products:[...]}}. Tolerate "results" too.
    block = data.get("data", data) if isinstance(data, dict) else {}
    products = block.get("products") or block.get("results") if isinstance(block, dict) else None
    if not isinstance(products, list) or not products:
        return None

    # Pick the product whose title best matches the query, then collect every
    # price it carries (top-level + per-retailer offers) and take the median —
    # robust across retailers and against outliers.
    q_tokens = _significant_tokens(query)

    def _title_score(r: dict[str, Any]) -> int:
        return len(q_tokens & _significant_tokens(str(r.get("title", ""))))

    best = max((r for r in products if isinstance(r, dict)), key=_title_score, default=None)
    if best is None:
        return None

    prices: list[float] = []
    currency = best.get("currency")
    best_url = None

    # Collect all per-retailer offer prices (exact field names confirmed in docs).
    for offer in _pa_offers(best):
        p = _coerce_price(offer.get("price"))
        if p is None:
            continue
        prices.append(p)
        currency = currency or offer.get("currency")
        best_url = best_url or offer.get("url") or offer.get("seller_url")

    # Fall back to the candidate's headline price if offers are empty/degraded.
    if not prices:
        top = _coerce_price(best.get("price"))
        if top is not None:
            prices.append(top)

    if not prices:
        return None

    price = round(statistics.median(prices), 2)
    confidence = "high" if len(prices) >= 3 else "medium"
    return {
        "price": price,
        "currency": str(currency or settings.price_currency).upper(),
        "source_url": best_url,
        "confidence": confidence,
        "reason": f"Median of {len(prices)} PricesAPI offer(s) for {best.get('title', query)!r}"[:500],
    }


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
    """Find a price for ``query``. Suitable for /api/lookup and the sweep.

    Pipeline (each step falls through to the next if it yields no price):
      1. staticICE.com.au — AU price-comparison aggregator, deterministic, no LLM.
      2. PricesAPI.io — paid multi-retailer search (only if a key is configured).
      3. DDG search → scrape JSON-LD/og:meta off the top pages → Ollama parse.

    Returns a dict with keys: price, currency, source_url, confidence, reason.
    Never raises — errors are captured in the returned dict.
    """
    settings = get_settings()

    if settings.price_use_staticice:
        si = _staticice_lookup(query, settings.price_fetch_timeout)
        if si is not None and si["price"] is not None:
            logger.info(
                "Price result (staticICE) for %r: price=%s confidence=%s",
                query, si["price"], si["confidence"],
            )
            return si

    if settings.prices_api_key:
        pa = _pricesapi_lookup(query, settings)
        if pa is not None and pa["price"] is not None:
            logger.info(
                "Price result (PricesAPI) for %r: price=%s confidence=%s",
                query, pa["price"], pa["confidence"],
            )
            return pa

    return _ddg_ollama_lookup(query, settings)


def _ddg_ollama_lookup(query: str, settings: Any) -> dict[str, Any]:
    """DDG search → page scrape → Ollama parse. The fallback price source."""
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
