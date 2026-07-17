"""Tests for utils — URL validation, HTML stripping, link extraction."""

import pytest

from ricibrowser.utils import (
    detect_cloudflare,
    extract_links,
    strip_html,
    truncate,
    validate_url,
)


class TestValidateUrl:
    def test_valid_http(self):
        assert validate_url("http://example.com") == "http://example.com"

    def test_valid_https(self):
        assert validate_url("https://example.com/path?q=1") == "https://example.com/path?q=1"

    def test_invalid_scheme(self):
        with pytest.raises(ValueError, match="http or https"):
            validate_url("ftp://example.com")

    def test_empty(self):
        with pytest.raises(ValueError):
            validate_url("")

    def test_none(self):
        with pytest.raises(ValueError):
            validate_url(None)  # type: ignore

    def test_missing_host(self):
        with pytest.raises(ValueError, match="host"):
            validate_url("https://")


class TestStripHtml:
    def test_simple_tags(self):
        assert strip_html("<p>Hello</p>") == "Hello"

    def test_nested_tags(self):
        assert strip_html("<div><b>Bold</b> text</div>") == "Bold text"

    def test_script_removed(self):
        result = strip_html("<script>alert(1)</script><p>visible</p>")
        assert "alert" not in result
        assert "visible" in result

    def test_entities(self):
        assert strip_html("&lt;b&gt;text&lt;/b&gt;") == "<b>text</b>"

    def test_empty(self):
        assert strip_html("") == ""
        assert strip_html(None) == ""  # type: ignore


class TestExtractLinks:
    def test_basic_links(self):
        html = '<a href="/page1">Page 1</a><a href="https://other.com">Other</a>'
        links = extract_links(html, "https://example.com")
        assert len(links) == 2
        assert links[0]["href"] == "https://example.com/page1"
        assert links[1]["href"] == "https://other.com"

    def test_dedup(self):
        html = '<a href="/page">A</a><a href="/page">B</a>'
        links = extract_links(html, "https://example.com")
        assert len(links) == 1

    def test_skip_javascript(self):
        html = '<a href="javascript:void(0)">Click</a><a href="/real">Real</a>'
        links = extract_links(html, "https://example.com")
        assert len(links) == 1
        assert "real" in links[0]["href"]

    def test_empty(self):
        assert extract_links("", "https://example.com") == []


class TestTruncate:
    def test_short(self):
        text, truncated = truncate("short", 100)
        assert text == "short"
        assert truncated is False

    def test_long(self):
        text, truncated = truncate("a" * 100, 10)
        assert len(text) <= 11  # 10 chars + ellipsis
        assert truncated is True
        assert text.endswith("…")


class TestDetectCloudflare:
    def test_cf_challenge(self):
        html = '<title>Just a moment...</title><script src="/cdn-cgi/challenge-platform/h/g/orchestrate/"></script>'
        is_cf, ctype = detect_cloudflare(html, "Just a moment...")
        assert is_cf is True
        assert ctype == "cloudflare"

    def test_clean_page(self):
        is_cf, ctype = detect_cloudflare("<html><body>Hello</body></html>", "Hello")
        assert is_cf is False
        assert ctype is None

    def test_cf_ray_header(self):
        is_cf, ctype = detect_cloudflare("cf-ray: 123abc", "")
        assert is_cf is True
        assert ctype == "cloudflare"
