"""Tests for the extended HumanMouse actions (hover/drag/right/double/scroll).

These pin the CDP dispatch sequences — the trusted-input contracts the
Riciplay CLI's interact steps rely on:

* hover ends with mouseMoved events at the element center (what triggers
  real CSS :hover),
* right click uses button "right" with buttons=2,
* double click sends a second press with clickCount=2 (the dblclick
  synthesizer),
* drag = press → moved-with-buttons=1 along a path → release at target,
* scroll emits chunked mouseWheel events (|delta| <= 120) with the right
  sign,
* the viewport clamps sync from the LIVE window size (the old 1920x1080
  assumption sent coordinates into unreachable space).
"""

import asyncio
import json

from ricibrowser.input import HumanMouse


class FakeCDP:
    def __init__(self):
        self.calls: list[tuple[str, dict]] = []

    async def send(self, method: str, params: dict) -> dict:
        self.calls.append((method, dict(params)))
        return {}


class FakeSession:
    _RESOLVER_JS = "var __rb_resolve = function(s){ return null; };"

    def __init__(self, boxes=None, viewport=(1920, 1080)):
        self._boxes = boxes or {}
        self._viewport = viewport

    async def evaluate(self, expr: str):
        if "window.innerWidth" in expr:
            return json.dumps({"w": self._viewport[0], "h": self._viewport[1]})
        if "getBoundingClientRect" in expr:
            for sel, box in self._boxes.items():
                if json.dumps(sel) in expr:
                    return json.dumps(box)
            return json.dumps(None)
        return None

    async def evaluate_bool(self, expr: str) -> bool:
        return True

    async def click(self, selector: str, timeout: float = 5.0) -> bool:
        self.clicked = selector
        return True


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _events(mouse_cdp: FakeCDP, event_type: str, button: str | None = None):
    out = []
    for method, params in mouse_cdp.calls:
        if method != "Input.dispatchMouseEvent":
            continue
        if params.get("type") != event_type:
            continue
        if button is not None and params.get("button") != button:
            continue
        out.append(params)
    return out


# ── viewport sync ────────────────────────────────────────────────────


def test_viewport_clamps_sync_from_live_window():
    cdp = FakeCDP()
    session = FakeSession(viewport=(800, 600))
    mouse = HumanMouse(cdp)
    assert mouse._viewport_width == 1920  # constructor default
    _run(mouse._sync_viewport(session))
    assert mouse._viewport_width == 800 and mouse._viewport_height == 600
    # Movement to an out-of-window coordinate is clamped into reach.
    _run(mouse.move_to(2000, 1000))
    moved = _events(cdp, "mouseMoved")
    assert moved
    assert all(p["x"] <= 800 + 1 and p["y"] <= 600 + 1 for p in moved)


def test_viewport_sync_failure_is_silent_noop():
    class BoomSession:
        _RESOLVER_JS = ""

        async def evaluate(self, expr):
            raise RuntimeError("detached")

    cdp = FakeCDP()
    mouse = HumanMouse(cdp)
    _run(mouse._sync_viewport(BoomSession()))
    assert mouse._viewport_width == 1920  # unchanged


# ── hover ────────────────────────────────────────────────────────────


def test_hover_moves_to_element_center():
    cdp = FakeCDP()
    session = FakeSession(boxes={".menu": {"x": 120, "y": 240}})
    mouse = HumanMouse(cdp)
    assert _run(mouse.hover_element(session, ".menu")) is True
    moved = _events(cdp, "mouseMoved")
    assert moved
    last = moved[-1]
    assert abs(last["x"] - 120) <= 2 and abs(last["y"] - 240) <= 2


def test_hover_missing_element_is_false():
    cdp = FakeCDP()
    mouse = HumanMouse(cdp)
    assert _run(mouse.hover_element(FakeSession(), "#gone")) is False
    assert not cdp.calls


# ── right click ──────────────────────────────────────────────────────


def test_right_click_uses_right_button():
    cdp = FakeCDP()
    session = FakeSession(boxes={"#row": {"x": 50, "y": 60}})
    mouse = HumanMouse(cdp)
    assert _run(mouse.right_click_element(session, "#row")) is True
    pressed = _events(cdp, "mousePressed", button="right")
    released = _events(cdp, "mouseReleased", button="right")
    assert len(pressed) == 1 and len(released) == 1
    assert pressed[0]["buttons"] == 2


# ── double click ─────────────────────────────────────────────────────


def test_double_click_second_press_has_count_two():
    cdp = FakeCDP()
    session = FakeSession(boxes={"#cell": {"x": 30, "y": 40}})
    mouse = HumanMouse(cdp)
    assert _run(mouse.double_click_element(session, "#cell")) is True
    pressed = _events(cdp, "mousePressed", button="left")
    assert [p["clickCount"] for p in pressed] == [1, 2]


# ── drag ─────────────────────────────────────────────────────────────


def test_drag_presses_moves_and_releases():
    cdp = FakeCDP()
    session = FakeSession(boxes={
        "#knob": {"x": 100, "y": 300},
        "#target": {"x": 500, "y": 300},
    })
    mouse = HumanMouse(cdp)
    assert _run(mouse.drag_element(session, "#knob", to_selector="#target")) is True
    pressed = _events(cdp, "mousePressed", button="left")
    released = _events(cdp, "mouseReleased", button="left")
    assert len(pressed) == 1 and len(released) == 1
    assert abs(pressed[0]["x"] - 100) <= 3
    assert abs(released[0]["x"] - 500) <= 3
    # The drag path: mouseMoved events carrying buttons=1 (button held).
    held = [p for p in _events(cdp, "mouseMoved") if p.get("buttons") == 1]
    assert len(held) >= 10
    xs = [p["x"] for p in held]
    assert min(xs) <= 150 and max(xs) >= 450  # travelled the span


def test_drag_by_offset_lands_at_start_plus_offset():
    cdp = FakeCDP()
    session = FakeSession(boxes={"#slider": {"x": 200, "y": 400}})
    mouse = HumanMouse(cdp)
    assert _run(mouse.drag_element(session, "#slider", dx=300, dy=0)) is True
    released = _events(cdp, "mouseReleased", button="left")
    assert abs(released[0]["x"] - 500) <= 3


def test_drag_requires_target_or_offset():
    cdp = FakeCDP()
    session = FakeSession(boxes={"#knob": {"x": 10, "y": 10}})
    mouse = HumanMouse(cdp)
    assert _run(mouse.drag_element(session, "#knob")) is False
    assert not _events(cdp, "mousePressed")


# ── scroll ───────────────────────────────────────────────────────────


def test_scroll_down_chunked_positive_deltas():
    cdp = FakeCDP()
    mouse = HumanMouse(cdp)
    assert _run(mouse.scroll(FakeSession(), "down", 600)) is True
    wheels = _events(cdp, "mouseWheel")
    assert wheels
    assert all(p["deltaX"] == 0 for p in wheels)
    assert sum(p["deltaY"] for p in wheels) == 600
    assert all(abs(p["deltaY"]) <= 120 for p in wheels)


def test_scroll_up_negative_deltas():
    cdp = FakeCDP()
    mouse = HumanMouse(cdp)
    assert _run(mouse.scroll(FakeSession(), "up", 300)) is True
    wheels = _events(cdp, "mouseWheel")
    assert sum(p["deltaY"] for p in wheels) == -300


def test_scroll_zero_amount_is_noop():
    cdp = FakeCDP()
    mouse = HumanMouse(cdp)
    assert _run(mouse.scroll(FakeSession(), "down", 0)) is True
    assert not cdp.calls


# ── click offset ─────────────────────────────────────────────────────


def test_click_element_at_offset_lands_offset_from_center():
    cdp = FakeCDP()
    session = FakeSession(boxes={"#canvas": {"x": 400, "y": 300}})
    mouse = HumanMouse(cdp)
    assert _run(mouse.click_element_at_offset(session, "#canvas", 150, -80)) is True
    pressed = _events(cdp, "mousePressed", button="left")
    assert len(pressed) == 1
    assert abs(pressed[0]["x"] - 550) <= 4
    assert abs(pressed[0]["y"] - 220) <= 4


def test_click_element_falls_back_when_box_unavailable():
    cdp = FakeCDP()
    session = FakeSession()  # no boxes
    mouse = HumanMouse(cdp)
    assert _run(mouse.click_element(session, "#gone")) is True
    assert session.clicked == "#gone"  # session.click fallback ran
    assert not _events(cdp, "mousePressed")


# ── acquire: re-measure after approach (hover-collapse regression) ────


class ShiftingSession(FakeSession):
    """Element measured at one position, then shifts (hover menu collapsed
    when the cursor left it) — the acquire loop must correct onto it."""

    def __init__(self, first, second):
        super().__init__()
        self._first = first
        self._second = second
        self._probes = 0

    async def evaluate(self, expr):
        if "getBoundingClientRect" in expr:
            self._probes += 1
            box = self._first if self._probes == 1 else self._second
            return json.dumps(box)
        return await super().evaluate(expr)


def test_acquire_corrects_after_layout_shift():
    cdp = FakeCDP()
    # First measurement: hover-expanded (knob pushed 17px down); after the
    # approach move the hover collapses: knob jumps up to y=124.
    session = ShiftingSession({"x": 23, "y": 141}, {"x": 23, "y": 124})
    mouse = HumanMouse(cdp)
    assert _run(mouse.click_element(session, "#knob")) is True
    pressed = _events(cdp, "mousePressed", button="left")
    assert len(pressed) == 1
    # The press landed on the SHIFTED position, not the stale first read.
    assert abs(pressed[0]["y"] - 124) <= 4
    assert abs(pressed[0]["y"] - 141) > 8


def test_acquire_converges_when_stable():
    cdp = FakeCDP()
    session = FakeSession(boxes={"#a": {"x": 50, "y": 60}})
    mouse = HumanMouse(cdp)
    assert _run(mouse.click_element(session, "#a")) is True
    pressed = _events(cdp, "mousePressed", button="left")
    assert abs(pressed[0]["x"] - 50) <= 4


def test_acquire_missing_element_returns_none():
    cdp = FakeCDP()
    mouse = HumanMouse(cdp)
    assert _run(mouse._acquire(FakeSession(), "#gone")) is None
