"""Tests for cookie jar persistence."""

import json
import stat
import os
import tempfile

import pytest

from ricibrowser.cookie_jar import CookieJar


class TestCookieJar:
    def test_load_save_roundtrip(self):
        jar = CookieJar()
        jar._data = {"cookies": [{"name": "session", "value": "abc"}], "storage": {}}

        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            path = f.name

        try:
            jar.path = path
            jar.save()
            assert stat.S_IMODE(os.stat(path).st_mode) == 0o600

            jar2 = CookieJar(path)
            jar2.load()
            assert len(jar2.cookies) == 1
            assert jar2.cookies[0]["name"] == "session"
        finally:
            os.unlink(path)

    def test_update_cookies_merge(self):
        jar = CookieJar()
        jar._data = {"cookies": [{"name": "a", "domain": ".ex.com", "value": "1"}], "storage": {}}
        jar.update_cookies([
            {"name": "b", "domain": ".ex.com", "value": "2"},
            {"name": "a", "domain": ".ex.com", "value": "updated"},  # Replace
        ])
        assert len(jar.cookies) == 2
        a_cookie = [c for c in jar.cookies if c["name"] == "a"][0]
        assert a_cookie["value"] == "updated"

    def test_get_cookies_for_domain(self):
        jar = CookieJar()
        jar._data = {
            "cookies": [
                {"name": "a", "domain": ".example.com"},
                {"name": "b", "domain": "example.com"},
                {"name": "c", "domain": ".other.com"},
            ],
            "storage": {},
        }
        result = jar.get_cookies_for_domain("example.com")
        assert len(result) == 2  # ".example.com" matches

    def test_storage(self):
        jar = CookieJar()
        jar.set_storage("https://example.com", {"token": "xyz"})
        assert jar.get_storage("https://example.com")["token"] == "xyz"

    def test_clear(self):
        jar = CookieJar()
        jar._data = {"cookies": [{"name": "a"}], "storage": {"x": {}}}
        jar.clear()
        assert jar.cookies == []
        assert jar.get_storage("https://example.com") == {}


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
