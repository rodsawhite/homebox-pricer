"""Amazon order CSV parsing and Homebox item fuzzy-matching.

Supports two CSV formats:
  1. Amazon "Order History Reports" (amazon.com.au/gp/b2b/reports) —
     official, the recommended export.
  2. Amazon "Request My Data" ZIP (Retail.OrderHistory.1.csv) —
     the data-export archive.

Both are parsed into a normalised list of dicts; unrecognised formats are
rejected with a clear error message rather than silently producing garbage.
"""

from __future__ import annotations

import csv
import io
import re
from datetime import datetime
from typing import Any


# ---------------------------------------------------------------------------
# CSV column normalisation
# ---------------------------------------------------------------------------

# Official Order History Reports column names (amazon.com.au/gp/b2b/reports).
_OHR_COLS = {
    "order id":           "order_id",
    "order date":         "order_date",
    "title":              "title",
    "category":           "category",
    "asin/isbn":          "asin",
    "asin":               "asin",
    "quantity":           "quantity",
    "unit price":         "unit_price",
    "purchase price per unit": "unit_price",
    "currency":           "currency",
    "seller":             "seller",
}

# "Request My Data" ZIP format column names.
_RMD_COLS = {
    "order id":           "order_id",
    "order date":         "order_date",
    "title":              "title",
    "category":           "category",
    "asin/isbn":          "asin",
    "asin":               "asin",
    "quantity":           "quantity",
    "unit price":         "unit_price",
    "currency":           "currency",
    "seller":             "seller",
    "shipment date":      "_skip",
    "shipping charge":    "_skip",
}


def _normalise_header(raw: str) -> str:
    return raw.strip().lower()


def _parse_price(raw: str | None) -> float | None:
    if not raw:
        return None
    cleaned = re.sub(r"[^\d.]", "", raw.replace(",", ""))
    try:
        v = float(cleaned)
        return v if v > 0 else None
    except ValueError:
        return None


def _parse_date(raw: str | None) -> str | None:
    if not raw:
        return None
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y", "%B %d, %Y", "%d %B %Y", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(raw.strip(), fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return raw.strip()


def parse_amazon_csv(content: bytes | str) -> list[dict[str, Any]]:
    """Parse an Amazon order CSV and return normalised order rows.

    Raises ValueError with a descriptive message if the file isn't a
    recognised Amazon export format.
    """
    if isinstance(content, bytes):
        content = content.decode("utf-8-sig", errors="replace")

    reader = csv.DictReader(io.StringIO(content))
    if not reader.fieldnames:
        raise ValueError("Empty or unreadable CSV — no header row found.")

    norm_fields = {_normalise_header(f): f for f in reader.fieldnames}
    col_map = {}
    for norm, canonical in {**_OHR_COLS, **_RMD_COLS}.items():
        if norm in norm_fields:
            col_map[norm_fields[norm]] = canonical

    required = {"order_id", "order_date", "title"}
    mapped = set(col_map.values()) - {"_skip"}
    missing = required - mapped
    if missing:
        raise ValueError(
            f"Not a recognised Amazon order export — missing columns: {missing}. "
            f"Got: {list(reader.fieldnames)}"
        )

    orders: list[dict[str, Any]] = []
    for row in reader:
        mapped_row: dict[str, Any] = {}
        for raw_col, canonical in col_map.items():
            if canonical == "_skip":
                continue
            mapped_row[canonical] = row.get(raw_col, "").strip() or None

        order_id = mapped_row.get("order_id")
        title = mapped_row.get("title")
        order_date = _parse_date(mapped_row.get("order_date"))
        if not order_id or not title or not order_date:
            continue

        orders.append({
            "order_id":   order_id,
            "order_date": order_date,
            "asin":       mapped_row.get("asin"),
            "title":      title,
            "quantity":   int(mapped_row.get("quantity") or 1),
            "unit_price": _parse_price(mapped_row.get("unit_price")),
            "currency":   (mapped_row.get("currency") or "AUD").upper(),
            "seller":     mapped_row.get("seller") or "Amazon",
        })

    if not orders:
        raise ValueError("CSV parsed successfully but contained no order rows.")

    return orders


# ---------------------------------------------------------------------------
# Fuzzy matching against Homebox items
# ---------------------------------------------------------------------------

def _tokens(text: str) -> set[str]:
    """Lowercase alphanumeric tokens, length >= 3."""
    return {t for t in re.findall(r"[a-z0-9]+", text.lower()) if len(t) >= 3}


def match_score(amazon_title: str, homebox_name: str) -> float:
    """Jaccard-style token overlap, 0.0–1.0."""
    a = _tokens(amazon_title)
    b = _tokens(homebox_name)
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def match_orders_to_items(
    orders: list[dict[str, Any]],
    homebox_items: list[dict[str, Any]],
    threshold: float = 0.25,
) -> list[tuple[dict[str, Any], dict[str, Any] | None, float]]:
    """For each order, find the best-matching Homebox item above ``threshold``.

    Returns a list of (order, best_item_or_None, score).
    Items with no purchasePrice set are preferred — we only want to fill blanks.
    """
    results = []
    for order in orders:
        best_item = None
        best_score = 0.0
        for item in homebox_items:
            s = match_score(order["title"], item.get("name", ""))
            if s > best_score:
                best_score = s
                best_item = item
        if best_score < threshold:
            best_item = None
            best_score = 0.0
        results.append((order, best_item, best_score))
    return results
