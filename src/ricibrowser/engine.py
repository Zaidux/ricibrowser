"""Engine facade — auto-selects Lightpanda (fast) or CDP-Chrome (thorough).

The engine is the public entry point for ricibrowser. It manages the lifecycle
of browser processes and CDP connections, and auto-falls-back from Lightpanda
to Chrome when Lightpanda is unavailable or a feature isn't supported (e.g.
screenshots).
"""

from __future__ import annotations

import asyncio
import logging
import subprocess
from typing import Any

from ricibrowser.chrome_launcher import get_debug_url, launch_chrome, stop_chrome
from ricibrowser.cdp_client import CDPClient, CDPError
from ricibrowser.config import EngineConfig, EngineType
from ricibrowser.cookie_jar import CookieJar
from ricibrowser.fingerprint import FingerprintShield
from ricibrowser.geolocation import GEO_PROFILES, GeolocationManager
from ricibrowser.captcha import CaptchaHandler, CloudflareAutoSolver
from ricibrowser.headers import HeaderManager
from ricibrowser.input import HumanMouse
from ricibrowser.lightpanda import LightpandaEngine
from ricibrowser.network import NetworkCapture
from ricibrowser.session import Page, Session
from ricibrowser.utils import truncate, validate_url

logger = logging.getLogger(__name__)


class Engine:
    """Two-engine browser facade.

    Auto-selects the appropriate engine based on the operation:
      - fast_browse() → Lightpanda (fast path, crawl/recon)
      - create_session() → CDP-Chrome (thorough path, DAST/JS-heavy)

    Falls back from Lightpanda → Chrome when Lightpanda is unavailable or
    doesn't support the requested action (e.g. screenshots).
    """

    def __init__(self, config: EngineConfig | None = None):
        self.config = config or EngineConfig()
        self._lightpanda: LightpandaEngine | None = None
        self._chrome_proc: subprocess.Popen | None = None
        self._cookie_jar = CookieJar(self.config.cookie_jar_path)
        self._cookie_jar.load()
        self._network = NetworkCapture(enabled=self.config.debug_network)
        # ── Extension modules ──────────────────────────────────────
        self._fingerprint = FingerprintShield(enabled=self.config.fingerprint_shield)
        geo_profile = None
        if self.config.geolocation_profile:
            geo_profile = GEO_PROFILES.get(self.config.geolocation_profile)
        self._geolocation = GeolocationManager(profile=geo_profile)
        self._captcha_handler = CaptchaHandler(
            auto_solver=CloudflareAutoSolver() if self.config.cloudflare_auto_resolve else None,
        )
        self._header_manager = HeaderManager()
        self._human_mouse: HumanMouse | None = None  # Per-session

    @property
    def cookie_jar(self) -> CookieJar:
        return self._cookie_jar

    @property
    def network(self) -> NetworkCapture:
        return self._network

    @property
    def captcha_handler(self) -> CaptchaHandler:
        """Get the CAPTCHA handler for detecting/solving challenges."""
        return self._captcha_handler

    @property
    def human_mouse(self) -> HumanMouse | None:
        """Get the human mouse simulator (None if not enabled)."""
        return self._human_mouse

    async def fast_browse(self, url: str, max_chars: int = 6000, **kwargs) -> Page:
        """Browse a URL using the fast engine (Lightpanda by default).

        Falls back to CDP-Chrome if Lightpanda is unavailable or the page
        needs a screenshot.
        """
        url = validate_url(url)

        # Try Lightpanda first
        engine_type = self.config.fast_engine_resolved
        if engine_type == EngineType.LIGHTPANDA:
            try:
                page = await self._browse_lightpanda(url, max_chars, **kwargs)
                if page is not None:
                    # Save cookies from the browse
                    if page.cookies:
                        self._cookie_jar.update_cookies(page.cookies)
                        self._cookie_jar.save()
                    return page
            except Exception as exc:
                logger.warning("Lightpanda browse failed, falling back to Chrome: %s", exc)

        # Fallback to CDP-Chrome
        return await self._browse_chrome(url, max_chars, **kwargs)

    def fast_browse_sync(self, url: str, **kwargs) -> Page:
        """Synchronous wrapper for fast_browse (for CLI use)."""
        return asyncio.run(self.fast_browse(url, **kwargs))

    async def create_session(self, engine: EngineType | None = None) -> Session:
        """Create a new browser session using the thorough engine (CDP-Chrome).

        The session provides navigate/evaluate/screenshot/click/fill methods.
        Cookies are loaded from the jar on creation and saved on close.
        """
        engine_type = engine or self.config.thorough_engine
        if engine_type == EngineType.LIGHTPANDA:
            return await self._create_lightpanda_session()
        return await self._create_chrome_session()

    async def close(self) -> None:
        """Stop all browser processes. Each stop is independent — failure
        in one path doesn't prevent cleanup of the other."""
        if self._lightpanda:
            try:
                await self._lightpanda.stop()
            except Exception as exc:
                logger.warning("Lightpanda stop failed: %s", exc)
            finally:
                self._lightpanda = None
        if self._chrome_proc:
            try:
                stop_chrome(self._chrome_proc)
            except Exception as exc:
                logger.warning("Chrome stop failed: %s", exc)
            finally:
                self._chrome_proc = None

    # ── Lightpanda path ────────────────────────────────────────────

    async def _get_lightpanda(self) -> LightpandaEngine:
        if self._lightpanda is None or not self._lightpanda.is_started:
            self._lightpanda = LightpandaEngine(
                ws_url=self.config.lightpanda_url,
                timeout=self.config.timeout,
            )
            await self._lightpanda.start()
        return self._lightpanda

    async def _browse_lightpanda(self, url: str, max_chars: int, **kwargs) -> Page | None:
        """Browse via Lightpanda. Returns None if Lightpanda isn't available."""
        if not await LightpandaEngine.is_available(self.config.lightpanda_url):
            logger.info("Lightpanda not available, will use Chrome")
            return None
        lp = await self._get_lightpanda()
        return await lp.browse(url, max_chars=max_chars, cookies=self._cookie_jar.cookies or None, **kwargs)

    async def _create_lightpanda_session(self) -> Session:
        """Create a session via Lightpanda (limited — no screenshots)."""
        lp = await self._get_lightpanda()
        client = lp._client
        if client is None:
            raise RuntimeError("Lightpanda client not initialized")
        session = Session(client, engine_name="lightpanda")
        session._url_stability_timeout = self.config.url_stability_timeout
        session._nav_timeout = self.config.nav_timeout
        # Preload cookies from jar (same as Chrome path)
        if self._cookie_jar.cookies:
            try:
                await session.set_cookies(self._cookie_jar.cookies)
            except Exception:
                pass
        return session

    # ── CDP-Chrome path ────────────────────────────────────────────

    async def _ensure_chrome(self) -> str:
        """Ensure Chrome is running and return its debug URL.

        Launches Chrome if needed, then polls until the CDP HTTP endpoint is
        reachable (Chrome takes ~1-2s to start listening). Also detects
        zombie/hung Chrome processes — reuses existing process ONLY if it
        responds to a health check.

        This fixes the "All connection attempts failed" error when Chrome
        is slow to start, and the silent hang when Chrome is alive but
        unresponsive.
        """
        debug_url = get_debug_url(self.config.chrome_debug_port)

        # If Chrome process exists and hasn't exited, verify it's responsive
        if self._chrome_proc and self._chrome_proc.poll() is None:
            import httpx
            try:
                async with httpx.AsyncClient(timeout=2.0) as http:
                    resp = await http.get(f"{debug_url}/json/version")
                    if resp.status_code == 200:
                        return debug_url
            except Exception:
                logger.warning("Chrome process alive but not responding — restarting")
                stop_chrome(self._chrome_proc)
            self._chrome_proc = None

        # If old Chrome exited (zombie), clean up
        if self._chrome_proc is not None:
            try:
                self._chrome_proc.wait(timeout=0.1)
            except Exception:
                pass
            self._chrome_proc = None

        self._chrome_proc = launch_chrome(
            port=self.config.chrome_debug_port,
            proxy=self.config.proxy_url,
            stealth=self.config.stealth,
            user_data_dir=self.config.user_data_dir,
            extra_args=self.config.extra_chrome_args,
            viewport_width=self.config.viewport_width,
            viewport_height=self.config.viewport_height,
            user_agent=self.config.user_agent,
            ignore_cert_errors=self.config.ignore_cert_errors,
        )

        import httpx
        async with httpx.AsyncClient(timeout=2.0) as http:
            # Chrome cold-starts can take several seconds (first launch, cold
            # disk cache, container CPU contention). Polling only ~5s and then
            # "proceeding anyway" caused the flaky "connects but page loads are
            # empty" symptom: connect_to_target would race a not-yet-ready
            # browser. Wait up to config.chrome_startup_timeout (default 20s).
            startup_timeout = getattr(self.config, "chrome_startup_timeout", 20.0)
            poll_interval = 0.25
            polls = max(1, int(startup_timeout / poll_interval))
            for attempt in range(polls):
                # Bail early if Chrome died on launch (bad flag, missing lib).
                if self._chrome_proc is not None and self._chrome_proc.poll() is not None:
                    raise RuntimeError(
                        f"Chrome exited during startup (code "
                        f"{self._chrome_proc.returncode}). Check Chrome flags / "
                        f"install. Try headless=new with --no-sandbox."
                    )
                try:
                    resp = await http.get(f"{debug_url}/json/version")
                    if resp.status_code == 200:
                        logger.info("Chrome ready after %.1fs", (attempt + 1) * poll_interval)
                        return debug_url
                except Exception:
                    pass
                await asyncio.sleep(poll_interval)
        logger.warning(
            "Chrome did not become ready after %.0fs — proceeding anyway",
            startup_timeout,
        )
        return debug_url

    async def _browse_chrome(self, url: str, max_chars: int, **kwargs) -> Page:
        """Browse via CDP-Chrome (thorough path)."""
        debug_url = await self._ensure_chrome()
        client = await CDPClient.connect_to_target(debug_url)
        try:
            session = Session(client, engine_name="cdp_chrome")
            session._url_stability_timeout = self.config.url_stability_timeout
            session._nav_timeout = self.config.nav_timeout

            # Load cookies from jar
            if self._cookie_jar.cookies:
                await session.set_cookies(self._cookie_jar.cookies)

            # Start network capture if enabled
            if self._network.enabled:
                await self._network.start(client)

            # When proxied, avoid "networkidle" — a slow proxy can keep
            # in-flight resource counters from ever settling. Fall back to
            # "load" (Page.loadEventFired) which still fires reliably.
            _want_networkidle = self._network.enabled and not self.config.proxy_url
            page = await session.navigate(
                url,
                wait_until="networkidle" if _want_networkidle else "load",
                max_chars=max_chars,
            )

            # Save cookies back to jar
            cookies = await session.get_cookies()
            if cookies:
                self._cookie_jar.update_cookies(cookies)
                self._cookie_jar.save()

            return page
        finally:
            if self._network.enabled:
                await self._network.stop(client)
            await client.close()

    async def _create_chrome_session(self) -> Session:
        """Create a persistent session via CDP-Chrome with all extensions applied."""
        debug_url = await self._ensure_chrome()
        client = await CDPClient.connect_to_target(debug_url)
        session = Session(client, engine_name="cdp_chrome")
        session._url_stability_timeout = self.config.url_stability_timeout
        session._nav_timeout = self.config.nav_timeout

        # Load cookies from jar
        if self._cookie_jar.cookies:
            await session.set_cookies(self._cookie_jar.cookies)

        # Start network capture if enabled
        if self._network.enabled:
            await self._network.start(client)

        # ── Apply extension modules ────────────────────────────────
        # Fingerprint shield (canvas/WebGL/audio randomization)
        if self._fingerprint.enabled:
            await self._fingerprint.apply(client)

        # Geolocation consistency (timezone/locale/geo override) — only
        # when the operator explicitly requested a geographic profile.
        if self.config.geolocation_profile:
            await self._geolocation.apply(client)

        # Header consistency (Sec-CH-UA matches UA)
        if self.config.header_consistency:
            await self._header_manager.apply(client)

        # Human mouse input (bezier-curve movement)
        if self.config.human_input:
            self._human_mouse = HumanMouse(
                client,
                viewport_width=self.config.viewport_width,
                viewport_height=self.config.viewport_height,
            )

        return session
