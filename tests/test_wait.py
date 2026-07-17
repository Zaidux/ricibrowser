"""Tests for the wait module — auto-waiting helpers."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from ricibrowser.cdp_client import CDPError
from ricibrowser.wait import wait_for_content_stable


@pytest.mark.asyncio
async def test_wait_ready_state_complete():
    """ReadyState 'complete' returns immediately."""
    cdp = MagicMock()
    result = MagicMock()
    result.get.return_value = {"value": "complete"}
    cdp.send = AsyncMock(return_value=result)

    await wait_for_content_stable(cdp, mode="load", timeout=1.0)
    # Should have sent at least one Runtime.evaluate
    cdp.send.assert_called()


@pytest.mark.asyncio
async def test_wait_ready_state_interactive():
    """Domcontentloaded mode accepts 'interactive'."""
    cdp = MagicMock()
    result = MagicMock()
    result.get.return_value = {"value": "interactive"}
    cdp.send = AsyncMock(return_value=result)

    await wait_for_content_stable(cdp, mode="domcontentloaded", timeout=1.0)


@pytest.mark.asyncio
async def test_wait_timeout():
    """If readyState never reaches target, we timeout gracefully."""
    cdp = MagicMock()
    result = MagicMock()
    result.get.return_value = {"value": "loading"}
    cdp.send = AsyncMock(return_value=result)

    # Should not raise — just log and return
    await wait_for_content_stable(cdp, mode="load", timeout=0.5)


@pytest.mark.asyncio
async def test_wait_cdp_error_handled():
    """CDP errors during wait don't crash."""
    cdp = MagicMock()
    cdp.send = AsyncMock(side_effect=CDPError("Runtime.evaluate", -1, "noop"))

    await wait_for_content_stable(cdp, mode="load", timeout=0.5)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
