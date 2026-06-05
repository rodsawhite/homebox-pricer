"""Amazon order CSV parsing and Homebox item fuzzy-matching.

Supports two CSV formats:
  1. "Request My Data" export (amazon.com.au → Account → Privacy Central →
     Request My Data → "Your Orders"). The archive's Retail.OrderHistory.1.csv
     is the current official way to export order history — Amazon retired the
     old "Order History Reports" page in 2023.
  2. Legacy "Order History Reports" CSV / third-party exporter extensions,
     which use slightly different column names (Title, ASIN/ISBN, Seller).

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

# Maps normalised (lowercased, stripped) source headers → our canonical names.
# Deliberately broad so it accepts all the common exporters without per-tool
# config: Amazon's official Retail.OrderHistory.1.csv, the legacy Order History
# Reports CSV, the azad extension ("Amazon Order History Reporter", which uses
# `description`/`price`), and other browser exporters.
_COLUMN_MAP = {
    # --- identifiers ---
    "order id":                 "order_id",
    "order #":                  "order_id",
    "order date":               "order_date",
    "date":                     "order_date",
    # --- product title (Request-My-Data: "Product Name"; legacy: "Title";
    #     azad Items: "description"; azad Orders: "items") ---
    "product name":             "title",
    "title":                    "title",
    "description":              "title",
    "item name":                "title",
    "name":                     "title",
    "items":                    "title",
    # --- product id ---
    "asin":                     "asin",
    "asin/isbn":                "asin",
    # --- quantity ---
    "quantity":                 "quantity",
    "qty":                      "quantity",
    # --- price (azad Items: "price"; azad Orders: "total" — note the Orders
    #     report's total is the whole-order amount, not per-item) ---
    "unit price":               "unit_price",
    "purchase price per unit":  "unit_price",
    "price":                    "unit_price",
    "item total":               "unit_price",
    "amount":                   "unit_price",
    "total":                    "unit_price",
    "order total":              "unit_price",
    "total owed":               "total_owed",
    "currency":                 "currency",
    # --- seller (Request-My-Data has no seller column) ---
    "seller":                   "seller",
    "sold by":                  "seller",
    # --- status (used to drop cancelled orders when present) ---
    "order status":             "order_status",
    "status":                   "order_status",
}


def _normalise_header(raw: str) -> str:
    return raw.strip().lower()


def _parse_price(raw: str | None) -> float | None:
    """Parse a monetary value. Returns None for blanks, zero, or non-numeric
    tokens like "1Audiblecredit" (digital orders) — only a clean number counts."""
    if not raw:
        return None
    cleaned = raw.replace("$", "").replace(",", "").strip()
    # Reject anything that isn't purely a number (drops "1 Audible credit" etc.)
    if not re.fullmatch(r"\d+(\.\d+)?", cleaned):
        return None
    v = float(cleaned)
    return v if v > 0 else None


def _parse_date(raw: str | None) -> str | None:
    if not raw:
        return None
    raw = raw.strip()
    # Request-My-Data uses ISO 8601 with a trailing Z (e.g. 2024-03-15T10:23:45Z).
    iso = raw.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(iso).strftime("%Y-%m-%d")
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y", "%B %d, %Y", "%d %B %Y"):
        try:
            return datetime.strptime(raw, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return raw


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
    col_map = {
        norm_fields[norm]: canonical
        for norm, canonical in _COLUMN_MAP.items()
        if norm in norm_fields
    }

    required = {"order_id", "order_date", "title"}
    mapped = set(col_map.values())
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
            mapped_row[canonical] = (row.get(raw_col) or "").strip() or None

        # Skip cancelled orders when the export tells us the status.
        status = (mapped_row.get("order_status") or "").lower()
        if "cancel" in status:
            continue

        order_id = mapped_row.get("order_id")
        title = mapped_row.get("title")
        order_date = _parse_date(mapped_row.get("order_date"))
        # Skip a repeated header row (some exporters append the header again at
        # the end of the file) and any row missing the essentials.
        if not order_id or not title or not order_date:
            continue
        if order_id.strip().lower() == "order id":
            continue

        # Prefer the per-unit price; fall back to "Total Owed" if that's all
        # the export gives (some rows have no unit price).
        unit_price = _parse_price(mapped_row.get("unit_price"))
        if unit_price is None:
            unit_price = _parse_price(mapped_row.get("total_owed"))

        orders.append({
            "order_id":   order_id,
            # Default ASIN to "" (not None): SQLite treats NULLs as distinct in
            # the UNIQUE(order_id, asin) index, which would break dedup on
            # exports that omit ASIN (e.g. azad's Orders report).
            "asin":       mapped_row.get("asin") or "",
            "order_date": order_date,
            "title":      title,
            "quantity":   int(mapped_row.get("quantity") or 1),
            "unit_price": unit_price,
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
