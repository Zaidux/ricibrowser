"""Persistent cookie + localStorage jar — survives browser restarts.

Saves to JSON on disk so authenticated sessions (and cf_clearance cookies)
persist across browser sessions. Critical for scanning behind login and for
re-using solved Cloudflare challenges.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

logger = logging.getLogger(__name__)


class CookieJar:
    """Persistent cookie + localStorage jar backed by a JSON file.

    Format::

        {
            "cookies": [
                {"name": "session", "value": "abc", "domain": ".example.com", ...},
                ...
            ],
            "storage": {
                "https://example.com": {"key": "value", ...},
                ...
            }
        }
    """

    def __init__(self, path: str | None = None):
        self.path = path or os.path.expanduser("~/.config/ricibrowser/cookies.json")
        self._data: dict[str, Any] = {"cookies": [], "storage": {}}

    def load(self) -> None:
        """Load the jar from disk (no-op if file doesn't exist)."""
        if not self.path or not os.path.exists(self.path):
            return
        try:
            self._data = json.loads(open(self.path, encoding="utf-8").read())
            if "cookies" not in self._data:
                self._data["cookies"] = []
            if "storage" not in self._data:
                self._data["storage"] = {}
        except Exception as exc:
            logger.warning("CookieJar load failed: %s", exc)
            self._data = {"cookies": [], "storage": {}}

    def save(self) -> None:
        """Save the jar to disk."""
        if not self.path:
            return
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self._data, f, indent=2, default=str)
        os.replace(tmp, self.path)

    @property
    def cookies(self) -> list[dict]:
        return self._data.get("cookies", [])

    def set_cookies(self, cookies: list[dict]) -> None:
        """Replace all cookies."""
        self._data["cookies"] = cookies

    def update_cookies(self, cookies: list[dict]) -> None:
        """Merge new cookies (by name + domain)."""
        existing = {(c.get("name"), c.get("domain")): c for c in self.cookies}
        for c in cookies:
            key = (c.get("name"), c.get("domain"))
            existing[key] = c
        self._data["cookies"] = list(existing.values())

    def get_cookies_for_domain(self, domain: str) -> list[dict]:
        """Get cookies matching a domain (handles leading dot)."""
        result = []
        for c in self.cookies:
            cdomain = c.get("domain", "")
            if cdomain == domain:
                result.append(c)
            elif cdomain.startswith(".") and domain.endswith(cdomain[1:]):
                result.append(c)
        return result

    def get_storage(self, origin: str) -> dict:
        """Get localStorage for an origin."""
        return self._data.get("storage", {}).get(origin, {})

    def set_storage(self, origin: str, storage: dict) -> None:
        """Set localStorage for an origin."""
        self._data.setdefault("storage", {})[origin] = storage

    def clear(self) -> None:
        """Clear all cookies and storage."""
        self._data = {"cookies": [], "storage": {}}
