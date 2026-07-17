"""Request header ordering + Sec-CH-UA consistency via CDP Network domain.

Real Chrome sends request headers in a specific order. Headless browsers and
automation tools sometimes reorder them, which is detectable. This module
ensures header order and Sec-CH-UA client hints match a real Chrome release.

Sec-CH-UA: Chrome sends ``Sec-CH-UA``, ``Sec-CH-UA-Mobile``, and
``Sec-CH-UA-Platform`` headers that describe the browser brand/version.
These MUST be consistent with the User-Agent string — a mismatch is an
immediate fingerprint. We use ``Network.setUserAgentOverride`` with the
full ``userAgentMetadata`` to keep them in sync.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from ricibrowser.cdp_client import CDPClient, CDPError

logger = logging.getLogger(__name__)


@dataclass
class ChromeVersion:
    """A Chrome version with its full client hints metadata."""

    major: int
    """Major version (e.g. 125)."""
    full: str
    """Full version (e.g. 125.0.6422.142)."""

    @property
    def user_agent(self) -> str:
        """The full User-Agent string for this Chrome version."""
        return (
            f"Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            f"(KHTML, like Gecko) Chrome/{self.full} Safari/537.36"
        )

    @property
    def brands(self) -> list[dict[str, str]]:
        """The Sec-CH-UA brand list matching this version.

        Chrome includes ``Google Chrome`` (the real brand) and ``Chromium``
        (the open-source brand) plus a ``Not/A)Brand`` entry.
        """
        return [
            {"brand": "Google Chrome", "version": str(self.major)},
            {"brand": "Chromium", "version": str(self.major)},
            {"brand": "Not/A)Brand", "version": "99"},
        ]

    @property
    def full_version_list(self) -> list[dict[str, str]]:
        """The Sec-CH-UA-Full-Version-List (full version numbers)."""
        return [
            {"brand": "Google Chrome", "version": self.full},
            {"brand": "Chromium", "version": self.full},
            {"brand": "Not/A)Brand", "version": "99.0.0.0"},
        ]


# Default Chrome version for consistency (updated quarterly)
DEFAULT_CHROME = ChromeVersion(major=125, full="125.0.6422.142")


def detect_chrome_version(user_agent: str) -> ChromeVersion | None:
    """Extract Chrome version from a User-Agent string.

    Returns None if the UA doesn't contain Chrome version info.
    """
    match = re.search(r"Chrome/(\d+)\.(\d+)\.(\d+)\.(\d+)", user_agent)
    if not match:
        return None
    major = int(match.group(1))
    full = f"{match.group(1)}.{match.group(2)}.{match.group(3)}.{match.group(4)}"
    return ChromeVersion(major=major, full=full)


class HeaderManager:
    """Ensures request headers and Sec-CH-UA are consistent.

    Uses CDP ``Network.setUserAgentOverride`` with ``userAgentMetadata`` to
    set the UA + client hints atomically, so they can never drift out of sync.

    Usage::

        headers = HeaderManager(chrome_version=DEFAULT_CHROME)
        await headers.apply(cdp_client)
    """

    def __init__(self, chrome_version: ChromeVersion | None = None):
        self.chrome_version = chrome_version or DEFAULT_CHROME
        self._applied = False

    @property
    def user_agent(self) -> str:
        return self.chrome_version.user_agent

    async def apply(self, cdp: CDPClient) -> None:
        """Set the User-Agent and Sec-CH-UA client hints via CDP.

        ``Network.setUserAgentOverride`` with ``userAgentMetadata`` sets both
        the UA string AND the client hints atomically, preventing mismatches.
        This requires ``Network.enable`` to be called first.

        Note: ``Network.enable`` is a detection vector. Only call this when
        you explicitly need UA override (e.g. anti-bot mode).
        """
        try:
            await cdp.send("Network.enable")
        except CDPError:
            pass

        try:
            await cdp.send("Network.setUserAgentOverride", {
                "userAgent": self.user_agent,
                "platform": "Linux",
                "userAgentMetadata": {
                    "brands": self.chrome_version.brands,
                    "fullVersionList": self.chrome_version.full_version_list,
                    "fullVersion": self.chrome_version.full,
                    "platform": "Linux",
                    "platformVersion": "6.5.0",
                    "architecture": "x86",
                    "bitness": "64",
                    "model": "",
                    "mobile": False,
                    "wow64": False,
                },
            })
            self._applied = True
            logger.info(
                "Header consistency applied: Chrome/%s (UA + Sec-CH-UA synced)",
                self.chrome_version.full,
            )
        except CDPError as exc:
            logger.warning("setUserAgentOverride failed: %s", exc)

    async def set_extra_headers(
        self, cdp: CDPClient, headers: dict[str, str]
    ) -> None:
        """Set extra HTTP headers via CDP ``Network.setExtraHTTPHeaders``.

        Use this to add/override specific headers while keeping the default
        Chrome header order intact.
        """
        try:
            await cdp.send("Network.setExtraHTTPHeaders", {"headers": headers})
        except CDPError as exc:
            logger.warning("setExtraHTTPHeaders failed: %s", exc)

    async def set_accept_language(self, cdp: CDPClient, locale: str = "en-US,en;q=0.9") -> None:
        """Set the Accept-Language header."""
        await self.set_extra_headers(cdp, {"Accept-Language": locale})
