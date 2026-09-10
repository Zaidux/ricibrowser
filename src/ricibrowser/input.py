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
    await mouse.hover_element(session, ".menu-item")   # Real :hover
    await mouse.drag_element(session, "#knob", dx=120) # Sliders / DnD
    await mouse.right_click_element(session, "#row")   # Context menus
    await mouse.double_click_element(session, "#cell") # dblclick handlers
    await mouse.scroll(session, "down", 800)           # Trusted wheel events
"""

from __future__ import annotations

import asyncio
import json
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

    async def _sync_viewport(self, session) -> None:
        """Update the movement clamps from the LIVE viewport size.

        The constructor's 1920x1080 default is a guess; real sessions open
        smaller windows (or resize mid-session), and clamping coordinates
        against a stale viewport silently sends events to unreachable
        positions — clicks that hit nothing. Cheap enough to call before
        every element interaction; degrades to a no-op on failure.
        """
        try:
            raw = await session.evaluate(
                "JSON.stringify({w: window.innerWidth, h: window.innerHeight})"
            )
            if raw:
                dims = json.loads(raw)
                w, h = int(dims.get("w") or 0), int(dims.get("h") or 0)
                if w > 0 and h > 0:
                    self._viewport_width = w
                    self._viewport_height = h
                    # A cursor position remembered from a larger viewport
                    # is now unreachable — paths starting there dispatch
                    # coordinates the window can't hold. Re-center.
                    if (self._current_x > w or self._current_y > h
                            or self._current_x < 0 or self._current_y < 0):
                        self._current_x = w / 2
                        self._current_y = h / 2
        except Exception as exc:
            logger.debug("viewport sync failed: %s", exc)

    async def _element_center(self, session, selector: str):
        """Resolve an element and return its viewport-relative center.

        Scrolls the element into view first (a box outside the viewport
        yields coordinates the mouse cannot reach). Returns (x, y) or None.
        """
        box_js = f"""
        (function() {{
            {session._RESOLVER_JS}
            var el = __rb_resolve({json.dumps(selector)});
            if (!el) return JSON.stringify(null);
            if (typeof el.scrollIntoView === 'function') {{
                el.scrollIntoView({{block: 'center', inline: 'center'}});
            }}
            var rect = el.getBoundingClientRect();
            if (!rect.width && !rect.height) return JSON.stringify(null);
            return JSON.stringify({{x: rect.x + rect.width / 2, y: rect.y + rect.height / 2}});
        }})()
        """
        try:
            box_result = await session.evaluate(box_js)
            if box_result:
                box = json.loads(box_result)
                if box and "x" in box:
                    return float(box["x"]), float(box["y"])
        except (json.JSONDecodeError, TypeError) as exc:
            logger.debug("element center resolve failed for %s: %s", selector, exc)
        return None

    async def _acquire(self, session, selector: str, max_rechecks: int = 2):
        """Move the cursor onto an element, re-measuring after the approach.

        Layout shifts AS the cursor moves: hover menus collapse when the
        cursor leaves them, sticky headers appear, overlays dismiss — each
        can move the target between measurement and press. Measuring once
        and dispatching at stale coordinates makes presses land on
        whatever moved into that space (verified live: a hovered menu
        pushed a slider knob 17px down; the press hit the body). So:
        measure → approach → RE-measure → correct when the target moved.
        """
        center = await self._element_center(session, selector)
        if center is None:
            return None
        await self.move_to(center[0], center[1])
        for _ in range(max_rechecks):
            again = await self._element_center(session, selector)
            if again is None:
                return center
            if (abs(again[0] - center[0]) <= 4
                    and abs(again[1] - center[1]) <= 4):
                return again  # stable — cursor is on the target
            center = again
            await self.move_to(center[0], center[1])
        return center

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
        """Move + click a DOM element by CSS selector, label or placeholder.

        Acquires the target with post-approach re-measurement (hover menus
        and sticky headers shift layout as the cursor arrives), then clicks
        with jitter. Falls back to session.click() if box retrieval fails.
        """
        await self._sync_viewport(session)
        center = await self._acquire(session, selector)
        if center is not None:
            await self.click_at(center[0], center[1])
            return True
        # Fallback: use session.click
        return await session.click(selector)

    async def click_element_at_offset(
        self, session, selector: str, offset_x: float, offset_y: float,
    ) -> bool:
        """Click a point offset from an element's center (canvas widgets).

        Canvas/webgl targets expose no per-feature selectors — the agent
        identifies the canvas element and aims at element-relative offsets.
        """
        await self._sync_viewport(session)
        center = await self._acquire(session, selector)
        if center is None:
            return False
        await self.click_at(
            center[0] + float(offset_x), center[1] + float(offset_y),
        )
        return True

    async def hover_element(self, session, selector: str, dwell_s: float = 0.3) -> bool:
        """Move the cursor onto an element and dwell (real CSS :hover).

        Trusted mouseMoved events are what trigger :hover pseudo-classes and
        JS mouseenter/mouseover handlers — synthetic events cannot. The
        dwell lets CSS transitions and delayed menus fire before the caller
        reads the page.
        """
        await self._sync_viewport(session)
        center = await self._acquire(session, selector)
        if center is None:
            return False
        await self.move_to(center[0], center[1])
        await asyncio.sleep(max(0.0, dwell_s) + random.uniform(0.05, 0.2))
        return True

    async def right_click_element(self, session, selector: str) -> bool:
        """Move to an element and perform a human-like right click."""
        await self._sync_viewport(session)
        center = await self._acquire(session, selector)
        if center is None:
            return False
        x = center[0] + random.uniform(-3, 3)
        y = center[1] + random.uniform(-3, 3)
        await self.move_to(x, y)
        await asyncio.sleep(random.uniform(0.05, 0.15))
        await self._dispatch("mousePressed", x, y, button="right", buttons=2, click_count=1)
        await asyncio.sleep(random.uniform(0.03, 0.08))
        await self._dispatch("mouseReleased", x, y, button="right", buttons=0, click_count=1)
        return True

    async def double_click_element(self, session, selector: str) -> bool:
        """Move to an element and perform a rapid double click.

        The second press carries clickCount=2 — that's how Chromium
        synthesizes the dblclick DOM event clients actually listen for.
        """
        await self._sync_viewport(session)
        center = await self._acquire(session, selector)
        if center is None:
            return False
        x = center[0] + random.uniform(-2, 2)
        y = center[1] + random.uniform(-2, 2)
        await self.move_to(x, y)
        await asyncio.sleep(random.uniform(0.05, 0.15))
        # First click
        await self._dispatch("mousePressed", x, y, button="left", buttons=1, click_count=1)
        await asyncio.sleep(random.uniform(0.03, 0.06))
        await self._dispatch("mouseReleased", x, y, button="left", buttons=0, click_count=1)
        # Brief inter-click gap (real double clicks: 40-100ms)
        await asyncio.sleep(random.uniform(0.04, 0.10))
        # Second click, clickCount=2 → dblclick event
        await self._dispatch("mousePressed", x, y, button="left", buttons=1, click_count=2)
        await asyncio.sleep(random.uniform(0.03, 0.06))
        await self._dispatch("mouseReleased", x, y, button="left", buttons=0, click_count=2)
        return True

    async def drag_element(
        self, session, from_selector: str,
        to_selector: str | None = None,
        dx: float | None = None, dy: float | None = None,
    ) -> bool:
        """Mouse-drag an element onto another element, or by pixel offset.

        Presses at the source element's center, moves along a bezier path
        to the target (or to center + (dx, dy) — the slider/canvas shape),
        and releases. This is the mouse-based drag used by sliders, kanban
        boards and map widgets.

        Note: HTML5 drag-and-drop (``draggable=true`` + DataTransfer) is a
        different event family — sites built on it need synthetic
        dragstart/dragover/drop events instead, which the caller can
        dispatch via session.evaluate.
        """
        await self._sync_viewport(session)
        start = await self._acquire(session, from_selector)
        if start is None:
            return False
        if to_selector is not None:
            end = await self._element_center(session, to_selector)
            if end is None:
                return False
        elif dx is not None or dy is not None:
            end = (
                start[0] + float(dx or 0),
                start[1] + float(dy or 0),
            )
        else:
            return False

        sx = start[0] + random.uniform(-2, 2)
        sy = start[1] + random.uniform(-2, 2)
        ex = end[0] + random.uniform(-2, 2)
        ey = end[1] + random.uniform(-2, 2)

        # Move to the source first (real users acquire the handle).
        await self.move_to(sx, sy)
        await asyncio.sleep(random.uniform(0.08, 0.2))

        # Press and drag along a curved path.
        await self._dispatch("mousePressed", sx, sy, button="left", buttons=1, click_count=1)
        await asyncio.sleep(random.uniform(0.05, 0.12))
        steps = random.randint(15, 30)
        for px, py in self._bezier_path(sx, sy, ex, ey, steps):
            await self._dispatch("mouseMoved", px, py, buttons=1)
            await asyncio.sleep(random.uniform(0.008, 0.020))
        # Brief hold at the destination — drop targets often verify the
        # cursor is stationary before accepting.
        await asyncio.sleep(random.uniform(0.05, 0.15))
        await self._dispatch("mouseReleased", ex, ey, button="left", buttons=0, click_count=1)

        self._current_x = ex
        self._current_y = ey
        return True

    async def scroll(
        self, session, direction: str = "down", amount: int = 600,
    ) -> bool:
        """Scroll the page with trusted wheel events at the cursor position.

        Amount is broken into ~100px ticks with human timing — infinite-
        scroll loaders and lazy-mount observers commonly debounce or ignore
        single giant wheel deltas.
        """
        await self._sync_viewport(session)
        total = max(0, int(amount))
        if total == 0:
            return True
        sign = -1 if str(direction).lower() == "up" else 1
        remaining = sign * total
        while remaining:
            chunk = sign * min(120, abs(remaining))
            try:
                await self._cdp.send("Input.dispatchMouseEvent", {
                    "type": "mouseWheel",
                    "x": self._current_x,
                    "y": self._current_y,
                    "deltaX": 0,
                    "deltaY": chunk,
                })
            except CDPError as exc:
                logger.warning("wheel dispatch failed: %s", exc)
                return False
            remaining -= chunk
            await asyncio.sleep(random.uniform(0.05, 0.12))
        return True

    async def type_text(self, session, selector: str, text: str) -> bool:
        """Type text into an input element with human-like timing.

        Focuses and clears the element, then types each character with
        variable 50-150ms delays (matching real typing speed).

        Clearing goes through the session's framework-safe setter rather than
        assigning ``el.value = ''`` — a raw assignment leaves React's value
        tracker holding the old string, so the component never sees the reset.
        """
        # Focus and clear via the framework-safe path, reusing the session's
        # resolver so labels/placeholders work here too.
        if not await session.fill(selector, "", timeout=5.0):
            return False

        focused = await session.evaluate_bool(
            f"""
            (function() {{
                {session._RESOLVER_JS}
                var el = __rb_resolve({json.dumps(selector)});
                if (!el || typeof el.focus !== 'function') return false;
                el.focus();
                return document.activeElement === el;
            }})()
            """
        )
        if focused is not True:
            return False

        # Type each character with CDP Input.dispatchKeyEvent. `text` alone
        # produces a char event but no key identity; frameworks listening for
        # keydown/keyup (validation, masking, autocomplete) need `key` too.
        for char in text:
            try:
                await self._cdp.send("Input.dispatchKeyEvent", {
                    "type": "keyDown",
                    "text": char,
                    "key": char,
                    "unmodifiedText": char,
                })
                await self._cdp.send("Input.dispatchKeyEvent", {
                    "type": "keyUp",
                    "key": char,
                })
                # Variable typing delay (50-150ms)
                await asyncio.sleep(random.uniform(0.05, 0.15))
            except CDPError:
                # Fallback: use fill
                return await session.fill(selector, text)

        # Trigger input/change so controlled components commit the value.
        await session.evaluate(
            f"""
            (function() {{
                {session._RESOLVER_JS}
                var el = __rb_resolve({json.dumps(selector)});
                if (!el) return false;
                el.dispatchEvent(new Event('input', {{bubbles: true}}));
                el.dispatchEvent(new Event('change', {{bubbles: true}}));
                return true;
            }})()
            """
        )

        # Confirm the characters actually landed — CDP key events go to the
        # focused node, so a stolen focus (modal, autocomplete popup) silently
        # types into the wrong place.
        return await session.evaluate_bool(
            f"""
            (function() {{
                {session._RESOLVER_JS}
                var el = __rb_resolve({json.dumps(selector)});
                if (!el) return false;
                var actual = el.isContentEditable ? el.textContent : el.value;
                return actual === {json.dumps(text)};
            }})()
            """
        ) is True
