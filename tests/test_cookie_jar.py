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

    def test_two_instances_preserve_disjoint_updates(self, tmp_path):
        path = str(tmp_path / "jar.json")
        CookieJar(path).save()
        a, b = CookieJar(path), CookieJar(path)
        a.load(); b.load()
        a.update_cookies([{"name": "a", "value": "1", "domain": "a.test", "path": "/"}])
        a.save()
        b.update_cookies([{"name": "b", "value": "2", "domain": "b.test", "path": "/"}])
        b.save()
        saved = json.loads((tmp_path / "jar.json").read_text())
        assert {cookie["name"] for cookie in saved["cookies"]} == {"a", "b"}

    def test_sync_records_cookie_and_empty_storage_deletions(self, tmp_path):
        path = str(tmp_path / "jar.json")
        seed = CookieJar(path)
        original = [{"name": "sid", "value": "old", "domain": "x.test", "path": "/"}]
        seed.set_cookies(original)
        seed.set_storage("https://x.test", {"token": "old"})
        seed.save()
        jar = CookieJar(path); jar.load()
        jar.sync_cookies([], baseline=original)
        jar.sync_storage("https://x.test", {}, baseline={"token": "old"})
        jar.save()
        reloaded = CookieJar(path); reloaded.load()
        assert reloaded.cookies == []
        assert reloaded.get_storage("https://x.test") == {}

    def test_explicit_tilde_path_is_expanded(self, monkeypatch, tmp_path):
        monkeypatch.setenv("HOME", str(tmp_path))
        jar = CookieJar("~/.config/example.json")
        assert jar.path == str(tmp_path / ".config" / "example.json")


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
