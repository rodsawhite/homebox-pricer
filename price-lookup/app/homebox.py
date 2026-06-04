"""Homebox API client.

Supports both a static bearer token (HOMEBOX_TOKEN) and credential-based
auto-refresh (HOMEBOX_USER + HOMEBOX_PASSWORD). If both are supplied,
credentials take precedence and the token is used only as a warm start.

All write operations use PUT with the full item object — PATCH has been
observed to silently drop custom fields on some Homebox versions.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Callable

import httpx

from .config import get_settings

logger = logging.getLogger(__name__)

# Bounded retry for *transient* failures only (connection errors, timeouts,
# 5xx). 4xx — including the 401 token-expiry case — is never retried here; that
# path is handled by the explicit _refresh_if_needed login flow.
_MAX_ATTEMPTS = 3
_BACKOFF_BASE = 1.0  # seconds: 1s, 2s, 4s — overridable/mockable in tests


class HomeboxError(Exception):
    pass


class HomeboxAuthError(HomeboxError):
    """Raised on a 401 we can't recover from (static token expired, no creds).

    Subclasses HomeboxError so existing ``except HomeboxError`` handlers still
    catch it, while callers that care can distinguish "token expired" from a
    generic failure and pause rather than thrash.
    """


def _retry_request(
    send: Callable[[], httpx.Response], *, what: str
) -> httpx.Response:
    """Call ``send`` with exponential backoff on transient failures.

    "Transient" means connection errors, timeouts, and 5xx responses. A 5xx is
    retried then returned for the caller to raise on if it persists; any other
    status (2xx, or 4xx incl. the 401 refresh handshake) is returned
    immediately for the caller to inspect. Raises HomeboxError only after
    exhausting attempts on a transport error.
    """
    last_exc: Exception | None = None
    for attempt in range(_MAX_ATTEMPTS):
        is_last = attempt == _MAX_ATTEMPTS - 1
        try:
            resp = send()
        except (httpx.TransportError, httpx.TimeoutException) as exc:
            last_exc = exc
            if is_last:
                break
            _backoff(attempt, what, str(exc))
            continue
        # 5xx is transient server-side; retry unless we're out of attempts.
        if resp.status_code >= 500 and not is_last:
            _backoff(attempt, what, f"HTTP {resp.status_code}")
            continue
        return resp
    raise HomeboxError(f"{what}: transport error after {_MAX_ATTEMPTS} attempts: {last_exc}")


def _backoff(attempt: int, what: str, cause: str) -> None:
    delay = _BACKOFF_BASE * (2 ** attempt)
    logger.warning(
        "Transient Homebox error on %s (attempt %d/%d): %s — retrying in %.0fs",
        what, attempt + 1, _MAX_ATTEMPTS, cause, delay,
    )
    time.sleep(delay)


class HomeboxClient:
    def __init__(self) -> None:
        s = get_settings()
        self._base = s.homebox_url.rstrip("/")
        # Strip any "Bearer " prefix — Homebox v0.22.0+ includes it in the
        # token value; users may also paste it verbatim from the Homebox UI.
        self._token: str = s.homebox_token.removeprefix("Bearer ").strip()
        self._user: str = s.homebox_user
        self._password: str = s.homebox_password

    # ------------------------------------------------------------------
    # Auth
    # ------------------------------------------------------------------

    def _auth_header(self) -> dict[str, str]:
        if not self._token:
            self._login()
        return {"Authorization": f"Bearer {self._token}"}

    def _login(self) -> None:
        url = f"{self._base}/api/v1/users/login"
        resp = httpx.post(
            url,
            # stayLoggedIn is required: without it Homebox issues a short-lived
            # session token that 401s on the very next API call. With it we get
            # a proper bearer token good for the configured lifetime.
            data={
                "username": self._user,
                "password": self._password,
                "stayLoggedIn": True,
            },
            timeout=10,
        )
        if resp.status_code != 200:
            raise HomeboxError(f"Login failed: {resp.status_code} {resp.text[:200]}")
        token = resp.json()["token"]
        # Homebox v0.22.0+ returns tokens with the "Bearer " prefix built-in.
        # Strip it so _auth_header() can apply it consistently.
        self._token = token.removeprefix("Bearer ").strip()
        logger.info("Homebox login successful")

    def _refresh_if_needed(self, status_code: int) -> bool:
        """Return True if we refreshed (caller should retry), False otherwise."""
        if status_code == 401 and self._user and self._password:
            logger.warning("Homebox token expired — refreshing")
            self._login()
            return True
        return False

    # ------------------------------------------------------------------
    # Items
    # ------------------------------------------------------------------

    def list_items(self) -> list[dict[str, Any]]:
        """Fetch all items across all pages."""
        items: list[dict[str, Any]] = []
        page = 1
        while True:
            batch = self._get_items_page(page)
            if not batch:
                break
            items.extend(batch)
            page += 1
        return items

    def _get_items_page(self, page: int) -> list[dict[str, Any]]:
        url = f"{self._base}/api/v1/items"
        params = {"page": page, "pageSize": 100}
        what = f"list_items page={page}"
        for attempt in range(2):
            resp = _retry_request(
                lambda: httpx.get(url, headers=self._auth_header(), params=params, timeout=15),
                what=what,
            )
            if resp.status_code == 401:
                if attempt == 0 and self._refresh_if_needed(401):
                    continue
                raise HomeboxAuthError(f"{what}: 401 (token expired, no credentials to refresh)")
            if resp.status_code != 200:
                raise HomeboxError(f"{what}: {resp.status_code} {resp.text[:200]}")
            data = resp.json()
            # Homebox wraps paginated results in {"items": [...], "total": N}
            # Fall back to a bare list for forward compat.
            if isinstance(data, list):
                return data
            return data.get("items", [])
        return []  # unreachable, satisfies type checker

    def get_item(self, item_id: str) -> dict[str, Any]:
        url = f"{self._base}/api/v1/items/{item_id}"
        what = f"get_item {item_id}"
        for attempt in range(2):
            resp = _retry_request(
                lambda: httpx.get(url, headers=self._auth_header(), timeout=10),
                what=what,
            )
            if resp.status_code == 401:
                if attempt == 0 and self._refresh_if_needed(401):
                    continue
                raise HomeboxAuthError(f"{what}: 401 (token expired, no credentials to refresh)")
            if resp.status_code != 200:
                raise HomeboxError(f"{what}: {resp.status_code} {resp.text[:200]}")
            return resp.json()
        return {}  # unreachable

    def put_item(self, item_id: str, item: dict[str, Any]) -> None:
        """Write the full item object back (read-modify-write pattern)."""
        url = f"{self._base}/api/v1/items/{item_id}"
        what = f"put_item {item_id}"
        for attempt in range(2):
            resp = _retry_request(
                lambda: httpx.put(url, headers=self._auth_header(), json=item, timeout=10),
                what=what,
            )
            if resp.status_code == 401:
                if attempt == 0 and self._refresh_if_needed(401):
                    continue
                raise HomeboxAuthError(f"{what}: 401 (token expired, no credentials to refresh)")
            if resp.status_code not in (200, 204):
                raise HomeboxError(f"{what}: {resp.status_code} {resp.text[:200]}")
            return

    def apply_price(self, item_id: str, price: float) -> None:
        """Fetch the full item, set purchasePrice, write it back."""
        item = self.get_item(item_id)
        payload = _build_put_payload(item)
        payload["purchasePrice"] = price
        self.put_item(item_id, payload)
        logger.info("Applied price %.2f to item %s", price, item_id)


def _build_put_payload(item: dict) -> dict:
    """Convert a GET ItemOut response to a PUT ItemUpdate-compatible payload.

    Homebox's GET response (ItemOut) contains read-only fields like
    'attachments' and nested objects (location, labels, parent) that the
    PUT endpoint (ItemUpdate) does not accept — sending them back causes 500.
    This function projects only the writable scalar fields and maps nested
    objects to their flat ID equivalents that ItemUpdate expects.
    """
    # location and labels come back as objects; PUT wants their IDs only.
    loc = item.get("location") or {}
    labels = item.get("labels") or []
    parent = item.get("parent") or {}

    def _date(value: Any) -> Any:
        # Homebox returns unset dates as the zero year; send null instead so
        # the PUT validator doesn't choke. Real dates pass through untouched.
        if not value or str(value).startswith("0001-01-01"):
            return None
        return value

    return {
        "id": item.get("id", ""),
        "name": item.get("name", ""),
        "description": item.get("description", ""),
        "assetId": item.get("assetId", "0"),
        "quantity": item.get("quantity", 1),
        "insured": item.get("insured", False),
        "archived": item.get("archived", False),
        "syncChildItemsLocations": item.get("syncChildItemsLocations", False),
        # Nested → flat IDs. Empty UUIDs must be null, not "" — an empty
        # string fails Homebox's UUID parse and triggers a generic 500.
        "locationId": loc.get("id") or None,
        "labelIds": [lbl["id"] for lbl in labels if lbl.get("id")],
        "parentId": parent.get("id") or None,
        # Identifications
        "serialNumber": item.get("serialNumber", ""),
        "modelNumber": item.get("modelNumber", ""),
        "manufacturer": item.get("manufacturer", ""),
        # Warranty
        "lifetimeWarranty": item.get("lifetimeWarranty", False),
        "warrantyExpires": _date(item.get("warrantyExpires")),
        "warrantyDetails": item.get("warrantyDetails", ""),
        # Purchase
        "purchaseTime": _date(item.get("purchaseTime")),
        "purchaseFrom": item.get("purchaseFrom", ""),
        "purchasePrice": item.get("purchasePrice", 0),
        # Sold
        "soldTime": _date(item.get("soldTime")),
        "soldTo": item.get("soldTo", ""),
        "soldPrice": item.get("soldPrice", 0),
        "soldNotes": item.get("soldNotes", ""),
        # Extras
        "notes": item.get("notes", ""),
        "fields": item.get("fields", []),
    }
