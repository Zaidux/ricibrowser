"""Tests for Session.snapshot_form_state / restore_form_state.

The pair exists so an interrupted interact sequence (call timeout, hard
navigation, CDP hiccup) does not cost the agent its filled form values:
snapshot harvests every re-fillable field in one evaluate round-trip,
restore re-applies records through the same fill machinery (label
resolver + framework event dispatch + stick verification) the original
call used. These tests pin the Python contract:

* snapshot: list-in → list-out, non-list/exception → [] (never raises),
* restore: text fields delegate to fill(); checkbox/radio use click
  toggling; multi-select selects options directly; every record gets an
  ok verdict; malformed records are reported not raised.
"""

from typing import Any

import pytest

from ricibrowser.session import Session


def _session(monkeypatch):
    client = type("C", (), {"_closed": False, "_event_handlers": {}, "_pending": {}})()
    sess = Session(client)
    calls: list[tuple[str, Any]] = []

    async def fake_evaluate_value(expr: str):
        calls.append(("evaluate_value", expr))
        return sess._harvest_payload

    async def fake_evaluate_bool(expr: str):
        calls.append(("evaluate_bool", expr))
        return True

    async def fake_fill(selector: str, value: str, timeout: float = 5.0):
        calls.append(("fill", (selector, value)))
        return True

    sess._harvest_payload = []
    sess.evaluate_value = fake_evaluate_value
    sess.evaluate_bool = fake_evaluate_bool
    sess.fill = fake_fill
    return sess, calls


# ── snapshot_form_state ──────────────────────────────────────────────


def test_snapshot_returns_records(monkeypatch):
    sess, calls = _session(monkeypatch)
    sess._harvest_payload = [
        {"selector": "#name", "value": "Alice", "type": "text", "tag": "input"},
        {"selector": "#tos", "checked": True, "type": "checkbox", "tag": "input"},
    ]
    rows = _run(sess.snapshot_form_state())
    assert rows == sess._harvest_payload
    assert calls[0][0] == "evaluate_value"
    assert "selFor" in calls[0][1]  # the harvest JS ran


def test_snapshot_non_list_payload_is_empty(monkeypatch):
    sess, _ = _session(monkeypatch)
    sess._harvest_payload = {"selector": "#x"}  # malformed payload
    assert _run(sess.snapshot_form_state()) == []


def test_snapshot_evaluation_failure_is_empty(monkeypatch):
    sess, _ = _session(monkeypatch)

    async def boom(expr):
        raise RuntimeError("detached frame")

    sess.evaluate_value = boom
    assert _run(sess.snapshot_form_state()) == []


def test_snapshot_drops_rows_without_selector(monkeypatch):
    sess, _ = _session(monkeypatch)
    sess._harvest_payload = [
        {"value": "orphan"},
        {"selector": "#ok", "value": "v", "type": "text", "tag": "input"},
        "not-a-dict",
    ]
    rows = _run(sess.snapshot_form_state())
    assert [r["selector"] for r in rows] == ["#ok"]


# ── restore_form_state ───────────────────────────────────────────────


def test_restore_text_fields_delegate_to_fill(monkeypatch):
    sess, calls = _session(monkeypatch)
    results = _run(sess.restore_form_state([
        {"selector": "#name", "value": "Alice", "type": "text"},
        {"selector": "Email", "value": "a@b.c", "type": "email"},
    ]))
    fills = [c for c in calls if c[0] == "fill"]
    assert fills == [("fill", ("#name", "Alice")), ("fill", ("Email", "a@b.c"))]
    assert all(r["ok"] for r in results)
    assert [r["selector"] for r in results] == ["#name", "Email"]


def test_restore_checkbox_toggles_via_click(monkeypatch):
    sess, calls = _session(monkeypatch)
    results = _run(sess.restore_form_state([
        {"selector": "#tos", "checked": True, "type": "checkbox"},
    ]))
    # No fill call — checkboxes go through the click toggle JS.
    assert not [c for c in calls if c[0] == "fill"]
    bools = [c for c in calls if c[0] == "evaluate_bool"]
    # First bool call is the mount-wait probe; the toggle JS carries the
    # click. json.dumps(True) must produce a JS literal (`true`), not a
    # Python repr — pin it so a regression to f-string interpolation
    # (which would emit invalid JS) fails here.
    toggle_js = next(c[1] for c in bools if "el.click()" in c[1])
    assert "el.checked !== true" in toggle_js
    assert "=== true" in toggle_js
    assert "True" not in toggle_js.replace("textContent", "")
    assert results[0]["ok"] is True
    assert results[0]["type"] == "checkbox"


def test_restore_radio_uses_same_toggle(monkeypatch):
    sess, calls = _session(monkeypatch)
    results = _run(sess.restore_form_state([
        {"selector": "#opt2", "checked": True, "type": "radio"},
    ]))
    toggle_js = next(
        c[1] for c in calls if c[0] == "evaluate_bool" and "el.click()" in c[1]
    )
    assert "el.checked !== true" in toggle_js
    assert results[0]["ok"] is True
    assert results[0]["type"] == "radio"


def test_restore_multiselect_selects_options(monkeypatch):
    sess, calls = _session(monkeypatch)
    results = _run(sess.restore_form_state([
        {"selector": "#tags", "multiple": True, "value": '["a","b"]', "type": "select"},
    ]))
    bools = [c for c in calls if c[0] == "evaluate_bool"]
    sel_js = next(c[1] for c in bools if "selected = want.indexOf" in c[1])
    # Pin the array literal: json.dumps(["a","b"]) -> ["a", "b"] (valid JS),
    # not a Python repr like ['a', 'b'].
    assert '["a", "b"]' in sel_js
    assert "['" not in sel_js
    assert results[0]["ok"] is True


def test_restore_multiselect_list_value_accepted(monkeypatch):
    sess, _ = _session(monkeypatch)
    results = _run(sess.restore_form_state([
        {"selector": "#tags", "multiple": True, "value": ["x", "y"], "type": "select"},
    ]))
    assert results[0]["ok"] is True


def test_restore_multiselect_unparseable_value_left_untouched(monkeypatch):
    sess, calls = _session(monkeypatch)
    results = _run(sess.restore_form_state([
        {"selector": "#tags", "multiple": True, "value": "not-json", "type": "select"},
    ]))
    assert results[0]["ok"] is False
    assert "left untouched" in results[0]["note"]
    # No option-selection JS ran — the live selection was not cleared.
    assert not [c for c in calls if c[0] == "evaluate_bool"
                and "selected = want.indexOf" in c[1]]


def test_restore_non_dict_record_reported_not_raised(monkeypatch):
    sess, _ = _session(monkeypatch)
    results = _run(sess.restore_form_state([
        "not-a-dict",
        {"selector": "#name", "value": "Alice", "type": "text"},
    ]))
    assert results[0]["ok"] is False
    assert "malformed" in results[0]["note"]
    assert results[1]["ok"] is True  # the rest of the replay survived


def test_restore_unmounted_element_reported(monkeypatch):
    sess, _ = _session(monkeypatch)

    async def never_mounts(selector, timeout=5.0):
        return False

    sess.wait_for_selector = never_mounts
    results = _run(sess.restore_form_state([
        {"selector": "#late", "checked": True, "type": "checkbox"},
    ]))
    assert results[0]["ok"] is False
    assert "did not mount" in results[0]["note"]


def test_restore_fill_failure_reports_not_ok(monkeypatch):
    sess, _ = _session(monkeypatch)

    async def failing_fill(selector, value, timeout=5.0):
        return False

    sess.fill = failing_fill
    results = _run(sess.restore_form_state([
        {"selector": "#gone", "value": "x", "type": "text"},
    ]))
    assert results[0]["ok"] is False


def test_restore_empty_and_malformed_inputs(monkeypatch):
    sess, _ = _session(monkeypatch)
    assert _run(sess.restore_form_state([])) == []
    assert _run(sess.restore_form_state(None)) == []
    # A record lacking even a selector still yields a result row, not a raise.
    results = _run(sess.restore_form_state([{"type": "text"}]))
    assert len(results) == 1 and results[0]["ok"] is False


def test_restore_exception_in_field_is_caught(monkeypatch):
    sess, _ = _session(monkeypatch)

    async def boom(selector, value, timeout=5.0):
        raise RuntimeError("frame gone")

    sess.fill = boom
    results = _run(sess.restore_form_state([
        {"selector": "#name", "value": "Alice", "type": "text"},
    ]))
    assert results[0]["ok"] is False


# ── helper ───────────────────────────────────────────────────────────


def _run(coro):
    import asyncio

    return asyncio.new_event_loop().run_until_complete(coro)
