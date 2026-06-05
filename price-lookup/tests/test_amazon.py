"""Tests for Amazon CSV parsing, fuzzy matching, and import endpoints."""

from __future__ import annotations

import importlib
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app.amazon import match_score, match_orders_to_items, parse_amazon_csv


# ---------------------------------------------------------------------------
# CSV parsing
# ---------------------------------------------------------------------------

# Current official "Request My Data" export: Retail.OrderHistory.1.csv.
# Real column set (subset) — note "Product Name" (not "Title"), ISO dates with
# Z, an "Order Status" column, and no "Seller" column.
_RMD_CSV = """\
"Website","Order ID","Order Date","Currency","Unit Price","Total Owed","ASIN","Quantity","Order Status","Product Name"
"Amazon.com.au","123-4567890-1234567","2024-03-15T10:23:45Z","AUD","399.00","399.00","B09XS7JWHH","1","Shipped","Sony WH-1000XM5 Wireless Headphones"
"Amazon.com.au","123-4567890-9999999","2024-01-10T08:00:00Z","AUD","129.95","129.95","B00ABC123","1","Shipped","Breville BRC460 Rice Cooker"
"Amazon.com.au","123-0000000-0000000","2024-02-01T00:00:00Z","AUD","49.00","49.00","B0CANCEL00","1","Cancelled","Cancelled Item Should Be Skipped"
"""

# Legacy / third-party exporter format: Title, ASIN/ISBN, Seller columns.
_LEGACY_CSV = """\
Order ID,Order Date,Title,Category,ASIN/ISBN,Quantity,Unit Price,Currency,Seller
123-0000001-0000001,2023-11-20,TP-Link TL-SG108S-M2 2.5G Switch,Electronics,B0CXXX123,1,89.00,AUD,Amazon AU
"""


def test_parse_rmd_csv():
    orders = parse_amazon_csv(_RMD_CSV)
    # Cancelled order is dropped → 2 not 3.
    assert len(orders) == 2
    assert orders[0]["order_id"] == "123-4567890-1234567"
    assert orders[0]["title"] == "Sony WH-1000XM5 Wireless Headphones"
    assert orders[0]["unit_price"] == 399.0
    assert orders[0]["currency"] == "AUD"
    assert orders[0]["order_date"] == "2024-03-15"  # ISO Z stripped
    assert orders[0]["asin"] == "B09XS7JWHH"
    assert orders[0]["seller"] == "Amazon"  # no seller column → default


def test_parse_rmd_skips_cancelled():
    orders = parse_amazon_csv(_RMD_CSV)
    titles = [o["title"] for o in orders]
    assert "Cancelled Item Should Be Skipped" not in titles


def test_parse_legacy_csv():
    orders = parse_amazon_csv(_LEGACY_CSV)
    assert len(orders) == 1
    assert orders[0]["order_id"] == "123-0000001-0000001"
    assert orders[0]["title"] == "TP-Link TL-SG108S-M2 2.5G Switch"
    assert orders[0]["unit_price"] == 89.0
    assert orders[0]["seller"] == "Amazon AU"


# azad extension ("Amazon Order History Reporter") Items export: uses
# "description" for the title and "price" for the amount.
_AZAD_CSV = """\
order id,date,description,quantity,price,ASIN
249-1112223-3334445,2024-05-02,Logitech MX Master 3S Mouse,1,159.00,B0B11NG89C
"""


def test_parse_azad_csv():
    orders = parse_amazon_csv(_AZAD_CSV)
    assert len(orders) == 1
    assert orders[0]["title"] == "Logitech MX Master 3S Mouse"
    assert orders[0]["unit_price"] == 159.0
    assert orders[0]["order_date"] == "2024-05-02"
    assert orders[0]["asin"] == "B0B11NG89C"


def test_parse_csv_bytes():
    orders = parse_amazon_csv(_RMD_CSV.encode("utf-8"))
    assert len(orders) == 2


def test_parse_csv_utf8_bom():
    orders = parse_amazon_csv(b"\xef\xbb\xbf" + _RMD_CSV.encode("utf-8"))
    assert len(orders) == 2


def test_parse_csv_bad_format():
    with pytest.raises(ValueError, match="recognised Amazon"):
        parse_amazon_csv("Name,Qty,Colour\nWidget,1,Blue\n")


def test_parse_csv_empty_body():
    with pytest.raises(ValueError):
        parse_amazon_csv("Order ID,Order Date,Title\n")  # header only, no rows


def test_parse_price_with_commas():
    csv = _RMD_CSV.replace("399.00", "1,399.00")
    orders = parse_amazon_csv(csv)
    assert orders[0]["unit_price"] == 1399.0


# ---------------------------------------------------------------------------
# Fuzzy matching
# ---------------------------------------------------------------------------

def test_match_score_exact():
    assert match_score("Sony WH-1000XM5", "Sony WH-1000XM5") == 1.0


def test_match_score_partial():
    s = match_score("Sony WH-1000XM5 Wireless Noise Cancelling", "Sony WH-1000XM5")
    assert 0.3 < s < 1.0


def test_match_score_no_overlap():
    assert match_score("Apple iPhone 15", "Breville Rice Cooker") == 0.0


def test_match_orders_picks_best():
    orders = [{"title": "Sony WH-1000XM5 Wireless Headphones", "id": 1}]
    items = [
        {"id": "hb-a", "name": "Sony WH-1000XM5"},
        {"id": "hb-b", "name": "Breville Rice Cooker"},
    ]
    results = match_orders_to_items(orders, items)
    _, best, score = results[0]
    assert best["id"] == "hb-a"
    assert score > 0.25


def test_match_orders_no_match_below_threshold():
    orders = [{"title": "Totally Different Product XYZ", "id": 1}]
    items = [{"id": "hb-a", "name": "Sony Headphones"}]
    results = match_orders_to_items(orders, items, threshold=0.25)
    _, best, score = results[0]
    assert best is None
    assert score == 0.0


# ---------------------------------------------------------------------------
# Import endpoint + apply endpoint (integration via TestClient)
# ---------------------------------------------------------------------------

@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("HOMEBOX_URL", "http://homebox.test")
    monkeypatch.setenv("HOMEBOX_TOKEN", "tok")
    with patch("app.scheduler.scheduler_loop", new_callable=AsyncMock):
        import app.main as main_mod
        importlib.reload(main_mod)
        with TestClient(main_mod.app) as c:
            yield c


def test_amazon_page_empty(client):
    resp = client.get("/amazon")
    assert resp.status_code == 200
    assert "Amazon Import" in resp.text
    assert "No orders imported yet" in resp.text


def test_amazon_import_csv(client):
    with patch("app.main.HomeboxClient") as mock_hb:
        mock_hb.return_value.list_items.return_value = [
            {"id": "hb-1", "name": "Sony WH-1000XM5"},
            {"id": "hb-2", "name": "Breville Rice Cooker"},
        ]
        resp = client.post(
            "/api/amazon/import",
            files={"file": ("orders.csv", _RMD_CSV.encode(), "text/csv")},
            headers={"accept": "application/json"},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["inserted"] == 2
    assert body["skipped"] == 0


def test_amazon_import_deduplicates(client):
    with patch("app.main.HomeboxClient") as mock_hb:
        mock_hb.return_value.list_items.return_value = []
        client.post(
            "/api/amazon/import",
            files={"file": ("orders.csv", _RMD_CSV.encode(), "text/csv")},
            headers={"accept": "application/json"},
        )
        # Re-upload same CSV
        resp = client.post(
            "/api/amazon/import",
            files={"file": ("orders.csv", _RMD_CSV.encode(), "text/csv")},
            headers={"accept": "application/json"},
        )
    assert resp.json()["inserted"] == 0
    assert resp.json()["skipped"] == 2


def test_amazon_apply_writes_to_homebox(client):
    from app import db

    # Insert an order and manually match it.
    db.upsert_amazon_orders([{
        "order_id": "111-AAA", "order_date": "2024-03-15",
        "asin": "B09XS7JWHH", "title": "Sony WH-1000XM5",
        "quantity": 1, "unit_price": 399.0, "currency": "AUD", "seller": "Amazon AU",
    }])
    row = db.list_amazon_orders()[0]
    db.set_amazon_match(row["id"], "hb-sony", 0.85)

    fake_item = {
        "id": "hb-sony", "name": "Sony WH-1000XM5", "description": "",
        "assetId": "", "quantity": 1, "insured": False, "archived": False,
        "notes": "", "manufacturer": "Sony", "modelNumber": "", "serialNumber": "",
        "purchaseFrom": "", "purchasePrice": 0, "location": None,
        "tags": [], "parent": None,
    }
    with patch("app.main.HomeboxClient") as mock_hb:
        mock_hb.return_value.get_item.return_value = fake_item
        mock_hb.return_value.put_item = MagicMock()
        resp = client.post(
            f"/api/amazon/{row['id']}/apply",
            headers={"accept": "application/json"},
        )

    assert resp.status_code == 200
    assert resp.json()["result"] == "applied"

    # Confirm the PUT was called with the right price and purchaseFrom.
    put_payload = mock_hb.return_value.put_item.call_args[0][1]
    assert put_payload["purchasePrice"] == 399.0
    assert put_payload["purchaseFrom"] == "Amazon AU"

    # Status updated in DB.
    updated = db.get_amazon_order(row["id"])
    assert updated["status"] == "applied"


def test_amazon_skip(client):
    from app import db
    db.upsert_amazon_orders([{
        "order_id": "222-BBB", "order_date": "2024-01-01",
        "asin": "B001", "title": "Some Widget", "quantity": 1,
        "unit_price": 50.0, "currency": "AUD", "seller": "Amazon",
    }])
    row = db.list_amazon_orders()[0]
    resp = client.post(
        f"/api/amazon/{row['id']}/skip",
        headers={"accept": "application/json"},
    )
    assert resp.status_code == 200
    assert db.get_amazon_order(row["id"])["status"] == "skipped"


def test_amazon_import_bad_csv(client):
    resp = client.post(
        "/api/amazon/import",
        files={"file": ("junk.csv", b"col1,col2\nval1,val2\n", "text/csv")},
        headers={"accept": "application/json"},
    )
    assert resp.status_code == 400
