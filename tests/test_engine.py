"""Tests for the Engine facade and config."""

import pytest

from ricibrowser.config import EngineConfig, EngineType
from ricibrowser.engine import Engine
from ricibrowser.session import Page


class TestEngineConfig:
    def test_defaults(self):
        config = EngineConfig()
        assert config.fast_engine == EngineType.AUTO
        assert config.thorough_engine == EngineType.CDP_CHROME
        assert config.lightpanda_url == "ws://127.0.0.1:9222"
        assert config.chrome_debug_port == 9223
        assert config.debug_network is False
        assert config.stealth is True

    def test_fast_engine_resolved_auto(self):
        config = EngineConfig()
        assert config.fast_engine_resolved == EngineType.LIGHTPANDA

    def test_fast_engine_resolved_explicit(self):
        config = EngineConfig(fast_engine=EngineType.CDP_CHROME)
        assert config.fast_engine_resolved == EngineType.CDP_CHROME

    def test_from_string(self):
        config = EngineConfig(fast_engine="cdp_chrome")
        assert config.fast_engine == EngineType.CDP_CHROME


class TestPage:
    def test_to_dict(self):
        page = Page(
            url="https://example.com",
            final_url="https://example.com/page",
            status_code=200,
            title="Example",
            text="Hello world",
            html="<html>Hello</html>",
            links=[{"text": "Link", "href": "https://example.com/link"}],
            cookies=[{"name": "session", "value": "abc"}],
            engine="lightpanda",
        )
        d = page.to_dict()
        assert d["status"] == "ok"
        assert d["tool"] == "browse"
        assert d["url"] == "https://example.com/page"
        assert d["http_status"] == 200
        assert d["title"] == "Example"
        assert d["text"] == "Hello world"
        assert d["link_count"] == 1
        assert d["engine"] == "lightpanda"
        assert d["stealth"] is True

    def test_page_dict_cf_detection(self):
        page = Page(
            url="https://example.com",
            final_url="https://example.com",
            status_code=403,
            title="Just a moment...",
            text="",
            html="",
            cloudflare_challenge=True,
            cloudflare_type="cloudflare",
            engine="lightpanda",
        )
        d = page.to_dict()
        assert d["anti_bot_detected"] is True
        assert d["anti_bot_type"] == "cloudflare"


class TestEngineInit:
    def test_engine_creates_without_error(self):
        engine = Engine()
        assert engine.config is not None
        assert engine._cookie_jar is not None
        assert engine._network is not None


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
