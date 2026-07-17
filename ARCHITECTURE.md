# ricibrowser — Architecture

## Overview

ricibrowser replaces Playwright as Riciplay's browsing engine with a two-engine
layer built entirely on CDP (Chrome DevTools Protocol).

```
┌─────────────────────────────────────────────────────┐
│                    Engine (facade)                  │
│  fast_browse() → Lightpanda  |  create_session()   │
│     (fall back to Chrome)     │  → CDP-Chrome       │
├──────────────┬───────────────┼──────────────────────┤
│  Lightpanda  │   CDP Client  │   CookieJar / Network│
│  (ws://9222) │   (ws://9223)  │   (opt-in capture)   │
└──────┬───────┴───────┬───────┴──────────────────────┘
       │               │
       ▼               ▼
  Zig headless    Real Chrome
  (no rendering)   (full rendering)
```

## Module boundaries

| Module | Responsibility |
|--------|---------------|
| `config.py` | EngineConfig dataclass, EngineType enum, auto-selection |
| `engine.py` | Engine facade — lifecycle, fallback, cookie jar loading |
| `cdp_client.py` | Raw WebSocket CDP JSON-RPC client (command/response, events, target discovery) |
| `lightpanda.py` | Lightpada engine: connects to ws://127.0.0.1:9222, CDP browse |
| `chrome_launcher.py` | Find + launch real installed Chrome with --remote-debugging-port |
| `session.py` | Session + Page abstractions: navigate, evaluate (isolated world), screenshot, click, fill |
| `stealth.py` | Chrome launch flags for naviagation.webdriver suppression (no JS injection) |
| `cookie_jar.py` | Persistent JSON cookie/localStorage jar (survives browser restarts) |
| `network.py` | Opt-in request/response capture via CDP Network domain (off by default) |
| `wait.py` | Auto-wait: network-idle + DOM-stability polling |
| `proxy_integration.py` | miniproxy (mitmproxy) integration: route Chrome through the proxy |
| `utils.py` | URL validation, HTML stripping, link extraction, CF detection, truncation |

## Anti-detection design

| Vector | Playwright approach | ricibrowser approach |
|--------|-------------------|---------------------|
| `navigator.webdriver` | JS init-script (detectable) | `--disable-blink-features=AutomationControlled` (Blink-level) |
| TLS/JA3 | Bundled Chromium | Real installed Chrome |
| Runtime.enable on main world | Called by default | NEVER — isolated worlds only |
| Console.enable | Called by default | Off by default |
| Network.enable | Called by default | Off by default (opt-in debug) |

## miniproxy integration

miniproxy (at `/root/projects/miniproxy/`) provides HTTP/HTTPS interception via
mitmproxy. When Chrome is launched with `--proxy-server=http://127.0.0.1:8080`,
all traffic flows through miniproxy and is captured in SQLite. The CDP client
controls the DOM/JS layer while miniproxy captures the HTTP layer — combined
visibility for security testing.

## Where Playwright is replaced in Riciplay

| File | Playwright usage | Replacement |
|------|-----------------|-------------|
| `cli/riciplay_cli/tools/registry.py:356-520` | `tool_browse` (sync Playwright) | `Engine.fast_browse()` |
| `cli/riciplay_cli/investigation/tools/browser.py:15-106` | `browser_navigate/eval/screenshot` | `Engine.create_session()` |
| `backend/services/browser_automation.py:99-841` | `BrowserSession` (async Playwright) | `Engine.create_session()` (async) |
| `backend/services/dom_scanner.py:259-327` | Dead `scan_dom_xss` | Rewire or delete |
