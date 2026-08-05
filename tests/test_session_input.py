"""Tests for Session.fill / click / wait_for_selector — framework-safe input.

Regression coverage for the failure where an agent could see a "Full name"
label on a React page but could neither fill nor click the field:

1. ``fill`` assigned ``el.value`` directly. React installs a ``_valueTracker``
   on the node and reads through a native prototype setter, so a raw
   assignment updates the tracker's cached copy and the synthetic onChange
   sees "no change" — the field looked filled but submitted empty.
2. ``click``/``fill`` declared a ``timeout`` parameter and never referenced it.
   A single querySelector ran against a tree that had not mounted yet, so
   client-rendered pages reported "element not found" immediately.
3. Neither could target an element by its visible label, which is the only
   stable handle on component-framework forms with no name/id.

The DOM-level behaviour (native setter actually driving React state) is
verified against real Chrome in the project's browser-backed checks; these
tests pin the Python contract: the timeout is honoured, the resolver is
injected, values are escaped, and a value that fails to stick is reported as
failure rather than success.
"""

import pytest

from ricibrowser.session import Session


def _session(monkeypatch, *, resolves_after=0, retained=True):
    """Build a Session whose JS evaluation is faked.

    *resolves_after* — number of resolve attempts that return False before the
    element "mounts", simulating a late React render.
    *retained* — whether the post-fill verification reports the value stuck.
    """
    client = type("C", (), {"_closed": False, "_event_handlers": {}, "_pending": {}})()
    sess = Session(client)

    state = {"resolve_calls": 0}
    evaluated: list[str] = []

    async def fake_evaluate_bool(expr: str):
        evaluated.append(expr)
        # The post-fill verification compares actual === value.
        if "actual ===" in expr:
            return retained
        # Everything else here is a resolve/exists probe.
        state["resolve_calls"] += 1
        if state["resolve_calls"] <= resolves_after:
            return False
        return True

    async def fake_evaluate(expr: str):
        evaluated.append(expr)
        return "http://target/"

    sess.evaluate_bool = fake_evaluate_bool
    sess.evaluate = fake_evaluate
    sess._wait_for_url_stability = lambda *a, **k: _noop()
    return sess, evaluated, state


async def _noop():
    return None


@pytest.mark.asyncio
async def test_fill_waits_for_late_mounting_element(monkeypatch):
    """A field that mounts after two polls is still filled, not reported missing."""
    sess, _, state = _session(monkeypatch, resolves_after=2)
    assert await sess.fill("Full name", "Ada") is True
    # Proves the wait loop actually polled rather than failing on first miss.
    assert state["resolve_calls"] > 2


@pytest.mark.asyncio
async def test_fill_times_out_when_element_never_appears(monkeypatch):
    """The timeout parameter is honoured instead of being silently ignored."""
    sess, _, _ = _session(monkeypatch, resolves_after=10_000)
    assert await sess.fill("#never", "x", timeout=0.5) is False


@pytest.mark.asyncio
async def test_fill_uses_native_setter_and_clears_value_tracker(monkeypatch):
    """The React-safe path is what gets injected — not a raw el.value assign."""
    sess, evaluated, _ = _session(monkeypatch)
    await sess.fill("Full name", "Ada")
    js = " ".join(evaluated)
    assert "_valueTracker" in js
    assert "getOwnPropertyDescriptor" in js
    assert "desc.set.call" in js


@pytest.mark.asyncio
async def test_fill_reports_failure_when_value_does_not_stick(monkeypatch):
    """A controlled/masked input that rejects the value must not report success."""
    sess, _, _ = _session(monkeypatch, retained=False)
    assert await sess.fill("#masked", "abc") is False


@pytest.mark.asyncio
async def test_resolver_is_injected_for_label_targets(monkeypatch):
    """Label/placeholder/aria-label resolution is available to fill and click."""
    sess, evaluated, _ = _session(monkeypatch)
    await sess.fill("Full name", "Ada")
    js = " ".join(evaluated)
    assert "__rb_resolve" in js
    assert "aria-labelledby" in js
    assert "placeholder" in js
    assert "shadowRoot" in js


@pytest.mark.asyncio
async def test_selector_and_value_are_json_escaped(monkeypatch):
    """Quotes in a selector or value must not break out of the JS string."""
    sess, evaluated, _ = _session(monkeypatch)
    await sess.fill("input[name=\"x'\\\"]", 'va"lue')
    js = " ".join(evaluated)
    # json.dumps escaping, never Python repr (which emits single-quoted JS).
    assert '\\"' in js
    assert "__rb_resolve('" not in js


@pytest.mark.asyncio
async def test_click_waits_then_clicks(monkeypatch):
    """click honours the timeout and scrolls the target into view."""
    sess, evaluated, _ = _session(monkeypatch, resolves_after=1)
    assert await sess.click("Save", wait_for_navigation=False) is True
    assert "scrollIntoView" in " ".join(evaluated)


@pytest.mark.asyncio
async def test_click_returns_false_when_target_never_resolves(monkeypatch):
    sess, _, _ = _session(monkeypatch, resolves_after=10_000)
    assert await sess.click("#ghost", timeout=0.4, wait_for_navigation=False) is False


@pytest.mark.asyncio
async def test_wait_for_selector_returns_bool(monkeypatch):
    sess, _, _ = _session(monkeypatch, resolves_after=1)
    assert await sess.wait_for_selector("Full name", timeout=2.0) is True

    sess2, _, _ = _session(monkeypatch, resolves_after=10_000)
    assert await sess2.wait_for_selector("#nope", timeout=0.3) is False
