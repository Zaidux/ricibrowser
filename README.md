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
stable references for the current page. Refresh the snapshot after navigation
or DOM changes; references from an older snapshot are rejected.

```python
snapshot = await session.accessibility_snapshot(interactive_only=True)
# Use snapshot["nodes"][0]["ref"] with session.act_reference(...)
```

The snapshot combines the CDP accessibility tree with DOM/ARIA enrichment,
including roles, accessible names, disabled/checked state, and selectors where
available.

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

    # Cookies persist across sessions via CookieJar
    await session.navigate("https://example.com/dashboard")  # authenticated!

    # JS evaluation in isolated world (never Runtime.enable on main world)
    count = await session.evaluate("document.querySelectorAll('script').length")

    # Network capture (opt-in, off by default)
    engine2 = Engine(EngineConfig(debug_network=True))
    session2 = await engine2.create_session()
    # ... browse ...
    flows = engine2.network.to_dict()

    await engine.close()

asyncio.run(main())
```

## Stealth

- `navigator.webdriver` suppressed via `--disable-blink-features=AutomationControlled` (Blink-level, not JS injection)
- Uses the user's real installed Chrome (not bundled Chromium) — TLS/JA3 fingerprint matches a real Chrome release
- Never calls `Runtime.enable` on the main world — isolated worlds only
- `Console.enable` off by default — only enabled in explicit debug mode
- `Network.enable` off by default — known CDP detection vector

## Architecture

See [ARCHITECTURE.md](ARCHITECTURE.md) for the full design.

## License

MIT
