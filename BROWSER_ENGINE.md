# BROWSER_ENGINE.md — Ricibrowser Design Specification

> **Status:** Design doc (awaiting approval before implementation)
> **Project:** `ricibrowser` — a two-engine browser automation module for Riciplay
> **Location:** `/root/projects/ricibrowser/`
> **License:** MIT (clean-room implementation, no AGPL-derived code)
> **Created:** 2026-07-17

---

## 1. Goal

Replace Playwright as Riciplay's browsing engine with a lightweight, purpose-built
two-engine layer:

| Engine | Use case | Technology |
|--------|----------|------------|
| **Lightpanda (fast path)** | crawl, recon, endpoint discovery, header analysis, non-JS-heavy targets | Zig-based headless engine, CDP endpoint at `ws://127.0.0.1:9222` |
| **Custom CDP client (thorough path)** | DAST, JS-heavy targets, auth flow testing, anti-bot-protected bug bounty targets | Custom Python CDP client driving the user's real installed Chrome |

**Key principle:** Do NOT wrap Playwright, Puppeteer, nodriver, patchright, or
rebrowser-patches. Everything is built from public, vendor-documented CDP
behavior. This gives us a clean detection fingerprint and no AGPL entanglement.

---

## 2. Where Playwright Currently Gets Called (Replacement Points)

Every Playwright call site in the Riciplay codebase that must be replaced:

### CLI (main path — `riciplay chat`)

| # | File | Line | What | Replace with |
|---|------|------|------|-------------|
| 1 | `cli/riciplay_cli/tools/registry.py` | 395–520 | `tool_browse` — full Playwright sync: launch, context, navigate, screenshot, content, links, cookies, CF bypass | Ricibrowser `Engine.browse()` — auto-selects Lightpada or CDP-Chrome |
| 2 | `cli/riciplay_cli/investigation/tools/browser.py` | 15–106 | `browser_navigate`, `browser_eval`, `browser_screenshot` — Playwright sync with optional `through_proxy` | Ricibrowser `Engine.navigate/eval/screenshot` — proxy passthrough via miniproxy |

### Backend (specialist agents)

| # | File | Line | What | Replace with |
|---|------|------|------|-------------|
| 3 | `backend/services/browser_automation.py` | 99–841 | `BrowserSession` — async Playwright: navigate, screenshot, evaluate, get_dom, click, fill_form, security_analysis, detect_dom_xss, detect_prototype_pollution, blind_injection, dynamic_wordlist | Ricibrowser async `Session` — all the same methods over CDP |
| 4 | `backend/services/dom_scanner.py` | 259–327 | `scan_dom_xss` — async Playwright (currently dead/uncalled) | Ricibrowser async `Session.evaluate()` — and actually wire it into the scan pipeline |

### Dead code to remove

| # | File | What |
|---|------|------|
| 5 | `backend/services/dom_scanner.py` | Dead `scan_dom_xss` — only called from `archive/`. Either rewire or delete. |
| 6 | `cli/pyproject.toml` | `[project.optional-dependencies] browser = ["playwright>=1.40"]` — replace with ricibrowser |
| 7 | `backend/requirements.txt` | `playwright>=1.40.0` — replace with ricibrowser |

### NOT replaced

| Surface | Reason |
|---------|--------|
| `frontend/e2e/*.spec.ts` (`@playwright/test`) | These are E2E tests of the Riciplay UI, not target automation. Playwright stays for testing. |
| `electron/src/main.ts` | Electron's own Chromium is a GUI shell, not headless automation. Unrelated. |
| `backend/routers/browser.py` | This HTTP endpoint uses httpx, not a browser. Already browser-free. |

---

## 3. Module Architecture

```
ricibrowser/
├── README.md
├── ARCHITECTURE.md
├── pyproject.toml
├── src/
│   └── ricibrowser/
│       ├── __init__.py              # Public API: Engine, EngineConfig, EngineType
│       ├── config.py                # EngineConfig dataclass, engine selection
│       ├── engine.py                # Engine facade — auto-selects Lightpanda or CDP-Chrome
│       ├── lightpanda.py            # Lightpanda engine: CDP-over-WS client for ws://127.0.0.1:9222
│       ├── cdp_client.py            # Custom CDP client: raw WebSocket + JSON-RPC to real Chrome
│       ├── chrome_launcher.py       # Find + launch the user's real installed Chrome (--remote-debugging-port)
│       ├── session.py               # Session abstraction (shared interface for both engines)
│       ├── stealth.py               # Stealth: launch-flag-based webdriver suppression, no JS injection
│       ├── cookie_jar.py            # Persistent cookie/localStorage jar (JSON on disk)
│       ├── network.py               # Network capture/interception (opt-in debug mode, off by default)
│       ├── proxy_integration.py     # miniproxy integration: route browser through mitmproxy
│       ├── wait.py                  # Auto-wait: network-idle + DOM-stability polling
│       └── utils.py                 # URL validation, HTML stripping, screenshot helpers
├── tests/
│   ├── test_cdp_client.py
│   ├── test_lightpanda.py
│   ├── test_engine.py
│   ├── test_cookie_jar.py
│   └── test_wait.py
└── scripts/
    └── install_lightpanda.sh        # Binary installer for Linux/macOS
```

---

## 4. Public API

```python
from ricibrowser import Engine, EngineConfig, EngineType

# Auto-select: Lightpanda for crawl/recon, Chrome-CDP for DAST
config = EngineConfig(
    fast_engine=EngineType.LIGHTPANDA,   # default for crawl/recon
    thorough_engine=EngineType.CDP_CHROME,  # default for DAST/JS-heavy
    proxy_url="http://127.0.0.1:8080",   # optional: route through miniproxy
    cookie_jar_path="~/.config/riciplay/cookies.json",
    debug_network=False,                  # opt-in: capture request/response
    stealth=True,                         # suppress navigator.webdriver via flags
)

engine = Engine(config)

# ── Fast path (Lightpanda) ──
page = await engine.fast_browse("https://example.com")
# page.title, page.text, page.html, page.links, page.cookies, page.status_code

# ── Thorough path (CDP-Chrome) ──
session = await engine.create_session()
await session.navigate("https://target.com/login")
await session.fill("#username", "admin")
await session.fill("#password", "pass")
await session.click("#login-btn")
# Cookies/localStorage now persisted in the jar for subsequent calls
await session.navigate("https://target.com/dashboard")  # authenticated!

# JS evaluation in ISOLATED world (never main world)
result = await session.evaluate("document.querySelectorAll('script').length")

# Screenshot
await session.screenshot("/tmp/dashboard.png")

# Network capture (debug mode)
async with engine.debug_network() as net:
    await session.navigate("https://api.target.com/users")
    flows = net.flows  # list of {request, response, timing}

await session.close()
```

---

## 5. Engine 1 — Lightpanda (Fast Path)

### 5.1 What it is
A Zig-based headless browser that speaks CDP natively at `ws://127.0.0.1:9222`.
No rendering engine (no screenshots/pixels), but full JS execution via real V8.
~16× less memory and ~9× faster than Chromium for 100-page batches.

### 5.2 Installation
```bash
# Linux x86_64
curl -L -o /usr/local/bin/lightpanda \
  https://github.com/lightpanda-io/browser/releases/download/nightly/lightpanda-x86_64-linux
chmod +x /usr/local/bin/lightpanda

# Or use the bundled installer script:
bash scripts/install_lightpanda.sh
```

### 5.3 Integration
- `lightpanda.py` connects to `ws://127.0.0.1:9222` using a minimal WebSocket
  client (no Playwright dependency).
- Sends CDP commands via JSON-RPC: `Page.navigate`, `Runtime.evaluate`,
  `DOM.getDocument`, `Network.getCookies`, etc.
- Lightpanda exposes the WS endpoint at the root (`ws://host:port`), not at
  `/devtools/browser/<guid>` like Chrome. The client handles both path formats.
- Security knobs enabled by default: `--block-private-networks`,
  `--http-max-response-size=10485760` (10 MB), `LIGHTPANDA_DISABLE_TELEMETRY=true`.

### 5.4 Limitations
- No screenshots (no rendering engine). For screenshots, auto-fallback to CDP-Chrome.
- No `Mozilla` in the User-Agent (forbidden by Lightpanda). Sites that gate on
  this UA pattern will see Lightpanda — acceptable for recon, not for stealth.
- Beta stability — crashes possible. The engine catches and falls back to CDP-Chrome.

---

## 6. Engine 2 — Custom CDP Client (Thorough Path)

### 6.1 What it is
A from-scratch Python CDP client that drives the user's **real installed Chrome**
(not a bundled Chromium) via `--remote-debugging-port`. Built using only the
public CDP spec from `chromedevtools.github.io/devtools-protocol/`.

### 6.2 Chrome Discovery & Launch
```python
# chrome_launcher.py
def find_chrome() -> str:
    """Find the user's real installed Chrome channel."""
    for name in ["google-chrome", "google-chrome-stable", "chrome",
                 "chromium-browser", "chromium", "google-chrome-beta"]:
        path = shutil.which(name)
        if path:
            return path
    raise RuntimeError("No Chrome/Chromium found on $PATH")

def launch_chrome(port: int = 9223, proxy: str | None = None) -> subprocess.Popen:
    """Launch Chrome with stealth flags (no JS injection)."""
    args = [
        find_chrome(),
        "--headless=new",                      # new headless mode (Chrome 112+)
        f"--remote-debugging-port={port}",
        "--disable-blink-features=AutomationControlled",  # suppress webdriver flag
        "--no-first-run",
        "--disable-default-apps",
        "--disable-extensions",
        "--disable-background-networking",
        "--disable-sync",
        "--disable-translate",
        "--mute-audio",
        "--no-sandbox",
        "--disable-dev-shm-usage",
    ]
    if proxy:
        args.append(f"--proxy-server={proxy}")
    return subprocess.Popen(args, stdout=DEVNULL, stderr=DEVNULL)
```

**Key:** `--disable-blink-features=AutomationControlled` suppresses
`navigator.webdriver` at the Blink layer (launch flag, not JS injection). This
is the approach that doesn't leave a JS-override detection fingerprint.

### 6.3 CDP Session Layer
```python
# cdp_client.py
class CDPClient:
    """Minimal CDP WebSocket client — no Playwright/Puppeteer dependency."""

    def __init__(self, ws_url: str):
        self.ws = websockets.connect(ws_url)
        self._msg_id = 0
        self._pending: dict[int, asyncio.Future] = {}

    async def send(self, method: str, params: dict = None) -> dict:
        """Send a CDP command and await the response."""
        self._msg_id += 1
        msg = {"id": self._msg_id, "method": method, "params": params or {}}
        await self.ws.send(json.dumps(msg))
        # Response matched by id in the receive loop
        ...

    async def create_isolated_world(self, frame_id: str) -> str:
        """Create an isolated execution context (NOT the main world).

        Per the CDP spec: Page.createIsolatedWorld creates a new isolated
        world for the given frame. We NEVER call Runtime.enable on the main
        context — all JS evaluation goes through isolated worlds.
        """
        result = await self.send("Page.createIsolatedWorld", {
            "frameId": frame_id,
            "worldName": "ricibrowser_isolated",
        })
        return result["executionContextId"]
```

### 6.4 Anti-Detection Design (Stealth)

| Detection vector | Playwright approach | Ricibrowser approach |
|---|---|---|
| `navigator.webdriver === true` | JS init-script override (detectable) | `--disable-blink-features=AutomationControlled` (Blink-level, no JS trace) |
| `navigator.plugins` empty | JS patch | Not patched — real Chrome has real plugins |
| `navigator.languages` | JS patch | Real Chrome values (no patch needed) |
| `window.chrome` missing | JS patch | Present in real Chrome (no patch needed) |
| CDP detection (Runtime.enable) | Called by default | **Never called on main world** — isolated worlds only |
| `Console.enable` | Called by default | **Off by default** — only enabled in debug mode |
| TLS/JA3 fingerprint | Bundled Chromium → matches Chrome | **Real installed Chrome** → matches real Chrome release |

### 6.5 Cookie/Session Persistence

```python
# cookie_jar.py
class CookieJar:
    """Persistent cookie + localStorage jar across browser sessions.

    Saves to JSON on disk (~/.config/riciplay/cookies.json) so authenticated
    sessions survive browser restarts — critical for scanning behind login.
    """

    def save_cookies(self, cookies: list[dict]):
        """CDP Network.getAllCookies → JSON file."""

    def load_cookies(self) -> list[dict]:
        """JSON file → CDP Network.setCookies."""

    def save_storage(self, origin: str, storage: dict):
        """DOM storage (localStorage) via CDP DOMStorage.*"""

    def load_storage(self, origin: str) -> dict:
        """Load localStorage for an origin."""
```

### 6.6 Network Capture (Debug Mode — Off by Default)

```python
# network.py
class NetworkCapture:
    """Opt-in request/response capture for security debugging.

    OFF by default (detection vector if left on). When enabled:
      - Listens to CDP Network.requestWillBeSent / Network.responseReceived
      - Captures {url, method, headers, body, status, timing}
      - Integrates with miniproxy for full HTTPS interception
    """

    def __init__(self, enabled: bool = False):
        self.enabled = enabled
        self.flows: list[dict] = []

    async def start(self, cdp: CDPClient):
        if not self.enabled:
            return
        await cdp.send("Network.enable")
        # Register event handlers...

    async def stop(self, cdp: CDPClient):
        if not self.enabled:
            return
        await cdp.send("Network.disable")
```

**Why off by default:** `Network.enable` is a known detection vector (sites can
detect the CDP listener). It's only turned on when the operator explicitly
requests debug network capture. This is the opposite of stealth-scraping tools
that leave everything on — we're a security tool that WANTS visibility, but
only when we ask for it.

### 6.7 Auto-Waiting

```python
# wait.py
async def wait_for_content_stable(cdp: CDPClient, timeout: float = 10.0):
    """Poll for network idle + DOM stability before considering navigation done.

    1. Wait for Page.loadEventFired
    2. Poll DOM subtree size every 200ms — if stable for 3 consecutive polls (600ms), done
    3. Check for outstanding network requests (if Network domain enabled)
    4. Fallback to timeout
    """
```

---

## 7. miniproxy Integration

miniproxy (now at `/root/projects/miniproxy/`) is a mitmproxy-based HTTP/HTTPS
interception proxy. Integration with ricibrowser:

1. **Browser-through-proxy:** Chrome launched with `--proxy-server=http://127.0.0.1:8080`
   so all HTTP/HTTPS traffic flows through mitmproxy → captured in SQLite.

2. **CDP + miniproxy combined:** When both the CDP client and miniproxy are
   active, the browser's traffic is intercepted at the HTTP layer (miniproxy)
   while DOM/JS is controlled at the CDP layer (ricibrowser). This gives both
   HTTP-level visibility (for replay/fuzzing) and DOM-level control (for
   JS-heavy auth flows).

3. **Cookie sharing:** miniproxy captures `Set-Cookie` headers; ricibrowser's
   `CookieJar` reads these from miniproxy's SQLite DB and loads them into the
   CDP session, so a login flow captured by miniproxy is automatically
   available for subsequent browser navigation.

4. **Discrepancy fix:** The miniproxy `db.py` is missing `get_config` /
   `set_config` methods (used by `intercept_addon.py`). This will be fixed as
   part of the integration so intercept mode works correctly.

---

## 8. How It Plugs Into the Existing Pipeline

### 8.1 CLI `browse` tool replacement

```python
# In riciplay_cli/tools/registry.py — tool_browse goes from Playwright to:
from ricibrowser import Engine, EngineConfig

def tool_browse(url, action, **kwargs):
    engine = Engine(EngineConfig(fast_engine=EngineType.AUTO))
    page = engine.fast_browse_sync(url)  # sync wrapper for CLI
    # Returns the SAME dict format the model expects:
    # {status, tool, action, url, http_status, title, text/html/links, cookies}
```

### 8.2 Backend `BrowserSession` replacement

```python
# In backend/services/browser_automation.py — BrowserSession becomes:
from ricibrowser import Engine

class BrowserSession:
    def __init__(self):
        self._engine = Engine(EngineConfig(thorough_engine=EngineType.CDP_CHROME))
        self._session = None

    async def navigate(self, url):
        self._session = await self._engine.create_session()
        await self._session.navigate(url)

    # All existing methods (evaluate, screenshot, get_dom, click, fill_form,
    # security_analysis) delegate to self._session with the same signatures.
```

### 8.3 Investigation `browser_navigate` replacement

```python
# In riciplay_cli/investigation/tools/browser.py — goes from Playwright to:
from ricibrowser import Engine

def browser_navigate(url, through_proxy=False):
    config = EngineConfig()
    if through_proxy:
        config.proxy_url = "http://127.0.0.1:8080"
    engine = Engine(config)
    session = engine.create_session_sync()
    session.navigate(url)
    return session
```

---

## 9. Implementation Phases

### Phase 1 — Lightpanda Wiring (self-contained, ~1 PR)
- [ ] `ricibrowser/` project skeleton + pyproject.toml
- [ ] `lightpanda.py`: CDP-over-WebSocket client for `ws://127.0.0.1:9222`
- [ ] `engine.py`: `Engine.fast_browse()` → returns `{title, text, html, links, cookies, status_code}`
- [ ] `scripts/install_lightpanda.sh`: binary installer
- [ ] Unit tests (mock WS, verify CDP command/response structure)
- [ ] Integration test: start lightpanda, browse example.com, verify title
- [ ] Wire into CLI `tool_browse` as the fast-path (Playwright fallback until verified)
- [ ] Move miniproxy to `/root/projects/miniproxy/` (done)
- [ ] Fix miniproxy `db.py` missing `get_config`/`set_config`

### Phase 2 — Custom CDP Client (separate PR-sized chunk)
- [ ] `chrome_launcher.py`: find + launch real Chrome with stealth flags
- [ ] `cdp_client.py`: raw WebSocket CDP JSON-RPC client
- [ ] `session.py`: navigate, evaluate (isolated world), screenshot, click, fill
- [ ] `stealth.py`: `--disable-blink-features=AutomationControlled`, no JS injection
- [ ] `cookie_jar.py`: persistent cookie/localStorage in JSON
- [ ] `network.py`: opt-in network capture (off by default)
- [ ] `wait.py`: network-idle + DOM-stability auto-wait
- [ ] `proxy_integration.py`: route Chrome through miniproxy
- [ ] Wire into backend `BrowserSession`
- [ ] Wire into CLI investigation `browser_navigate`/`browser_eval`/`browser_screenshot`
- [ ] Delete `backend/services/dom_scanner.py` dead code OR rewire it
- [ ] Remove Playwright from `backend/requirements.txt` + `cli/pyproject.toml`

---

## 10. Explicitly Out of Scope (Backlog)

These are intentionally NOT built yet. Flagged for future work:

| Backlog item | Why deferred | Priority |
|---|---|---|
| Canvas/WebGL fingerprint randomization | Complex, requires CDP `Emulation` domain + GPU context. Not needed for recon/DAST. | Low |
| Proxy/geolocation rotation | Requires proxy-pool management + `Emulation.setGeolocationOverride`. Not a browser-engine concern. | Medium |
| CAPTCHA solving | Complex (OCR, ML models, third-party services). Intentionally out of scope. | High (but separate) |
| **CDP-based Cloudflare/anti-bot bypass** | See §11 below — this IS in scope as a Phase 2 stretch. | High |

---

## 11. Anti-Bot & Cloudflare Bypass (In-Scope Stretch Goal)

The user's goal: "being able to bypass CAPTCHA and Cloudflare protected platforms."

### What we CAN do (no external services, no CAPTCHA solving):
1. **Real Chrome TLS/JA3 fingerprint** — using the user's real installed Chrome,
   not bundled Chromium. TLS ClientHello matches a real Chrome release.
2. **`navigator.webdriver` = false** — via Blink flag, not JS injection.
3. **No CDP detection** — never call `Runtime.enable` on main world,
   `Console.enable` off by default.
4. **Real Chrome UA + headers** — real plugins, real languages, real `window.chrome`.
5. **Cloudflare challenge auto-wait** — poll for challenge form to clear,
   wait for "Just a moment" to disappear, then capture the post-challenge cookies.
6. **Cookie persistence** — if a CF challenge passes, the `cf_clearance` cookie
   is saved to the jar and reused on subsequent navigation.

### What we CANNOT do (requires external services — flagged as backlog):
- reCAPTCHA / hCaptcha / Turnstile solving
- JS challenge emulation (would need to execute the obfuscated CF JS)
- Headless detection bypass beyond webdriver flag (e.g., WebDriver Chrome flag,
  CDP artifacts that some advanced detectors look for)

### Additional anti-detection features I recommend adding:
1. **User-data-dir persistence** — reuse a Chrome profile directory across
   sessions so `cf_clearance` and site cookies survive browser restarts. This
   is how real users work — they don't re-solve CF every time.
   ```python
   args.append(f"--user-data-dir={profile_dir}")
   ```
2. **Request header ordering** — CDP `Network.setExtraHTTPHeaders` to ensure
   header order matches real Chrome (Sensitive header ordering detection).
3. **Mouse movement simulation** — CDP `Input.dispatchMouseEvent` with
   bezier-curve mouse paths for click actions (fools simple behavioral
   fingerprinting that checks for instant 0ms clicks).
4. **Viewport + screen size consistency** — use the real display's dimensions
   if available, not a fixed 1920×1080 that doesn't match the host's screen.
5. **`Sec-CH-UA` consistency** — ensure `Sec-CH-UA` headers match the real
   Chrome version installed (CDP `Network.setUserAgentOverride` with the full
   client hints, not just the UA string).
6. **Process-level stealth** — launch Chrome with `--disable-features=IsolateOrigins`
   and `--disable-site-isolation-trials` to match real Chrome behavior (some
   detectors check for isolation flags that headless enables by default).

These are all CDP-native, no JS injection, no external dependencies.

---

## 12. Dependencies

### ricibrowser (this module)
```
websockets>=12.0       # CDP WebSocket transport
httpx>=0.27            # HTTP for CDP /json/version discovery
aiofiles>=23.0         # async file I/O for cookie jar
```

**NO Playwright, NO Puppeteer, NO selenium, NO pyppeteer.**

### miniproxy (already installed)
```
mitmproxy>=10.0.0
Flask>=3.0.0
requests>=2.31.0
```

### System requirements (for thorough path)
- Google Chrome or Chromium installed on the host (real channel, not bundled)
- miniproxy's CA cert trusted (for HTTPS interception through the proxy)

---

## 13. Testing Strategy

| Test | Method |
|---|---|
| CDP command encoding | Unit test — verify JSON-RPC structure matches spec |
| Isolated world creation | Unit test — verify `Page.createIsolatedWorld` is called, `Runtime.enable` is NOT |
| Cookie round-trip | Integration test — navigate, get cookies, save, reload, verify present |
| miniproxy integration | Integration test — start miniproxy, route browser through it, verify captured flows |
| Stealth verification | Integration test — browse a bot-detection page (e.g. `bot.sannysoft.com`), verify `navigator.webdriver` is false |
| Cloudflare bypass | Manual test — browse a CF-protected site with `cloudflare_bypass` action |
| Lightpanda fallback | Unit test — verify engine falls back to CDP-Chrome when Lightpanda is unavailable |

---

## 14. Success Criteria

1. `riciplay chat` → `browse https://example.com` works with Lightpanda (fast path)
2. `riciplay chat` → `browse https://js-heavy-spa.com` works with CDP-Chrome (thorough path)
3. Authenticated scan: login flow → cookies persisted → subsequent browse is authenticated
4. miniproxy + ricibrowser: browser traffic flows through miniproxy, captured in SQLite
5. `navigator.webdriver` is `false` on a detection test page
6. No Playwright import anywhere in the active codebase (only frontend E2E tests)
7. CR + CLI tests pass with the new engine

---

## 15. Public Repository

This module will be a **public GitHub repo** (`ricibrowser`) — a standalone,
reusable browser automation library. Riciplay will depend on it via pip:
```toml
# riciplay/cli/pyproject.toml
dependencies = [
    "ricibrowser>=0.1.0",
]
```
