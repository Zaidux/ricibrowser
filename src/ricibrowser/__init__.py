"""ricibrowser — a lightweight two-engine browser automation module.

No Playwright, no Puppeteer, no selenium. Built entirely on the public Chrome
DevTools Protocol specification (chromedevtools.github.io/devtools-protocol/).

Two engines:
  - **Lightpanda** (fast path): Zig-based headless engine, CDP at ws://127.0.0.1:9222.
    For crawl/recon/endpoint discovery — ~16× less memory, ~9× faster than Chromium.
  - **CDP-Chrome** (thorough path): custom CDP client driving the user's real
    installed Chrome. For DAST/JS-heavy targets/auth flow testing/anti-bot.

Extensions:
  - **fingerprint**: Canvas/WebGL/audio fingerprint randomization (opt-in).
  - **geolocation**: Timezone/locale/geolocation override + proxy rotation pool.
  - **captcha**: CAPTCHA detection, Cloudflare auto-resolve, solver hooks.
  - **input**: Human-like mouse movement simulation (bezier curves + timing).
  - **headers**: Sec-CH-UA consistency + request header ordering.

Usage::

    from ricibrowser import Engine, EngineConfig, EngineType

    engine = Engine(EngineConfig(fast_engine=EngineType.AUTO))
    page = await engine.fast_browse("https://example.com")
    print(page.title, page.text[:200])

    session = await engine.create_session()
    await session.navigate("https://example.com")
    result = await session.evaluate("document.title")
    await session.close()
"""

from ricibrowser.config import EngineConfig, EngineType
from ricibrowser.engine import Engine
from ricibrowser.session import Page, Session

# Read the version from installed package metadata so it can never drift from
# pyproject.toml (previously a hardcoded string that was left stale for several
# releases). Falls back to "0.0.0" when running from an uninstalled source tree.
try:
    from importlib.metadata import version as _pkg_version, PackageNotFoundError

    try:
        __version__ = _pkg_version("ricibrowser")
    except PackageNotFoundError:
        __version__ = "0.0.0"
except Exception:  # pragma: no cover - defensive
    __version__ = "0.0.0"

__all__ = ["Engine", "EngineConfig", "EngineType", "Page", "Session"]
