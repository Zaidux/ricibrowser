from __future__ import annotations

import pytest
from ricibrowser.session import Session


class FakeCdp:
    def __init__(self, tree=None):
        self._event_handlers = {}
        self.tree = tree or {"nodes": [{"role": {"value": "button"}, "name": {"value": "Save"}, "properties": []}]}

    async def send(self, method, params=None):
        if method == "Accessibility.getFullAXTree": return self.tree
        if method == "Runtime.evaluate": return {"result": {"value": {"elements": [{"tag": "button", "id": "save", "name": "Save"}], "url": "https://example.test"}}}
        raise RuntimeError(method)


@pytest.mark.asyncio
async def test_snapshot_contains_hybrid_accessibility_and_dom_data():
    snapshot = await Session(FakeCdp()).accessibility_snapshot(interactive_only=True)
    assert snapshot["source"] == "cdp_accessibility+dom_aria"
    assert snapshot["nodes"][0]["ref"] == "e1"
    assert snapshot["nodes"][0]["selector"] == "#save"


@pytest.mark.asyncio
async def test_snapshot_limit_and_identity_change():
    tree = {"nodes": [{"role": {"value": "button"}, "name": {"value": str(i)}, "properties": []} for i in range(10)]}
    session = Session(FakeCdp(tree))
    first = await session.accessibility_snapshot(max_nodes=3)
    second = await session.accessibility_snapshot(max_nodes=3)
    assert first["node_count"] == 3 and first["truncated"]
    assert first["snapshot_id"] != second["snapshot_id"]


@pytest.mark.asyncio
async def test_stale_reference_requires_new_snapshot():
    session = Session(FakeCdp())
    await session.accessibility_snapshot()
    result = await session.act_reference("e1", "click", snapshot_id="wrong")
    assert result["error_type"] == "stale_reference"


@pytest.mark.asyncio
async def test_reference_without_selector_is_actionable():
    session = Session(FakeCdp())
    await session.accessibility_snapshot()
    result = await session.act_reference("e1", "click", snapshot_id=session._snapshot_id)
    assert result["status"] == "error"
    assert result["success"] is False


@pytest.mark.asyncio
async def test_reference_unknown_action_is_rejected():
    session = Session(FakeCdp())
    await session.accessibility_snapshot()
    session._snapshot_refs["e1"]["selector"] = "#save"
    result = await session.act_reference("e1", "delete", snapshot_id=session._snapshot_id)
    assert result["status"] == "error"
