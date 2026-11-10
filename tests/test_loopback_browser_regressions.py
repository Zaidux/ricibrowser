"""Optional real-Chrome regressions against an operator-owned loopback fixture.

Run explicitly with ``RICIBROWSER_RUN_CHROME_TESTS=1``.  The normal unit suite
does not launch a browser.
"""
from __future__ import annotations

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from ricibrowser import Engine, EngineConfig, EngineType


pytestmark = pytest.mark.skipif(
    os.environ.get("RICIBROWSER_RUN_CHROME_TESTS") != "1",
    reason="set RICIBROWSER_RUN_CHROME_TESTS=1 to launch local Chrome",
)


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        status = 200
        headers: list[tuple[str, str]] = []
        if self.path == "/a":
            body = "<title>A</title><body>A</body>"
        elif self.path == "/b":
            body = "<title>B</title><body>B</body>"
        elif self.path == "/link":
            body = '<title>Link</title><a id="go" href="/missing">go</a>'
        elif self.path == "/missing":
            status = 404
            body = "<title>Missing</title><body>Missing</body>"
        elif self.path == "/asset":
            body = "<title>Asset</title><img src='/pixel'>"
        elif self.path == "/delayed":
            body = "<title>Delayed</title><script>setTimeout(()=>location.href='/final',700)</script>"
        elif self.path == "/final":
            body = "<title>Final</title><body>Final</body>"
        elif self.path == "/slow":
            body = "<title>Parsed</title><img src='/slow-pixel'>"
        elif self.path == "/slow-pixel":
            import time
            time.sleep(1.5)
            body = "slow"
        elif self.path == "/pixel":
            body = "pixel"
        elif self.path == "/state":
            body = """<title>State</title><body id='value'></body><script>
              document.getElementById('value').innerText =
                (localStorage.getItem('token') || 'MISSING') + '|' + document.cookie;
            </script>"""
        elif self.path == "/set":
            headers.append(("Set-Cookie", "sid=live; Path=/; HttpOnly"))
            body = """<title>Set</title><body>set</body><script>
              localStorage.setItem('token', 'live-storage');
            </script>"""
        elif self.path == "/clear":
            headers.append(("Set-Cookie", "sid=; Max-Age=0; Path=/; HttpOnly"))
            body = """<title>Clear</title><body>clear</body><script>localStorage.clear()</script>"""
        else:
            status = 404
            body = "<title>Unknown</title>"
        payload = body.encode()
        self.send_response(status)
        for name, value in headers:
            self.send_header(name, value)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        try:
            self.wfile.write(payload)
        except (BrokenPipeError, ConnectionResetError):
            # DOMContentLoaded intentionally returns before the slow resource;
            # Chrome may cancel it when the test moves on.
            pass

    def log_message(self, *args):
        pass


@pytest.fixture(scope="module")
def loopback_url():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _config(path, **kwargs):
    return EngineConfig(
        fast_engine=EngineType.CDP_CHROME,
        cookie_jar_path=str(path),
        nav_timeout=5,
        url_stability_timeout=0,
        **kwargs,
    )


@pytest.mark.asyncio
async def test_two_sessions_own_distinct_targets(loopback_url, tmp_path):
    engine = Engine(_config(tmp_path / "jar.json"))
    try:
        a, b = await engine.create_session(), await engine.create_session()
        assert a._target_id and b._target_id and a._target_id != b._target_id
        await a.navigate(loopback_url + "/a")
        await b.navigate(loopback_url + "/b")
        assert await a.evaluate("document.title") == "A"
        assert await b.evaluate("document.title") == "B"
        await a.close(); await b.close()
    finally:
        await engine.close()


@pytest.mark.asyncio
async def test_click_404_status_and_reference_invalidation(loopback_url, tmp_path):
    engine = Engine(_config(tmp_path / "jar.json"))
    try:
        session = await engine.create_session()
        await session.navigate(loopback_url + "/link")
        snap = await session.accessibility_snapshot(interactive_only=True)
        ref = next(node["ref"] for node in snap["nodes"] if node.get("selector") == "#go")
        assert await session.click("#go")
        page = await session.snapshot_current_page()
        assert page.final_url.endswith("/missing")
        assert page.status_code == 404
        stale = await session.act_reference(ref, "click", snapshot_id=snap["snapshot_id"])
        assert stale["error_type"] == "stale_reference"
        await session.close()
    finally:
        await engine.close()


@pytest.mark.asyncio
async def test_storage_restore_precedes_page_script_and_logout_persists(loopback_url, tmp_path):
    jar_path = tmp_path / "jar.json"
    origin = loopback_url
    jar_path.write_text(json.dumps({"cookies": [], "storage": {origin: {"token": "seeded"}}}))

    first = Engine(_config(jar_path))
    try:
        session = await first.create_session()
        page = await session.navigate(loopback_url + "/state")
        assert page.text.startswith("seeded|")
        await session.navigate(loopback_url + "/set")
        await session.close()
    finally:
        await first.close()

    second = Engine(_config(jar_path))
    try:
        session = await second.create_session()
        page = await session.navigate(loopback_url + "/state")
        # HttpOnly is intentionally hidden from document.cookie but must be
        # present in the browser cookie store before the request.
        assert page.text.startswith("live-storage|")
        assert any(c.get("name") == "sid" and c.get("value") == "live" for c in page.cookies)
        await session.navigate(loopback_url + "/clear")
        await session.close()
    finally:
        await second.close()

    saved = json.loads(jar_path.read_text())
    assert not any(cookie.get("name") == "sid" for cookie in saved["cookies"])
    assert saved["storage"][origin] == {}


@pytest.mark.asyncio
async def test_fast_capture_retained_after_return(loopback_url, tmp_path):
    engine = Engine(_config(tmp_path / "jar.json", debug_network=True))
    try:
        await engine.fast_browse(loopback_url + "/asset", wait_until="load")
        flows = engine.network.to_dict()
        assert any(flow["url"].endswith("/asset") for flow in flows)
        assert engine.network.stats["active_clients"] == 0
    finally:
        await engine.close()


@pytest.mark.asyncio
async def test_delayed_redirect_and_domcontentloaded_mode(loopback_url, tmp_path):
    import time

    engine = Engine(EngineConfig(
        cookie_jar_path=str(tmp_path / "jar.json"), nav_timeout=5,
        url_change_timeout=1.25, url_stability_timeout=3,
    ))
    try:
        session = await engine.create_session()
        page = await session.navigate(loopback_url + "/delayed", wait_until="load")
        assert page.final_url.endswith("/final")
        start = time.monotonic()
        page = await session.navigate(loopback_url + "/slow", wait_until="domcontentloaded")
        assert page.title == "Parsed"
        assert time.monotonic() - start < 1.2
        await session.close()
    finally:
        await engine.close()
