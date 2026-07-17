"""Utility functions for URL validation, HTML stripping, and content helpers."""

from __future__ import annotations

import re
from urllib.parse import urljoin, urlparse


_VALID_SCHEMES = ("http", "https")


def validate_url(url: str) -> str:
    """Validate and normalize a URL.

    Raises ValueError if the scheme is not http/https.
    Returns the URL stripped of fragments.
    """
    if not url or not isinstance(url, str):
        raise ValueError("URL must be a non-empty string")
    url = url.strip()
    if not url:
        raise ValueError("URL is empty")
    parsed = urlparse(url)
    if parsed.scheme not in _VALID_SCHEMES:
        raise ValueError(f"URL must use http or https scheme (got: {parsed.scheme or 'none'})")
    if not parsed.netloc:
        raise ValueError("URL must have a host (e.g. https://example.com)")
    return url


def strip_html(html: str) -> str:
    """Strip HTML tags, decode entities, and collapse whitespace.

    Returns plain text suitable for model consumption.
    """
    if not html:
        return ""
    # Remove script/style blocks entirely (their text isn't visible content)
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", "", html, flags=re.DOTALL | re.IGNORECASE)
    # Remove remaining tags
    text = re.sub(r"<[^>]+>", "", text)
    # Decode common HTML entities
    entities = {
        "&amp;": "&", "&lt;": "<", "&gt;": ">", "&quot;": '"',
        "&#39;": "'", "&#x27": "'", "&nbsp;": " ", "&copy;": "(c)",
        "&reg;": "(R)", "&trade;": "(TM)",
    }
    for entity, char in entities.items():
        text = text.replace(entity, char)
    # Decode numeric entities (decimal)
    text = re.sub(r"&#(\d+);", lambda m: chr(int(m.group(1))), text)
    # Decode numeric entities (hex)
    text = re.sub(r"&#x([0-9a-fA-F]+);", lambda m: chr(int(m.group(1), 16)), text)
    # Collapse whitespace
    text = re.sub(r"\s+", " ", text).strip()
    return text


def extract_links(html: str, base_url: str) -> list[dict[str, str]]:
    """Extract all <a href> links from HTML as [{text, href}, ...].

    Resolves relative URLs against base_url.
    """
    if not html or not base_url:
        return []
    links: list[dict[str, str]] = []
    seen: set[str] = set()
    # Match <a href="...">text</a>
    for match in re.finditer(r'<a[^>]*href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', html, re.IGNORECASE | re.DOTALL):
        href = match.group(1).strip()
        text = re.sub(r"<[^>]+>", "", match.group(2)).strip()
        # Skip empty/non-http links
        if not href or href in ("#", "javascript:void(0)", "javascript:;"):
            continue
        # Resolve relative URLs
        resolved = urljoin(base_url, href)
        if resolved in seen:
            continue
        seen.add(resolved)
        links.append({"text": text[:200], "href": resolved})
    return links


def truncate(text: str, max_chars: int = 6000) -> tuple[str, bool]:
    """Truncate text to max_chars. Returns (text, was_truncated)."""
    if len(text) <= max_chars:
        return text, False
    return text[:max_chars].rstrip() + "…", True


def detect_cloudflare(html: str, title: str) -> tuple[bool, str | None]:
    """Detect if a page is showing a Cloudflare/anti-bot challenge.

    Returns (is_challenge, challenge_type).
    """
    html_lower = (html or "").lower()
    title_lower = (title or "").lower()
    checks = [
        ("just a moment", "cloudflare"),
        ("checking your browser", "cloudflare"),
        ("cf-ray", "cloudflare"),
        ("challenge-platform", "cloudflare"),
        ("cf-challenge", "cloudflare"),
        ("attention required", "cloudflare"),
        ("enable javascript and cookies", "generic_captcha"),
        ("please complete the security check", "generic_captcha"),
    ]
    for pattern, ctype in checks:
        if pattern in html_lower or pattern in title_lower:
            return True, ctype
    return False, None
