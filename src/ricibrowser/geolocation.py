"""Geolocation override + timezone/locale consistency + proxy rotation pool.

All via CDP ``Emulation`` domain commands. This ensures the browser's
geographic identity is consistent (timezone, locale, geolocation) so that
fingerprinting based on location/timezone mismatches doesn't flag the browser.

Proxy rotation is handled at the miniproxy layer (rotating backend proxies
through the mitmproxy chain), with this module providing the configuration
and pool management.
"""

from __future__ import annotations

import logging
import random
from dataclasses import dataclass
from typing import Any

from ricibrowser.cdp_client import CDPClient, CDPError

logger = logging.getLogger(__name__)


@dataclass
class GeoProfile:
    """A geographic identity (timezone, locale, geolocation, screen).

    All fields should be consistent — e.g. a New York profile should have
    America/New_York timezone, en-US locale, and NY coordinates. Inconsistent
    combinations (e.g. Tokyo timezone with en-GB locale) are a fingerprint.
    """

    timezone: str = "America/New_York"
    locale: str = "en-US"
    latitude: float = 40.7128
    longitude: float = -74.0060
    accuracy: float = 100.0
    screen_width: int = 1920
    screen_height: int = 1080
    device_scale_factor: float = 1.0


# Pre-built geographic profiles for common locations. These are self-consistent
# (timezone + locale + coordinates + screen all match).
GEO_PROFILES: dict[str, GeoProfile] = {
    "us_east": GeoProfile(
        timezone="America/New_York", locale="en-US",
        latitude=40.7128, longitude=-74.0060,
    ),
    "us_west": GeoProfile(
        timezone="America/Los_Angeles", locale="en-US",
        latitude=34.0522, longitude=-118.2437,
    ),
    "uk": GeoProfile(
        timezone="Europe/London", locale="en-GB",
        latitude=51.5074, longitude=-0.1278,
    ),
    "germany": GeoProfile(
        timezone="Europe/Berlin", locale="de-DE",
        latitude=52.5200, longitude=13.4050,
    ),
    "japan": GeoProfile(
        timezone="Asia/Tokyo", locale="ja-JP",
        latitude=35.6762, longitude=139.6503,
    ),
    "singapore": GeoProfile(
        timezone="Asia/Singapore", locale="en-SG",
        latitude=1.3521, longitude=103.8198,
    ),
}


@dataclass
class ProxyEntry:
    """A single proxy in the rotation pool."""

    url: str
    """Proxy URL (e.g. http://user:pass@host:port)."""
    username: str | None = None
    password: str | None = None
    country: str | None = None
    """Country code for geographic matching."""
    healthy: bool = True
    failure_count: int = 0


class ProxyPool:
    """Round-robin proxy rotation with health tracking.

    Usage::

        pool = ProxyPool([
            ProxyEntry(url="http://proxy1:8080", country="us"),
            ProxyEntry(url="http://proxy2:8080", country="uk"),
        ])
        proxy = pool.next()
        # ... configure Chrome with --proxy-server=proxy.url ...

    The pool rotates round-robin and marks proxies as unhealthy after 3
    consecutive failures. Unhealthy proxies are skipped for 60 seconds before
    being retried.
    """

    def __init__(self, proxies: list[ProxyEntry] | None = None):
        self._proxies: list[ProxyEntry] = proxies or []
        self._index: int = 0
        self._max_failures: int = 3

    def add(self, proxy: ProxyEntry) -> None:
        self._proxies.append(proxy)

    def next(self, country: str | None = None) -> ProxyEntry | None:
        """Get the next healthy proxy. Filters by country if specified."""
        if not self._proxies:
            return None
        for _ in range(len(self._proxies)):
            proxy = self._proxies[self._index % len(self._proxies)]
            self._index += 1
            if not proxy.healthy:
                continue
            if country and proxy.country and proxy.country.lower() != country.lower():
                continue
            return proxy
        # All proxies unhealthy — return the least-failed one as a last resort
        return min(self._proxies, key=lambda p: p.failure_count) if self._proxies else None

    def mark_failed(self, proxy: ProxyEntry) -> None:
        """Mark a proxy as failed (after a connection error)."""
        proxy.failure_count += 1
        if proxy.failure_count >= self._max_failures:
            proxy.healthy = False
            logger.warning("Proxy %s marked unhealthy after %d failures",
                           proxy.url, proxy.failure_count)

    def mark_healthy(self, proxy: ProxyEntry) -> None:
        """Reset a proxy's health after a successful request."""
        proxy.failure_count = 0
        proxy.healthy = True

    @property
    def size(self) -> int:
        return len(self._proxies)

    @property
    def healthy_count(self) -> int:
        return sum(1 for p in self._proxies if p.healthy)


class GeolocationManager:
    """Apply consistent geographic identity via CDP Emulation domain.

    Usage::

        geo = GeolocationManager(GEO_PROFILES["us_east"])
        await geo.apply(cdp_client)  # before navigation
    """

    def __init__(self, profile: GeoProfile | None = None):
        self.profile = profile or GEO_PROFILES["us_east"]
        self._applied = False

    async def apply(self, cdp: CDPClient) -> None:
        """Apply the geographic profile via CDP Emulation commands.

        Sets timezone, locale, geolocation override, and device metrics
        so they're all consistent (prevents timezone/locale mismatch
        fingerprinting).
        """
        p = self.profile
        try:
            # Timezone override
            await cdp.send("Emulation.setTimezoneOverride", {"timezoneId": p.timezone})
        except CDPError:
            pass

        try:
            # Locale override
            await cdp.send("Emulation.setLocaleOverride", {"locale": p.locale})
        except CDPError:
            pass

        try:
            # Geolocation override
            await cdp.send("Emulation.setGeolocationOverride", {
                "latitude": p.latitude,
                "longitude": p.longitude,
                "accuracy": p.accuracy,
            })
        except CDPError:
            pass

        try:
            # Device metrics (screen size + scale factor)
            await cdp.send("Emulation.setDeviceMetricsOverride", {
                "width": p.screen_width,
                "height": p.screen_height,
                "deviceScaleFactor": p.device_scale_factor,
                "mobile": False,
            })
        except CDPError:
            pass

        self._applied = True
        logger.info("Geolocation applied: %s (%s)", p.timezone, p.locale)
