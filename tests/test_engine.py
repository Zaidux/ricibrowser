"""Tests for the Engine facade and config."""

import os

import pytest

from ricibrowser.chrome_launcher import find_free_port
from ricibrowser.config import EngineConfig, EngineType
from ricibrowser.engine import Engine
from ricibrowser.session import Page


class TestEngineConfig:
    def test_defaults(self):
        config = EngineConfig()
        assert config.fast_engine == EngineType.AUTO
        assert config.thorough_engine == EngineType.CDP_CHROME
        assert config.lightpanda_url == "ws://127.0.0.1:9222"
        # 0 = ephemeral: each Engine binds its own free port so concurrent
        # instances don't collide on one shared Chrome.
        assert config.chrome_debug_port == 0
        assert config.debug_network is False
        assert config.stealth is True

    def test_explicit_debug_port_is_respected(self):
        config = EngineConfig(chrome_debug_port=9223)
        assert config.chrome_debug_port == 9223

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


class TestChromeIsolation:
    """Concurrent Engines must not share one Chrome.

    With a fixed default port and no user_data_dir, the second Engine's launch
    health check found the FIRST Engine's Chrome answering on that port and
    attached to it — both engines then drove the same browser, interleaving
    tabs, cookies and navigations.
    """

    def test_find_free_port_returns_distinct_bindable_ports(self):
        ports = {find_free_port() for _ in range(5)}
        assert len(ports) == 5
        assert all(1024 < p < 65536 for p in ports)

    @pytest.mark.asyncio
    async def test_each_engine_gets_its_own_port_and_profile(self, monkeypatch):
        launches = []

        class _Sentinel(Exception):
            pass

        def fake_launch(**kwargs):
            launches.append(kwargs)
            raise _Sentinel  # stop before the httpx readiness polling

        monkeypatch.setattr("ricibrowser.engine.launch_chrome", fake_launch)

        engines = [Engine(), Engine()]
        try:
            for engine in engines:
                with pytest.raises(_Sentinel):
                    await engine._ensure_chrome()

            ports = [call["port"] for call in launches]
            dirs = [call["user_data_dir"] for call in launches]
            assert len(set(ports)) == 2, ports
            assert all(p for p in ports), "ephemeral port must be resolved before launch"
            assert len(set(dirs)) == 2, dirs
            assert all(d and os.path.isdir(d) for d in dirs)
        finally:
            for engine in engines:
                await engine.close()

        # close() removes the temp profiles it created.
        for path in dirs:
            assert not os.path.exists(path)

    @pytest.mark.asyncio
    async def test_explicit_port_and_profile_are_respected(self, monkeypatch):
        launches = []

        class _Sentinel(Exception):
            pass

        def fake_launch(**kwargs):
            launches.append(kwargs)
            raise _Sentinel

        monkeypatch.setattr("ricibrowser.engine.launch_chrome", fake_launch)

        engine = Engine(EngineConfig(chrome_debug_port=9999, user_data_dir="/tmp/rb-explicit"))
        try:
            with pytest.raises(_Sentinel):
                await engine._ensure_chrome()
            assert launches[0]["port"] == 9999
            assert launches[0]["user_data_dir"] == "/tmp/rb-explicit"
            # An operator-supplied profile is never deleted by close().
            assert engine._temp_user_data_dir is None
        finally:
            await engine.close()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
