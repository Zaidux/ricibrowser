import pytest

from ricibrowser.cookie_jar import CookieJar
from ricibrowser.session import Session


def test_cookie_identity_includes_path():
    jar = CookieJar()
    jar.update_cookies([
        {"name": "sid", "domain": ".example.com", "path": "/", "value": "root"},
        {"name": "sid", "domain": ".example.com", "path": "/app", "value": "app"},
    ])
    assert {item["value"] for item in jar.cookies} == {"root", "app"}


@pytest.mark.asyncio
async def test_evaluate_preserves_native_json_values():
    class Client:
        _closed = False
        _event_handlers = {}
        _pending = {}

        async def send(self, method, params=None):
            if method == "Page.createIsolatedWorld":
                return {"executionContextId": 1}
            if method == "Runtime.evaluate":
                return {"result": {"value": {"ok": True, "items": [1, 2], "empty": None}}}
            return {}

    session = Session(Client())
    session._frame_id = "frame-1"
    value = await session.evaluate("({ok:true,items:[1,2],empty:null})")
    assert value == {"ok": True, "items": [1, 2], "empty": None}
