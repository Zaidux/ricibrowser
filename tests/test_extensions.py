"""Tests for fingerprint shield, geolocation, captcha, input, and headers."""

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from ricibrowser.captcha import (
    CaptchaHandler,
    CaptchaType,
    CloudflareAutoSolver,
    detect_captcha,
)
from ricibrowser.fingerprint import FingerprintShield
from ricibrowser.geolocation import (
    GEO_PROFILES,
    GeoProfile,
    GeolocationManager,
    ProxyEntry,
    ProxyPool,
)
from ricibrowser.headers import ChromeVersion, DEFAULT_CHROME, detect_chrome_version
from ricibrowser.input import HumanMouse
from ricibrowser.session import Session


# ── Session.evaluate_bool ────────────────────────────────────────────

class TestEvaluateBool:
    @pytest.mark.asyncio
    async def test_eval_true(self):
        """evaluate_bool returns True when CDP returns Python True (str=True → 'True')."""
        from ricibrowser.cdp_client import CDPClient, CDPError
        client = MagicMock()
        client.send = AsyncMock(return_value={"result": {"value": True}})
        client._closed = False
        client._event_handlers = {}
        client._pending = {}
        session = Session(client)
        result = await session.evaluate_bool("1==1")
        assert result is True

    @pytest.mark.asyncio
    async def test_eval_false(self):
        from ricibrowser.cdp_client import CDPClient
        client = MagicMock()
        client.send = AsyncMock(return_value={"result": {"value": False}})
        client._closed = False
        client._event_handlers = {}
        client._pending = {}
        session = Session(client)
        result = await session.evaluate_bool("1==2")
        assert result is False

    @pytest.mark.asyncio
    async def test_eval_none(self):
        from ricibrowser.cdp_client import CDPClient, CDPError
        client = MagicMock()
        client.send = AsyncMock(side_effect=CDPError("test", -1, "fail"))
        client._closed = False
        client._event_handlers = {}
        client._pending = {}
        session = Session(client)
        result = await session.evaluate_bool("nah")
        assert result is None


# ── Fingerprint ──────────────────────────────────────────────────────

class TestFingerprintShield:
    def test_disabled_by_default(self):
        shield = FingerprintShield()
        assert shield.enabled is False

    def test_enabled(self):
        shield = FingerprintShield(enabled=True)
        assert shield.enabled is True
        assert shield._seed > 0

    def test_reset_seed(self):
        shield = FingerprintShield(enabled=True)
        old_seed = shield._seed
        shield.reset_seed()
        assert shield._seed > 0
        assert shield._applied is False

    @pytest.mark.asyncio
    async def test_apply_skipped_when_disabled(self):
        shield = FingerprintShield(enabled=False)
        cdp = MagicMock()
        cdp.send = AsyncMock()
        await shield.apply(cdp)
        cdp.send.assert_not_called()

    @pytest.mark.asyncio
    async def test_apply_injects_script_when_enabled(self):
        shield = FingerprintShield(enabled=True)
        cdp = MagicMock()
        cdp.send = AsyncMock(return_value={"identifier": "script_123"})
        await shield.apply(cdp)
        cdp.send.assert_called_once()
        call_args = cdp.send.call_args
        assert call_args[0][0] == "Page.addScriptToEvaluateOnNewDocument"
        assert shield._applied is True
        assert shield._script_identifier == "script_123"


# ── Geolocation ──────────────────────────────────────────────────────

class TestGeoProfiles:
    def test_us_east_exists(self):
        assert "us_east" in GEO_PROFILES
        p = GEO_PROFILES["us_east"]
        assert p.timezone == "America/New_York"
        assert p.latitude == 40.7128

    def test_japan_exists(self):
        assert "japan" in GEO_PROFILES
        p = GEO_PROFILES["japan"]
        assert p.locale == "ja-JP"


class TestProxyPool:
    def test_empty(self):
        pool = ProxyPool()
        assert pool.next() is None
        assert pool.size == 0
        assert pool.healthy_count == 0

    def test_rotation(self):
        p1 = ProxyEntry(url="http://p1:8080")
        p2 = ProxyEntry(url="http://p2:8080")
        pool = ProxyPool([p1, p2])
        first = pool.next()
        second = pool.next()
        third = pool.next()  # wraps around
        assert first is not None
        assert second is not None
        assert third is not None

    def test_country_filter(self):
        p1 = ProxyEntry(url="http://p1:8080", country="us")
        p2 = ProxyEntry(url="http://p2:8080", country="uk")
        pool = ProxyPool([p1, p2])
        result = pool.next(country="us")
        assert result is not None
        assert result.country == "us"

    def test_mark_failed(self):
        p = ProxyEntry(url="http://p:8080")
        pool = ProxyPool([p])
        pool.mark_failed(p)
        pool.mark_failed(p)
        pool.mark_failed(p)
        assert p.healthy is False
        assert pool.healthy_count == 0


# ── Captcha ──────────────────────────────────────────────────────────

class TestCaptchaDetection:
    @pytest.mark.asyncio
    async def test_no_captcha(self):
        session = MagicMock()
        session.evaluate = AsyncMock(return_value="Welcome to the site")
        session.evaluate_bool = AsyncMock(return_value=False)
        result = await detect_captcha(session)
        assert result == CaptchaType.NONE

    @pytest.mark.asyncio
    async def test_cloudflare_js(self):
        session = MagicMock()
        session.evaluate = AsyncMock(return_value="Just a moment...")
        result = await detect_captcha(session)
        assert result == CaptchaType.CLOUDFLARE_JS


class TestCloudflareAutoSolver:
    @pytest.mark.asyncio
    async def test_solve_success(self):
        session = MagicMock()
        # First call: challenge text present. Later: cleaned.
        responses = [
            "Just a moment...",     # check 1 — still challenge
            "Welcome to the site",  # check 2 — resolved
        ]
        idx = [0]

        async def _eval(expr):
            if idx[0] < len(responses):
                r = responses[idx[0]]
                idx[0] += 1
                return r
            return "Welcome to the site"

        session.evaluate = _eval

        async def _get_cookies():
            return [{"name": "cf_clearance", "value": "abc123"}]

        session.get_cookies = _get_cookies

        solver = CloudflareAutoSolver(max_wait=3.0, poll_interval=0.01)
        result = await solver.solve(session)
        assert result.solved is True
        assert result.cookie_name == "cf_clearance"
        assert result.solver_used == "cloudflare_auto"

    @pytest.mark.asyncio
    async def test_solve_timeout(self):
        session = MagicMock()
        session.evaluate = AsyncMock(return_value="Just a moment...")
        session.get_cookies = AsyncMock(return_value=[])

        solver = CloudflareAutoSolver(max_wait=0.5, poll_interval=0.1)
        result = await solver.solve(session)
        assert result.solved is False
        assert "did not auto-resolve" in (result.error or "")

    @pytest.mark.asyncio
    async def test_solve_eval_returns_none_keeps_waiting(self):
        """When evaluate returns None (context not ready), solver should NOT
        falsely report solved — it should keep waiting (fail-closed)."""
        session = MagicMock()
        session.evaluate = AsyncMock(return_value=None)  # Always None
        session.get_cookies = AsyncMock(return_value=[])

        solver = CloudflareAutoSolver(max_wait=0.5, poll_interval=0.1)
        result = await solver.solve(session)
        assert result.solved is False
        assert "did not auto-resolve" in (result.error or "")

    @pytest.mark.asyncio
    async def test_solve_empty_body_keeps_waiting(self):
        """Empty body text should NOT be treated as 'challenge cleared'."""
        session = MagicMock()
        session.evaluate = AsyncMock(return_value="")
        session.get_cookies = AsyncMock(return_value=[])

        solver = CloudflareAutoSolver(max_wait=0.5, poll_interval=0.1)
        result = await solver.solve(session)
        assert result.solved is False


class TestCaptchaHandler:
    @pytest.mark.asyncio
    async def test_no_captcha_returns_solved(self):
        handler = CaptchaHandler()
        session = MagicMock()
        session.evaluate = AsyncMock(return_value="Welcome")
        session.evaluate_bool = AsyncMock(return_value=False)
        result = await handler.detect_and_solve(session)
        assert result.solved is True
        assert result.captcha_type == CaptchaType.NONE


# ── Headers ──────────────────────────────────────────────────────────

class TestChromeVersion:
    def test_user_agent(self):
        v = ChromeVersion(major=125, full="125.0.6422.142")
        assert "Chrome/125.0.6422.142" in v.user_agent

    def test_brands(self):
        v = ChromeVersion(major=125, full="125.0.6422.142")
        brands = v.brands
        assert {"brand": "Google Chrome", "version": "125"} in brands
        assert {"brand": "Chromium", "version": "125"} in brands

    def test_detect_from_ua(self):
        ua = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.6478.126 Safari/537.36"
        v = detect_chrome_version(ua)
        assert v is not None
        assert v.major == 126
        assert "126.0.6478.126" in v.full

    def test_detect_non_chrome_ua(self):
        ua = "Mozilla/5.0 (X11; Linux x86_64; rv:109.0) Gecko/20100101 Firefox/115.0"
        v = detect_chrome_version(ua)
        assert v is None


# ── Input ────────────────────────────────────────────────────────────

class TestHumanMouse:
    def test_bezier_path(self):
        mouse = HumanMouse.__new__(HumanMouse)
        mouse._viewport_width = 1920
        mouse._viewport_height = 1080
        mouse._current_x = 0
        mouse._current_y = 0

        path = mouse._bezier_path(0, 0, 100, 100, steps=10)
        assert len(path) == 11  # steps + 1
        assert path[0] == (0, 0) or abs(path[0][0]) < 2  # Start ~0,0 (jitter)
        assert path[-1][0] > 90  # End ~100 (jitter)
        assert path[-1][1] > 90


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
