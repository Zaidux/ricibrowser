"""Tests for the slider-puzzle CAPTCHA solver.

Covers the pure gap-detection heuristic on synthetic images and the full
solver pipeline (geometry resolve → CDP clip capture → gap analysis →
trusted drag → verification) against fakes — so the algorithm is pinned
without needing a live Aliyun widget.
"""

import asyncio
import base64
import io
import json

from PIL import Image

from ricibrowser.captcha import (
    CaptchaType,
    SliderCaptchaSolver,
    detect_captcha,
    find_gap_offset,
)


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _puzzle_png(width=300, height=150, notch_x=200, notch_w=30) -> bytes:
    """Synthetic slider background: flat grey with a dark notch rectangle."""
    img = Image.new("L", (width, height), color=200)
    px = img.load()
    for x in range(notch_x, min(notch_x + notch_w, width)):
        for y in range(0, height):
            px[x, y] = 60
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


# ── find_gap_offset (pure heuristic) ─────────────────────────────────


def test_find_gap_offset_locates_notch():
    png = _puzzle_png(notch_x=200)
    dx, info = find_gap_offset(png, handle_center_x=15.0)
    assert dx is not None, info
    # The notch's left or right edge is the peak: dx lands near 185-245.
    assert 180 <= dx <= 245, (dx, info)
    assert info["image_width"] == 300
    assert info["peak_energy"] > info["profile_avg"]


def test_find_gap_offset_other_position():
    png = _puzzle_png(notch_x=140)
    dx, _ = find_gap_offset(png, handle_center_x=15.0)
    assert dx is not None
    assert 120 <= dx <= 185, dx


def test_find_gap_offset_bad_bytes():
    dx, info = find_gap_offset(b"not an image", 15.0)
    assert dx is None
    assert "error" in info


def test_find_gap_offset_tiny_image():
    img = Image.new("L", (10, 5), color=128)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    dx, info = find_gap_offset(buf.getvalue(), 5.0)
    assert dx is None
    assert "too small" in info.get("error", "")


# ── detection ────────────────────────────────────────────────────────


class DetectSession:
    """evaluate/evaluate_bool stub returning canned values by substring."""

    def __init__(self, slider_present=False, text=""):
        self._slider = slider_present
        self._text = text

    async def evaluate(self, expr):
        if "document.title" in expr:
            return "Sign in"
        if "innerText" in expr:
            return self._text
        return ""

    async def evaluate_bool(self, expr):
        if "slider" in expr.lower() or "nc_" in expr or "data-riciplay" in expr:
            # The real _SLIDER_PRESENT_JS checks selectors AND body-text
            # hints, so the fake mirrors that combined logic.
            hint = any(h in self._text for h in (
                "拖动滑块", "按住滑块", "请完成安全验证", "滑动验证", "向右滑动",
                "drag to complete", "slide to verify",
            ))
            return self._slider or hint
        return False


def test_detect_slider_by_widget():
    assert _run(detect_captcha(DetectSession(slider_present=True))) == CaptchaType.SLIDER


def test_detect_slider_by_text_hint():
    s = DetectSession(slider_present=False, text="请按住滑块，拖动到最右边")
    assert _run(detect_captcha(s)) == CaptchaType.SLIDER


def test_detect_none_clean_page():
    assert _run(detect_captcha(DetectSession())) == CaptchaType.NONE


# ── full solver pipeline ─────────────────────────────────────────────


class FakeCDP:
    def __init__(self, screenshot_png: bytes):
        self.calls = []
        self._png = screenshot_png

    async def send(self, method, params):
        self.calls.append((method, dict(params)))
        if method == "Page.captureScreenshot":
            return {"data": base64.b64encode(self._png).decode()}
        return {}

    def mouse_events(self, event_type):
        return [p for m, p in self.calls
                if m == "Input.dispatchMouseEvent" and p.get("type") == event_type]


_GEO = {
    "handle_selector": '[data-riciplay-slider="1"]',
    "handle": {"x": 100.0, "y": 300.0, "width": 40.0, "height": 40.0},
    "track": {"x": 100.0, "y": 300.0, "width": 320.0, "height": 40.0},
    "image": {"x": 100.0, "y": 180.0, "width": 300.0, "height": 150.0},
    "root": {"x": 100.0, "y": 180.0, "width": 320.0, "height": 160.0},
}


class SolverSession:
    _RESOLVER_JS = "var __rb_resolve = function(s){ return null; };"

    def __init__(self, cdp, state="success"):
        self._cdp = cdp
        self._state = state

    async def evaluate(self, expr):
        if "window.innerWidth" in expr:
            return '{"w": 1280, "h": 800}'
        if "handle_selector" in expr and "data-riciplay-slider" in expr:
            return json.dumps(_GEO)
        if "验证通过" in expr:
            return self._state
        if "getBoundingClientRect" in expr:
            return json.dumps({"x": 120.0, "y": 320.0})
        return ""

    async def evaluate_bool(self, expr):
        return True


def test_slider_solver_success_pipeline():
    cdp = FakeCDP(_puzzle_png(notch_x=200))
    session = SolverSession(cdp, state="success")
    result = _run(SliderCaptchaSolver(attempts=2, settle_s=0.01).solve(session))
    assert result.solved is True
    assert result.solver_used == "slider_gap_analysis"
    assert result.captcha_type == CaptchaType.SLIDER
    # The trusted drag actually dispatched a press/move/release sequence.
    assert cdp.mouse_events("mousePressed") and cdp.mouse_events("mouseReleased")
    assert result.detail["drag_dx"] > 100


def test_slider_solver_retries_then_fails():
    cdp = FakeCDP(_puzzle_png(notch_x=200))
    session = SolverSession(cdp, state="pending")  # never succeeds
    result = _run(SliderCaptchaSolver(attempts=2, settle_s=0.01).solve(session))
    assert result.solved is False
    assert "did not pass" in (result.error or "")
    # Two attempts → two presses.
    assert len(cdp.mouse_events("mousePressed")) == 2
    assert result.detail["attempt"] == 2


def test_slider_solver_no_widget():
    cdp = FakeCDP(_puzzle_png())

    class NoWidget(SolverSession):
        async def evaluate(self, expr):
            if "window.innerWidth" in expr:
                return '{"w": 1280, "h": 800}'
            if "handle_selector" in expr:
                return None
            return ""

    result = _run(SliderCaptchaSolver(attempts=1).solve(NoWidget(cdp)))
    assert result.solved is False
    assert "No slider widget" in (result.error or "")


def test_slider_solver_without_cdp_reports_clearly():
    class NoCdp:
        _RESOLVER_JS = ""

        async def evaluate(self, expr):
            return ""

    result = _run(SliderCaptchaSolver(attempts=1).solve(NoCdp()))
    assert result.solved is False
    assert "CDP" in (result.error or "")
