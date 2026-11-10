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
│  (ws://9222) │  (ephemeral)   │   (opt-in capture)   │
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
| `engine.py` | Engine facade — serialized browser lifecycle, per-Session target ownership, fallback, persistence hooks |
| `cdp_client.py` | Raw WebSocket CDP JSON-RPC client (command/response, events, target creation/discovery/closure) |
| `lightpanda.py` | Lightpada engine: connects to ws://127.0.0.1:9222, CDP browse |
| `chrome_launcher.py` | Find + launch real installed Chrome with --remote-debugging-port |
| `session.py` | Session + Page abstractions: document-bound navigation/status/refs, public current snapshot, evaluate, screenshot, input |
| `stealth.py` | Chrome launch flags for naviagation.webdriver suppression (no JS injection) |
| `cookie_jar.py` | Persistent JSON cookie/localStorage jar with locked cross-process merge and deletion reconciliation |
| `network.py` | Opt-in, bounded request/response **metadata** capture (no response bodies) |
| `wait.py` | Auto-wait: network-idle + DOM-stability polling |
| `utils.py` | URL validation, HTML stripping, link extraction, CF detection, truncation |

## Anti-detection design

| Vector | Playwright approach | ricibrowser approach |
|--------|-------------------|---------------------|
| `navigator.webdriver` | JS init-script (detectable) | `--disable-blink-features=AutomationControlled` (Blink-level) |
| TLS/JA3 | Bundled Chromium | Real installed Chrome |
| Runtime.enable on main world | Called by default | NEVER — isolated worlds only |
| Console.enable | Called by default | Off by default |
| Network debug archival | Called by default | Off by default; navigation can still enable Network for status/cookies |

## Ownership and evidence boundaries

- An Engine owns at most one Chrome process/profile at a time. Startup and
  restart are serialized. Failed process cleanup retains `_chrome_proc` and
  `_temp_user_data_dir` for a retry.
- Every Chrome Session owns a unique page target and CDP socket. Sessions under
  one Engine still share Chrome's profile/cookies; target isolation is not
  browser-context identity isolation.
- Top-level document and same-document navigation events advance a document
  generation. Isolated contexts, accessibility references, and unverified HTTP
  status are invalidated at that boundary.
- `Session.snapshot_current_page()` exposes status source, errors/timeouts,
  document identity/generation, actual stealth mode, and capabilities without
  removing the legacy private `_capture_page()` entry point.
- Session close persists cookies/localStorage (including deletions), stops its
  capture, closes the socket, and retires the target. Engine close finalizes all
  tracked Sessions first.
- Network evidence is metadata-only. Stopped client buckets move into a bounded
  Engine archive; capabilities explicitly report response-body capture as false.

## miniproxy integration

`proxy_url` configures Chrome's `--proxy-server` route. A separately operated
miniproxy can capture traffic that actually traverses it, while ricibrowser
controls the DOM/JS layer. This repository does not perform miniproxy health
checks, read its SQLite database, import its cookies, or correlate proxy flow
IDs. Capabilities identify this accurately as `proxy_mode="route_only"`.

## Where Playwright is replaced in Riciplay

| File | Playwright usage | Replacement |
|------|-----------------|-------------|
| `cli/riciplay_cli/tools/registry.py:356-520` | `tool_browse` (sync Playwright) | `Engine.fast_browse()` |
| `cli/riciplay_cli/investigation/tools/browser.py:15-106` | `browser_navigate/eval/screenshot` | `Engine.create_session()` |
| `backend/services/browser_automation.py:99-841` | `BrowserSession` (async Playwright) | `Engine.create_session()` (async) |
| `backend/services/dom_scanner.py:259-327` | Dead `scan_dom_xss` | Rewire or delete |
