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
