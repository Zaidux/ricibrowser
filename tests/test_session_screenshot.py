"""Tests for Session.screenshot — no 0-byte PNGs left behind on failure.

Regression coverage for the failure where a CDP error (or an empty ``data``
field) still returned the path of the temp file created up front. Callers saw a
"successful" screenshot path pointing at a 0-byte PNG, which then failed to
open downstream far away from the actual cause.
"""

import os

import pytest

from ricibrowser.cdp_client import CDPError
from ricibrowser.session import Session


def _session(send):
    client = type("C", (), {"_closed": False, "_event_handlers": {}, "_pending": {}})()
    client.send = send
    return Session(client)


@pytest.mark.asyncio
async def test_screenshot_writes_png_on_success():
    async def send(method, params=None):
        # base64 of b"PNGDATA"
        return {"data": "UE5HREFUQQ=="}

    sess = _session(send)
    path = await sess.screenshot()
    try:
        assert path is not None
        with open(path, "rb") as f:
            assert f.read() == b"PNGDATA"
    finally:
        if path:
            os.unlink(path)


@pytest.mark.asyncio
async def test_screenshot_cdp_error_returns_none_and_removes_temp_file():
    created: list[str] = []

    async def send(method, params=None):
        raise CDPError("Page.captureScreenshot", -1, "not supported")

    sess = _session(send)
    # Capture the temp path the implementation allocates.
    import tempfile as _tf
    real_mkstemp = _tf.mkstemp

    def spy_mkstemp(*args, **kwargs):
        fd, path = real_mkstemp(*args, **kwargs)
        created.append(path)
        return fd, path

    _tf.mkstemp = spy_mkstemp
    try:
        result = await sess.screenshot()
    finally:
        _tf.mkstemp = real_mkstemp

    assert result is None
    assert created, "expected a temp file to have been allocated"
    assert not os.path.exists(created[0]), "empty temp PNG must be removed"


@pytest.mark.asyncio
async def test_screenshot_empty_data_returns_none():
    async def send(method, params=None):
        return {"data": ""}

    sess = _session(send)
    assert await sess.screenshot() is None


@pytest.mark.asyncio
async def test_screenshot_keeps_caller_supplied_path_on_failure(tmp_path):
    """We only delete files we created. A caller-supplied path is theirs — it
    may already hold a previous screenshot we must not destroy."""
    target = tmp_path / "shot.png"
    target.write_bytes(b"previous")

    async def send(method, params=None):
        raise CDPError("Page.captureScreenshot", -1, "boom")

    sess = _session(send)
    assert await sess.screenshot(path=str(target)) is None
    assert target.read_bytes() == b"previous"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
