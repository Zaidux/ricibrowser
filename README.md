# ricibrowser

A lightweight two-engine browser automation module built entirely on the Chrome DevTools Protocol (CDP). No Playwright, no Puppeteer, no selenium.

## Engines

| Engine | Use case | Technology |
|--------|----------|------------|
| **Lightpanda** (fast path) | crawl, recon, endpoint discovery, non-JS-heavy targets | Zig-based headless engine, CDP at `ws://127.0.0.1:9222` |
| **CDP-Chrome** (thorough path) | DAST, JS-heavy targets, auth flows, anti-bot | Custom CDP client driving the user's real installed Chrome |

## Install

```bash
pip install ricibrowser

# For the fast path (optional):
bash scripts/install_lightpanda.sh
lightpanda serve --host 127.0.0.1 --port 9222

# For the thorough path:
# Just have Google Chrome installed on your system.
```

## Quick start

## Hybrid page snapshots

CDP-Chrome sessions can expose a bounded accessibility/DOM snapshot with
document-bound references for the current page. References are invalidated by
full document navigations and same-document URL changes; use a fresh snapshot
after either event.

```python
snapshot = await session.accessibility_snapshot(interactive_only=True)
# Use snapshot["nodes"][0]["ref"] with session.act_reference(...)
```

The snapshot combines the CDP accessibility tree with DOM/ARIA enrichment,
including roles, accessible names, disabled/checked state, and selectors where
available.

`Session.snapshot_current_page()` is the supported way to capture the currently
loaded page without navigating. Its `Page` includes HTTP-status provenance,
document generation, navigation/wait diagnostics, actual stealth configuration,
and serializable capabilities. `Page.to_dict()["status"]` is `"partial"` when
the DOM was captured but no HTTP status was observed, and `"error"` when the
navigation itself reported an error; status code `0` remains the compatible
in-memory marker for unknown status.

```python
page = await session.snapshot_current_page(include_accessibility=True)
print(page.status_source, page.document_generation, page.capabilities)
```

`Engine.capabilities` reports the facade's available features. Per-page/session
capabilities are more specific: for example, Lightpanda reports no reliable HTTP
status/final URL and no screenshots, while CDP network capture reports
`network_bodies=False`.

## JavaScript exception surfacing

`Runtime.evaluate` responses now carry CDP `exceptionDetails` through
`session.last_eval_error` — a thrown expression returns `None` **with the
actual JS exception text** instead of an unexplained null, so callers can
distinguish "expression threw" from "returned undefined".

## Trusted clicks and promise-aware evaluation

`Session.click` now dispatches a **trusted** CDP `Input.dispatchMouseEvent` at
the element's center — React/Vue router buttons that ignore synthetic
`el.click()` dispatches respond to these. A synthetic-click fallback remains
for hidden or zero-size elements.

`evaluate`/`evaluate_value` now set `awaitPromise`, so expressions like
`fetch('/api').then(r => r.text())` resolve to the final body instead of an
opaque `{}` — no store-then-read workaround needed.

## Stealth diagnostics

`ricibrowser.stealth_benchmark` provides a defensive, local consistency
benchmark for an operator-owned fixture page. It checks observable signals such
as `navigator.webdriver`, user-agent/client-hint consistency, locale/timezone,
WebGL, canvas/audio stability, plugins, CDP artifacts, and TLS consistency when
the fixture supplies them. The score is a debugging heuristic, not a promise of
invisibility or a vendor bot-detector result. It does not probe third-party
anti-bot systems or attempt to evade them.

```python
import asyncio
from ricibrowser import Engine, EngineConfig

async def main():
    engine = Engine(EngineConfig())

    # Fast path (Lightpanda) — crawl/recon
    page = await engine.fast_browse("https://example.com")
    print(f"Title: {page.title}")
    print(f"Text: {page.text[:200]}")
    print(f"Links: {len(page.links)}")

    # Thorough path (CDP-Chrome) — DAST/auth flows
    session = await engine.create_session()
    await session.navigate("https://example.com/login")
    await session.fill("#username", "admin")
    await session.fill("#password", "pass")
    await session.click("#login-btn")

    # Cookies/localStorage are persisted when the Session closes. Stored
    # localStorage is installed before application scripts on the next visit.
    await session.navigate("https://example.com/dashboard")  # authenticated!

    # JS evaluation in isolated world (never Runtime.enable on main world)
    count = await session.evaluate("document.querySelectorAll('script').length")

    # Network metadata capture (opt-in, off by default). Response bodies are
    # not captured by this CDP monitor.
    engine2 = Engine(EngineConfig(debug_network=True))
    session2 = await engine2.create_session()
    # ... browse ...
    flows = engine2.network.to_dict()

    await session.close()
    await session2.close()
    await engine.close()
    await engine2.close()

asyncio.run(main())
```

## Stealth

- `navigator.webdriver` suppressed via `--disable-blink-features=AutomationControlled` (Blink-level, not JS injection)
- Uses the user's real installed Chrome (not bundled Chromium) — TLS/JA3 fingerprint matches a real Chrome release
- Never calls `Runtime.enable` on the main world — isolated worlds only
- `Console.enable` off by default — only enabled in explicit debug mode
- Debug network archival is off by default. Navigation may still enable the CDP
  Network domain to observe the main-document status and manage cookies.

## Session, persistence, and capture lifecycle

- Every Chrome `Session` owns a distinct page target. Targets share the owning
  Engine's Chrome profile and cookie store; separate tabs are **not** separate
  browser identities.
- Chrome startup/restart is serialized per Engine. `Engine.close()` first closes
  active Sessions, then the owned process and temporary profile.
- `Session.close()` captures cookies and the current origin's localStorage,
  including deletion/empty-state changes, saves the jar, finalizes its network
  capture, closes its CDP connection, and retires its target.
- Cookie-jar saves use a cross-process locked read/merge/write transaction. The
  on-disk JSON schema remains `{cookies, storage}`.
- If cleanup cannot be confirmed, `close()` raises `RuntimeError` and retains
  ownership handles so cleanup can be retried. Call close methods from `finally`.
- Completed debug-network metadata remains available from
  `engine.network.to_dict()` after a Session or `fast_browse()` finishes. The
  archive is bounded (default 5,000 flows); `network.stats` reports drops and
  active clients. Request/response bodies are not captured.

`fast_browse(..., wait_until=..., wait_for=..., timeout=...)` forwards these
options to the Chrome fallback. Persistent `Session.navigate()` accepts the same
wait arguments. `domcontentloaded` waits for the matching browser event rather
than full load; delayed URL changes use a separate bounded change-detection
window (`EngineConfig.url_change_timeout`).

## Proxy scope

`EngineConfig.proxy_url` is **route-only** integration: Chrome receives
`--proxy-server`, and invalid-certificate handling follows the configured proxy
mode. Ricibrowser does not read MiniProxy SQLite data, perform a proxy health
handshake, or correlate CDP records with proxy flow IDs. Capabilities therefore
report `proxy_mode="route_only"`, not full capture integration. Chrome commonly
bypasses loopback destinations; operator-owned loopback tests can opt in with
`extra_chrome_args=["--proxy-bypass-list=<-loopback>"]`.

## Architecture

See [ARCHITECTURE.md](ARCHITECTURE.md) for the full design.

## License

MIT
