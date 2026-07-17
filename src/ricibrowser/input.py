"""Human-like mouse movement simulation via CDP Input.dispatchMouseEvent.

Real users don't click instantly at exact pixel coordinates. They move the
mouse along curved paths, with variable timing, and click at slightly random
positions within the target element. This module simulates that behavior
via CDP ``Input.dispatchMouseEvent`` to fool behavioral fingerprinting.

Bezier-curve paths: the mouse follows a quadratic bezier curve from the
current position to the target, with control points that add natural
curvature. Tiny random jitter is added to each point for realism.

Timing: each movement step takes a random 8-20ms (total ~200-500ms for a
typical path), matching real human mouse movement speed.

Usage::

    from ricibrowser.input import HumanMouse

    mouse = HumanMouse(cdp_client)
    await mouse.move_to(500, 300)     # Move to coordinates
    await mouse.click_at(session, "#login-btn")  # Move + click element
    await mouse.type_text(session, "#email", "user@example.com")
"""

from __future__ import annotations

import asyncio
import logging
import math
import random
from typing import Any

from ricibrowser.cdp_client import CDPClient, CDPError

logger = logging.getLogger(__name__)


class HumanMouse:
    """Human-like mouse movement and click simulation via CDP.

    Generates bezier-curve mouse paths with natural timing and random
    jitter to fool behavioral fingerprinting that checks for instant 0ms
    clicks at exact pixel coordinates.
    """

    def __init__(
        self,
        cdp: CDPClient,
        viewport_width: int = 1920,
        viewport_height: int = 1080,
    ):
        self._cdp = cdp
        self._viewport_width = viewport_width
        self._viewport_height = viewport_height
        self._current_x: float = viewport_width / 2
        self._current_y: float = viewport_height / 2

    async def _dispatch(
        self,
        event_type: str,
        x: float,
        y: float,
        button: str = "none",
        buttons: int = 0,
        click_count: int = 0,
    ) -> None:
        """Send a single Input.dispatchMouseEvent."""
        try:
            await self._cdp.send("Input.dispatchMouseEvent", {
                "type": event_type,
                "x": x,
                "y": y,
                "button": button,
                "buttons": buttons,
                "clickCount": click_count,
            })
        except CDPError as exc:
            logger.warning("Mouse dispatch failed: %s", exc)

    def _bezier_path(
        self, x0: float, y0: float, x1: float, y1: float, steps: int = 25,
    ) -> list[tuple[float, float]]:
        """Generate a quadratic bezier-curve path with natural curvature.

        The control point is offset perpendicular to the direct path by a
        random amount, creating a natural arc (real mouse paths curve,
        they don't go in straight lines).
        """
        # Midpoint of the direct path
        mid_x = (x0 + x1) / 2
        mid_y = (y0 + y1) / 2

        # Perpendicular offset for the control point
        dx = x1 - x0
        dy = y1 - y0
        length = math.sqrt(dx * dx + dy * dy) or 1.0

        # Random curvature: 10-30% of path length, perpendicular
        curve_offset = random.uniform(0.1, 0.3) * length
        # Random direction (positive or negative)
        direction = random.choice([-1, 1])

        # Control point
        cx = mid_x + (-dy / length) * curve_offset * direction
        cy = mid_y + (dx / length) * curve_offset * direction

        # Generate points along the bezier curve
        points = []
        for i in range(steps + 1):
            t = i / steps
            # Quadratic bezier: B(t) = (1-t)²·P0 + 2(1-t)t·P1 + t²·P2
            omt = 1 - t
            px = omt * omt * x0 + 2 * omt * t * cx + t * t * x1
            py = omt * omt * y0 + 2 * omt * t * cy + t * t * y1
            # Add tiny jitter (±1px) for realism
            px += random.uniform(-1, 1)
            py += random.uniform(-1, 1)
            points.append((px, py))
        return points

    async def move_to(self, x: float, y: float) -> None:
        """Move the mouse to (x, y) along a bezier-curve path.

        The movement has natural curvature and variable timing (8-20ms
        per step), matching real human mouse movement speed.
        """
        # Clamp to viewport
        x = max(0, min(self._viewport_width, x))
        y = max(0, min(self._viewport_height, y))

        steps = random.randint(15, 30)  # Variable steps for natural speed
        path = self._bezier_path(self._current_x, self._current_y, x, y, steps)

        for px, py in path:
            await self._dispatch("mouseMoved", px, py)
            # Variable delay between steps (8-20ms — real human speed)
            await asyncio.sleep(random.uniform(0.008, 0.020))

        self._current_x = x
        self._current_y = y

    async def click_at(self, x: float, y: float) -> None:
        """Move the mouse to (x, y) and perform a human-like click.

        Adds a small random position offset within ±3px of the target
        (real clicks don't land on exact pixel centers).
        """
        # Add slight randomness to click position (within element bounds)
        click_x = x + random.uniform(-3, 3)
        click_y = y + random.uniform(-3, 3)

        await self.move_to(click_x, click_y)

        # Small pause before clicking (real users have a brief hesitation)
        await asyncio.sleep(random.uniform(0.05, 0.15))

        # Mouse down
        await self._dispatch("mousePressed", click_x, click_y, button="left", buttons=1, click_count=1)
        # Brief hold (30-80ms — real clicks aren't instantaneous)
        await asyncio.sleep(random.uniform(0.03, 0.08))
        # Mouse up
        await self._dispatch("mouseReleased", click_x, click_y, button="left", buttons=0, click_count=1)

    async def click_element(self, session, selector: str) -> bool:
        """Move + click a DOM element by CSS selector.

        Gets the element's bounding box via a single JS evaluation (returns
        JSON), moves to its center (with jitter), and clicks. Falls back to
        session.click() if box retrieval fails.
        """
        # Single evaluation — returns JSON with coordinates or null.
        box_js = f"""
        (function() {{
            var el = document.querySelector({selector!r});
            if (!el) return JSON.stringify(null);
            var rect = el.getBoundingClientRect();
            return JSON.stringify({{x: rect.x + rect.width / 2, y: rect.y + rect.height / 2}});
        }})()
        """
        box_result = await session.evaluate(box_js)
        if box_result:
            try:
                import json
                box = json.loads(box_result)
                if box and "x" in box:
                    await self.click_at(box["x"], box["y"])
                    return True
            except (json.JSONDecodeError, TypeError) as exc:
                logger.warning("click_element: JSON parse failed for %s: %s", selector, exc)
        # Fallback: use session.click
        return await session.click(selector)

    async def type_text(self, session, selector: str, text: str) -> bool:
        """Type text into an input element with human-like timing.

        Focuses the element, clears it, then types each character with
        variable 50-150ms delays (matching real typing speed).
        """
        # Focus the element and clear it
        focus_js = f"""
        (function() {{
            var el = document.querySelector({selector!r});
            if (!el) return false;
            el.focus();
            el.value = '';
            return true;
        }})()
        """
        result = await session.evaluate(focus_js)
        if result not in ("true", "True", True):
            return False

        # Type each character with CDP Input.dispatchKeyEvent
        for char in text:
            try:
                await self._cdp.send("Input.dispatchKeyEvent", {
                    "type": "keyDown",
                    "text": char,
                })
                await self._cdp.send("Input.dispatchKeyEvent", {
                    "type": "keyUp",
                    "text": char,
                })
                # Variable typing delay (50-150ms)
                await asyncio.sleep(random.uniform(0.05, 0.15))
            except CDPError:
                # Fallback: use fill
                return await session.fill(selector, text)

        # Trigger input/change events
        await session.evaluate(
            f"document.querySelector({selector!r})?.dispatchEvent(new Event('input', {{bubbles: true}}))"
        )
        return True
