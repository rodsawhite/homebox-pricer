"""Homebox API client.

Supports both a static bearer token (HOMEBOX_TOKEN) and credential-based
auto-refresh (HOMEBOX_USER + HOMEBOX_PASSWORD). If both are supplied,
credentials take precedence and the token is used only as a warm start.

All write operations use PUT with the full item object — PATCH has been
observed to silently drop custom fields on some Homebox versions.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from .config import get_settings

logger = logging.getLogger(__name__)


class HomeboxError(Exception):
    pass


class HomeboxClient:
    def __init__(self) -> None:
        s = get_settings()
        self._base = s.homebox_url.rstrip("/")
        self._token: str = s.homebox_token
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
            data={"username": self._user, "password": self._password},
            timeout=10,
        )
        if resp.status_code != 200:
            raise HomeboxError(f"Login failed: {resp.status_code} {resp.text[:200]}")
        self._token = resp.json()["token"]
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
        for attempt in range(2):
            resp = httpx.get(url, headers=self._auth_header(), params=params, timeout=15)
            if resp.status_code == 401 and attempt == 0 and self._refresh_if_needed(401):
                continue
            if resp.status_code != 200:
                raise HomeboxError(f"list_items page={page}: {resp.status_code} {resp.text[:200]}")
            data = resp.json()
            # Homebox wraps paginated results in {"items": [...], "total": N}
            # Fall back to a bare list for forward compat.
            if isinstance(data, list):
                return data
            return data.get("items", [])
        return []  # unreachable, satisfies type checker

    def get_item(self, item_id: str) -> dict[str, Any]:
        url = f"{self._base}/api/v1/items/{item_id}"
        for attempt in range(2):
            resp = httpx.get(url, headers=self._auth_header(), timeout=10)
            if resp.status_code == 401 and attempt == 0 and self._refresh_if_needed(401):
                continue
            if resp.status_code != 200:
                raise HomeboxError(f"get_item {item_id}: {resp.status_code} {resp.text[:200]}")
            return resp.json()
        return {}  # unreachable

    def put_item(self, item_id: str, item: dict[str, Any]) -> None:
        """Write the full item object back (read-modify-write pattern)."""
        url = f"{self._base}/api/v1/items/{item_id}"
        for attempt in range(2):
            resp = httpx.put(url, headers=self._auth_header(), json=item, timeout=10)
            if resp.status_code == 401 and attempt == 0 and self._refresh_if_needed(401):
                continue
            if resp.status_code not in (200, 204):
                raise HomeboxError(f"put_item {item_id}: {resp.status_code} {resp.text[:200]}")
            return

    def apply_price(self, item_id: str, price: float) -> None:
        """Fetch the full item, set purchasePrice, write it back."""
        item = self.get_item(item_id)
        item["purchasePrice"] = price
        self.put_item(item_id, item)
        logger.info("Applied price %.2f to item %s", price, item_id)
