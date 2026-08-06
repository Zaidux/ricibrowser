"""Session and Page abstractions — shared interface for both engines.

A :class:`Session` wraps a CDP target and provides high-level methods
(navigate, evaluate, screenshot, click, fill, get_dom). Both the Lightpanda
fast path and the CDP-Chrome thorough path produce the same :class:`Session`
interface so callers can swap engines seamlessly.

A :class:`Page` is the immutable result of a browse/navigate operation.
"""

from __future__ import annotations

import asyncio as _aio
import base64
import json
import logging
import os
import tempfile
import time as _time
from dataclasses import dataclass, field
from typing import Any

from ricibrowser.cdp_client import CDPClient, CDPError
from ricibrowser.utils import detect_cloudflare, extract_links, strip_html, truncate, validate_url

logger = logging.getLogger(__name__)


@dataclass
class Page:
    """Result of a browse/navigate operation.

    Immutable snapshot of the page state after navigation.
    """

    url: str
    """The URL requested."""
    final_url: str
    """The URL after redirects (may differ from url)."""
    status_code: int
    """HTTP status code (0 if unknown)."""
    title: str
    """Page <title>."""
    text: str
    """Rendered body text (post-JS, HTML-stripped)."""
    html: str
    """Full rendered DOM HTML (post-JS)."""
    links: list[dict[str, str]] = field(default_factory=list)
    """Extracted links [{text, href}, ...]."""
    cookies: list[dict] = field(default_factory=list)
    """Cookies from the browser context."""
    truncated: bool = False
    """Whether text/html was truncated."""
    cloudflare_challenge: bool = False
    """Whether a Cloudflare/anti-bot challenge was detected."""
    cloudflare_type: str | None = None
    """Challenge type ('cloudflare', 'generic_captcha', or None)."""
    screenshot_path: str | None = None
    """Path to screenshot PNG (None if not taken)."""
    engine: str = "unknown"
    """Which engine produced this page ('lightpanda' or 'cdp_chrome')."""

    def to_dict(self) -> dict[str, Any]:
        """Convert to a dict matching the existing tool_browse return format."""
        return {
            "status": "ok",
            "tool": "browse",
            "url": self.final_url,
            "http_status": self.status_code,
            "title": self.title,
            "text": self.text,
            "html": self.html,
            "links": self.links,
            "link_count": len(self.links),
            "truncated": self.truncated,
            "cookies": self.cookies,
            "stealth": True,
            "anti_bot_detected": self.cloudflare_challenge,
            "anti_bot_type": self.cloudflare_type,
            "screenshot_path": self.screenshot_path,
            "engine": self.engine,
        }


class Session:
    """High-level browser session wrapping a CDP target.

    Provides navigate/evaluate/screenshot/click/fill methods. Maintains
    isolated-world JS execution (never calls Runtime.enable on the main world).
    """

    def __init__(self, cdp: CDPClient, engine_name: str = "cdp_chrome"):
        self._cdp = cdp
        self._engine_name = engine_name
        self._frame_id: str = ""
        self._isolated_context_id: int | None = None
        self._page_enabled = False
        self._current_url: str = ""
        # Register for frame navigation events so we invalidate the isolated
        # context when the frame changes (link clicks, SPA navigations, etc.).
        self._setup_frame_listener()

    def _setup_frame_listener(self) -> None:
        """Register a CDP event handler that invalidates the isolated context
        when the main frame navigates (even page-initiated navigations).

        Without this, the cached _isolated_context_id points at a destroyed
        execution context after a SPA navigation or link click, and
        evaluate() silently fails with a stale contextId error.

        Also tracks redirect URLs so _capture_page() can report the real
        final URL after an SSO redirect chain (e.g. ctf.hackthebox.com →
        account.hackthebox.com/login → ctf.hackthebox.com/callback).
        """
        def _on_frame_navigated(params: dict) -> None:
            frame = params.get("frame", {})
            # Only track the TOP-LEVEL frame. A page with iframes fires
            # Page.frameNavigated for each child frame too; adopting a child
            # frame id here meant _get_or_create_isolated_world later ran
            # against a subframe that could be torn down independently, giving
            # the recurring "No frame for given id found" (-32602) errors.
            if frame.get("parentId"):
                return
            new_frame_id = frame.get("id", "")
            new_url = frame.get("url", "")
            if new_frame_id and new_frame_id != self._frame_id:
                self._frame_id = new_frame_id
                self._isolated_context_id = None
                if new_url:
                    self._current_url = new_url
                logger.debug("Main frame navigated to %s, isolated context invalidated", new_url)

        self._cdp._event_handlers.setdefault("Page.frameNavigated", []).append(_on_frame_navigated)

    async def _ensure_page_enabled(self) -> None:
        """Enable the Page domain (needed for navigation events)."""
        if not self._page_enabled:
            try:
                await self._cdp.send("Page.enable")
                self._page_enabled = True
            except CDPError:
                pass

    async def _refresh_main_frame_id(self) -> str:
        """Look up the current top-level frame id via Page.getFrameTree.

        The cached ``self._frame_id`` (captured from Page.navigate) goes stale
        when the page swaps its main frame — cross-origin navigations, some SSO
        redirect chains, and provisional→committed frame transitions all mint a
        new frame id. Using the stale id in Page.createIsolatedWorld yields
        ``-32602: No frame for given id found``. We re-read the live tree and
        cache the real root frame id.
        """
        try:
            tree = await self._cdp.send("Page.getFrameTree")
            frame = (tree.get("frameTree") or {}).get("frame") or {}
            fid = frame.get("id", "")
            if fid:
                self._frame_id = fid
                url = frame.get("url")
                if url:
                    self._current_url = url
            return fid
        except CDPError as exc:
            logger.debug("Page.getFrameTree failed: %s", exc)
            return ""

    async def _get_or_create_isolated_world(self) -> int | None:
        """Get or create an isolated execution context for JS evaluation.

        Per the CDP spec: Page.createIsolatedWorld creates a new isolated
        world for the given frame. We NEVER call Runtime.enable on the main
        context — all JS evaluation goes through isolated worlds.

        If the cached frame id is stale (``No frame for given id found``), we
        re-resolve the live main frame from Page.getFrameTree once and retry,
        rather than logging a warning and returning None (which produced the
        recurring "Could not create isolated world" spam and empty captures
        while browsing sites that swap their main frame, e.g. login flows).
        """
        if self._isolated_context_id is not None:
            return self._isolated_context_id
        if not self._frame_id:
            # No frame yet — try to discover the live one.
            await self._refresh_main_frame_id()
            if not self._frame_id:
                return None

        async def _create(frame_id: str) -> int | None:
            result = await self._cdp.send("Page.createIsolatedWorld", {
                "frameId": frame_id,
                "worldName": "ricibrowser_isolated",
            })
            return result.get("executionContextId")

        try:
            self._isolated_context_id = await _create(self._frame_id)
            return self._isolated_context_id
        except CDPError as exc:
            # Stale frame id: re-resolve the live main frame and retry once.
            if "frame" in exc.message.lower():
                fresh = await self._refresh_main_frame_id()
                if fresh:
                    try:
                        self._isolated_context_id = await _create(fresh)
                        return self._isolated_context_id
                    except CDPError as exc2:
                        logger.debug("Isolated world retry failed: %s", exc2)
                        return None
            logger.debug("Could not create isolated world: %s", exc)
            return None

    async def navigate(self, url: str, wait_until: str = "load", max_chars: int = 10_000) -> Page:
        """Navigate to a URL and wait for the page to settle.

        Args:
            url: The URL to navigate to.
            wait_until: When to consider navigation complete.
                "load" — wait for the load event.
                "domcontentloaded" — wait for DOMContentLoaded.
                "networkidle" — wait for network to be idle (requires Network.enabled).

        Returns:
            A Page with the current state.
        """
        url = validate_url(url)
        await self._ensure_page_enabled()

        # Enable Network domain — needed for cookie capture across redirects
        # and for Network.getCookies to return cookies set during the redirect
        # chain (SSO flows: Set-Cookie on 302 responses to intermediate domains).
        try:
            await self._cdp.send("Network.enable")
        except CDPError:
            pass

        # ── Robust navigation gating (fixes flaky / empty page loads) ──
        # The previous implementation sent Page.navigate then immediately polled
        # document.readyState. That races the *old* document: a freshly-created
        # tab (about:blank) or a prior fully-loaded page answers readyState
        # 'complete' before Chrome commits the new navigation, so we captured a
        # blank/stale page. We now register a load-event waiter BEFORE issuing
        # Page.navigate, so the event can't fire in the gap between the command
        # and the subscription, then await the real load signal.
        nav_timeout = getattr(self, "_nav_timeout", 30.0)
        loop = _aio.get_event_loop()
        load_future: "_aio.Future[dict]" = loop.create_future()
        expected_frame: dict[str, str] = {"id": ""}

        def _on_load(params: dict) -> None:
            if not load_future.done():
                load_future.set_result(params)

        def _on_frame_stopped(params: dict) -> None:
            fid = params.get("frameId", "")
            if not load_future.done() and (not expected_frame["id"] or fid == expected_frame["id"]):
                load_future.set_result(params)

        self._cdp._event_handlers.setdefault("Page.loadEventFired", []).append(_on_load)
        self._cdp._event_handlers.setdefault("Page.frameStoppedLoading", []).append(_on_frame_stopped)

        try:
            result = await self._cdp.send("Page.navigate", {"url": url})
            # Page.navigate reports hard navigation failures via errorText
            # (net::ERR_NAME_NOT_RESOLVED, ERR_CONNECTION_REFUSED, etc.). A
            # blank page from a failed load is a real error, not a slow render.
            error_text = result.get("errorText")
            self._frame_id = result.get("frameId", "")
            expected_frame["id"] = self._frame_id
            self._current_url = url

            # Reset the isolated world (frame changed)
            self._isolated_context_id = None

            if error_text and error_text not in ("net::ERR_ABORTED",):
                # ERR_ABORTED is benign (e.g. a download or a client redirect
                # superseding the navigation); anything else is a real failure.
                logger.warning("Navigation to %s failed: %s", url, error_text)
                page = await self._capture_page(url, max_chars=max_chars)
                page.status_code = 0
                return page

            # Wait for the real load event (bounded). This replaces racing
            # readyState against a stale document.
            try:
                await _aio.wait_for(load_future, timeout=nav_timeout)
            except _aio.TimeoutError:
                logger.debug("load event not observed within %.1fs; falling back to poll", nav_timeout)
        finally:
            for name, cb in (("Page.loadEventFired", _on_load),
                             ("Page.frameStoppedLoading", _on_frame_stopped)):
                handlers = self._cdp._event_handlers.get(name)
                if handlers and cb in handlers:
                    handlers.remove(cb)

        # Supplementary content-stability wait (DOM settle for SPAs). This now
        # runs AFTER the new document has actually committed + loaded, so the
        # readyState it polls belongs to the target page, not the old one.
        # For plain "load" mode we already awaited the real Page.loadEventFired
        # above, so document.readyState is already "complete" — skip the
        # redundant readyState re-poll (saves a Runtime.evaluate round-trip per
        # navigation). DOM-stability / networkidle modes still run in full.
        from ricibrowser.wait import wait_for_content_stable, _wait_dom_stable, _wait_network_idle
        if wait_until == "load":
            pass  # load event already observed
        elif wait_until == "domcontentloaded":
            pass  # load event implies interactive
        elif wait_until == "domstable":
            await _wait_dom_stable(self._cdp, timeout=10.0)
        elif wait_until == "networkidle":
            await _wait_dom_stable(self._cdp, timeout=10.0)
            await _wait_network_idle(self._cdp, timeout=5.0)
        else:
            await wait_for_content_stable(self._cdp, self._frame_id, mode=wait_until)

        # ── URL stability: handle JS-based SSO redirects ────────────
        # Some auth flows (OAuth, SAML, SSO) fire a JS redirect AFTER the
        # initial page load completes (window.location, form auto-submit,
        # meta-refresh). wait_for_content_stable finishes at readyState==complete
        # on the intermediate page. We poll location.href to catch the final
        # URL once the redirect settles.
        #
        # Fast path: most navigations do NOT redirect post-load. We take one
        # immediate reading, and only enter the polling loop if the URL differs
        # from the requested URL (a redirect actually happened). This removes
        # the fixed ~1s two-poll tax that every navigation used to pay.
        last_url = ""
        stability_timeout = getattr(self, "_url_stability_timeout", 8.0)
        try:
            first = await self.evaluate("location.href")
        except Exception:
            first = None
        if first:
            self._current_url = first
        # Only poll for redirect settling if the landing URL diverged from the
        # requested one (ignoring a trailing-slash / fragment difference).
        def _norm(u: str) -> str:
            return (u or "").split("#", 1)[0].rstrip("/")

        if first and _norm(first) != _norm(url) and stability_timeout > 0:
            last_url = first
            deadline = _time.monotonic() + stability_timeout
            while _time.monotonic() < deadline:
                await _aio.sleep(0.25)
                try:
                    cur = await self.evaluate("location.href")
                    if cur and cur == last_url:
                        self._current_url = cur
                        break
                    last_url = cur or last_url
                except Exception:
                    pass

        return await self._capture_page(url, max_chars=max_chars, known_final_url=first or None)

    async def _capture_page(self, url: str, max_chars: int = 10_000,
                            known_final_url: str | None = None) -> Page:
        """Capture the current page state into a Page object.

        A single Runtime.evaluate returns title/html/text/url together so the
        capture costs ONE CDP round-trip rather than four. ``known_final_url``
        lets the caller skip re-reading location.href when it already polled it
        during URL-stability settling.
        """
        snapshot = await self.evaluate_value("""({
            title: document.title || '',
            html: document.documentElement ? document.documentElement.outerHTML : '',
            text: document.body ? document.body.innerText : '',
            url: location.href
        })""")
        snapshot = snapshot if isinstance(snapshot, dict) else {}
        title = str(snapshot.get("title", ""))
        html = str(snapshot.get("html", ""))
        text = str(snapshot.get("text", ""))
        final_url = str(snapshot.get("url", "") or known_final_url or self._current_url)

        if not text and html:
            text = strip_html(html)

        links = extract_links(html, url)

        # Get HTTP status (not always available via CDP)
        status_code = 0

        # Get cookies
        cookies = await self.get_cookies()

        # Detect Cloudflare
        is_cf, cf_type = detect_cloudflare(html, title)

        # Truncate
        truncated_text, text_truncated = truncate(text, max_chars)
        truncated_html, html_truncated = truncate(html, max(max_chars * 4, 20_000))

        return Page(
            url=url,
            final_url=final_url,
            status_code=status_code,
            title=title,
            text=truncated_text,
            html=truncated_html,
            links=links,
            cookies=cookies,
            truncated=text_truncated or html_truncated,
            cloudflare_challenge=is_cf,
            cloudflare_type=cf_type,
            engine=self._engine_name,
        )

    async def evaluate(self, expression: str) -> str | None:
        """Evaluate JavaScript in an isolated world and return the result.

        NEVER calls Runtime.enable on the main world — uses
        Page.createIsolatedWorld to create a separate execution context.
        If the isolated context is unavailable (before first navigate, or
        after an unobserved frame change), returns None with a warning
        rather than silently falling back to the main world.
        """
        value = await self.evaluate_value(expression)
        return str(value) if value is not None else None

    async def evaluate_value(self, expression: str) -> Any:
        """Evaluate in the isolated world and preserve JSON-compatible types.

        Falls back to the default execution context when the isolated world is
        unavailable — this can happen during redirects, before the first
        navigation, or when ``Page.createIsolatedWorld`` is not supported by
        the browser engine (Lightpanda).  Without this fallback,
        ``_capture_page`` receives an empty ``{}`` snapshot and the page
        appears blank.
        """
        context_id: int | None = None
        try:
            context_id = await self._get_or_create_isolated_world()
        except Exception as exc:
            logger.debug("Could not create isolated world (will use default context): %s", exc)
        params: dict[str, Any] = {
            "expression": expression,
            "returnByValue": True,
        }
        if context_id is not None:
            params["contextId"] = context_id

        try:
            result = await self._cdp.send("Runtime.evaluate", params)
            return result.get("result", {}).get("value")
        except CDPError as exc:
            # If the contextId was stale (frame changed underneath us), try a
            # fresh isolated world.  If that also fails, fall back to the
            # default execution context — a blank page is worse than a
            # detectable evaluation.
            if "context" in exc.message.lower() and context_id is not None:
                self._isolated_context_id = None
                try:
                    retry_context = await self._get_or_create_isolated_world()
                except Exception:
                    retry_context = None
                if retry_context is not None:
                    params["contextId"] = retry_context
                else:
                    params.pop("contextId", None)
                try:
                    result = await self._cdp.send("Runtime.evaluate", params)
                    return result.get("result", {}).get("value")
                except CDPError:
                    pass
            elif context_id is not None:
                # Non-context error: try without isolated context as fallback.
                params.pop("contextId", None)
                try:
                    result = await self._cdp.send("Runtime.evaluate", params)
                    return result.get("result", {}).get("value")
                except CDPError:
                    pass
            logger.warning("JS evaluation failed: %s", exc)
            return None

    async def evaluate_bool(self, expression: str) -> bool | None:
        """Evaluate a JS expression that returns a boolean.

        Returns True/False, or None if evaluation failed or returned
        a non-boolean value. Uses evaluate_value to preserve the native
        Python bool type returned by CDP Runtime.evaluate (avoids the
        string "true" vs Python bool True confusion).
        """
        value = await self.evaluate_value(expression)
        if isinstance(value, bool):
            return value
        if value is True or value is False:
            return value
        return None

    async def screenshot(self, path: str | None = None, full_page: bool = False) -> str:
        """Take a screenshot and save to a PNG file.

        Note: screenshots require a rendering engine. Lightpanda does NOT
        support this — the caller should use CDPChromeEngine for screenshots.

        Returns the path to the saved PNG.
        """
        if path is None:
            fd, path = tempfile.mkstemp(suffix=".png", prefix="ricibrowser_")
            os.close(fd)

        params: dict[str, Any] = {"format": "png"}
        if full_page:
            params["captureBeyondViewport"] = True

        try:
            result = await self._cdp.send("Page.captureScreenshot", params)
            data_b64 = result.get("data", "")
            if data_b64:
                with open(path, "wb") as f:
                    f.write(base64.b64decode(data_b64))
                return path
        except CDPError as exc:
            logger.warning("Screenshot failed: %s", exc)

        return path

    # ── Element resolution ────────────────────────────────────────────
    #
    # Injected into every element-targeting evaluation. Resolves a target
    # string against the DOM using progressively looser strategies, because
    # component frameworks routinely render inputs with no stable CSS hook —
    # no name, no semantic id, just a label sitting next to a nested <input>.
    #
    # Order matters: exact CSS first so an explicit selector always wins and
    # callers keep full control; the heuristics only run once CSS finds
    # nothing. Shadow roots are pierced because CSS selectors cannot cross
    # that boundary.
    _RESOLVER_JS = """
    function __rb_resolve(target) {
        function deepQuery(root, sel) {
            try {
                var hit = root.querySelector(sel);
                if (hit) return hit;
            } catch (e) { return null; }
            var walker = root.querySelectorAll('*');
            for (var i = 0; i < walker.length; i++) {
                if (walker[i].shadowRoot) {
                    var deep = deepQuery(walker[i].shadowRoot, sel);
                    if (deep) return deep;
                }
            }
            return null;
        }

        function allWithShadow(sel) {
            var found = [];
            function collect(root) {
                try {
                    var hits = root.querySelectorAll(sel);
                    for (var i = 0; i < hits.length; i++) found.push(hits[i]);
                } catch (e) {}
                var walker = root.querySelectorAll('*');
                for (var j = 0; j < walker.length; j++) {
                    if (walker[j].shadowRoot) collect(walker[j].shadowRoot);
                }
            }
            collect(document);
            return found;
        }

        // 1. Treat it as a CSS selector (including inside shadow roots).
        var el = deepQuery(document, target);
        if (el) return el;

        var needle = String(target).trim().toLowerCase();
        var fields = allWithShadow('input, textarea, select, [contenteditable="true"]');
        var visible = function (node) {
            if (!node) return false;
            var r = node.getBoundingClientRect();
            if (!r.width && !r.height) return false;
            var s = window.getComputedStyle(node);
            return s.visibility !== 'hidden' && s.display !== 'none';
        };
        var match = function (text) {
            if (!text) return false;
            text = String(text).trim().toLowerCase();
            return text === needle || (needle.length > 2 && text.indexOf(needle) !== -1);
        };

        // 2. A <label> whose text matches — the usual framework pattern.
        //    Now scans shadow roots so a label inside a web component is found.
        var labels = allWithShadow('label');
        for (var i = 0; i < labels.length; i++) {
            if (!match(labels[i].textContent)) continue;
            var forId = labels[i].getAttribute('for');
            if (forId) {
                // An explicit `for` is an unambiguous statement of intent. If
                // it dangles, STOP rather than falling through to the sibling
                // heuristics below — those would return some unrelated nearby
                // input, and silently filling the wrong field is worse than
                // failing. getElementById can't pierce shadow; deepQuery can.
                return deepQuery(document, '#' + CSS.escape(forId));
            }
            // Label wrapping its control, no `for` attribute.
            var nested = labels[i].querySelector('input, textarea, select');
            if (nested) return nested;
            // Label as a visual sibling of the field's container.
            var sib = labels[i].parentElement
                ? labels[i].parentElement.querySelector('input, textarea, select')
                : null;
            if (sib) return sib;
        }

        // 3. aria-label / aria-labelledby / placeholder / name / id.
        //    Now uses the shadow-aware field list from allWithShadow above.
        for (var j = 0; j < fields.length; j++) {
            var f = fields[j];
            if (!visible(f)) continue;
            if (match(f.getAttribute('aria-label'))) return f;
            if (match(f.getAttribute('placeholder'))) return f;
            if (match(f.getAttribute('name'))) return f;
            if (match(f.getAttribute('id'))) return f;
            var labelledBy = f.getAttribute('aria-labelledby');
            if (labelledBy) {
                var ref = deepQuery(document, '#' + CSS.escape(labelledBy));
                if (ref && match(ref.textContent)) return f;
            }
        }
        return null;
    }
    """

    # Sets a value the way a real user would, so framework-controlled inputs
    # actually register the change.
    #
    # React (and Vue/Svelte to a lesser degree) install a `_valueTracker` on
    # the node and read the value through a native prototype setter. Assigning
    # `el.value` directly updates the tracker's cached copy, so the synthetic
    # onChange sees "no change" and drops the edit — the field looks filled but
    # submits empty. Clearing the tracker first and going through the native
    # setter makes the change visible.
    #
    # Note: `_valueTracker` is an expando and expandos are per-execution-world,
    # so from an isolated world it reads as undefined. That is fine and
    # deliberate — the guard below skips the reset, and the native setter alone
    # still triggers the framework listener because *events* cross worlds even
    # though properties do not. Verified against React 18.
    _SETTER_JS = """
    function __rb_setValue(el, value) {
        var proto = Object.getPrototypeOf(el);
        var desc = Object.getOwnPropertyDescriptor(proto, 'value')
                || Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value');
        if (el.isContentEditable) {
            el.focus();
            el.textContent = value;
            el.dispatchEvent(new Event('input', {bubbles: true}));
            return true;
        }
        if (el._valueTracker && typeof el._valueTracker.setValue === 'function') {
            el._valueTracker.setValue('');
        }
        if (desc && desc.set) {
            desc.set.call(el, value);
        } else {
            el.value = value;
        }
        el.dispatchEvent(new Event('input', {bubbles: true}));
        el.dispatchEvent(new Event('change', {bubbles: true}));
        return true;
    }
    """

    async def wait_for_selector(self, selector: str, timeout: float = 5.0) -> bool:
        """Poll until *selector* resolves to an element, or *timeout* elapses.

        Frameworks mount asynchronously, so a selector that is absent on the
        first query is usually just late rather than wrong. Callers that skip
        this see a spurious "element not found" on every client-rendered page.
        """
        deadline = _time.monotonic() + timeout
        expr = self._RESOLVER_JS + f"\n__rb_resolve({json.dumps(selector)}) !== null"
        while True:
            if await self.evaluate_bool(f"(function() {{ {expr} }})()") is True:
                return True
            if _time.monotonic() >= deadline:
                return False
            await _aio.sleep(0.25)

    async def click(self, selector: str, timeout: float = 5.0, wait_for_navigation: bool = True) -> bool:
        """Click an element matching a CSS selector or a label/placeholder.

        Waits up to *timeout* seconds for the target to appear before giving
        up. If *wait_for_navigation* is True (default), polls location.href
        after the click to detect SSO/redirect chains and waits for URL
        stability. Returns True if the click succeeded.
        """
        if not await self.wait_for_selector(selector, timeout):
            logger.warning("click: %r did not resolve within %.1fs", selector, timeout)
            return False

        url_before = ""
        if wait_for_navigation:
            try:
                url_before = await self.evaluate("location.href") or ""
            except Exception:
                pass

        js = f"""
        (function() {{
            {self._RESOLVER_JS}
            var el = __rb_resolve({json.dumps(selector)});
            if (!el) return false;
            if (typeof el.scrollIntoView === 'function') {{
                el.scrollIntoView({{block: 'center', inline: 'center'}});
            }}
            el.click();
            return true;
        }})()
        """
        ok = await self.evaluate_bool(js) is True
        if ok and wait_for_navigation and url_before:
            await self._wait_for_url_stability(url_before)
        return ok

    async def fill(self, selector: str, value: str, timeout: float = 5.0) -> bool:
        """Fill an input, textarea, select or contenteditable with *value*.

        *selector* may be a CSS selector or a human-readable handle such as a
        label, placeholder or aria-label. Waits up to *timeout* for the target
        to mount, then sets the value in a way framework-controlled inputs
        register (see ``_SETTER_JS``). Returns True if the fill succeeded.
        """
        if not await self.wait_for_selector(selector, timeout):
            logger.warning("fill: %r did not resolve within %.1fs", selector, timeout)
            return False

        js = f"""
        (function() {{
            {self._RESOLVER_JS}
            {self._SETTER_JS}
            var el = __rb_resolve({json.dumps(selector)});
            if (!el) return false;
            if (typeof el.scrollIntoView === 'function') {{
                el.scrollIntoView({{block: 'center', inline: 'center'}});
            }}
            if (typeof el.focus === 'function') el.focus();
            return __rb_setValue(el, {json.dumps(value)});
        }})()
        """
        if await self.evaluate_bool(js) is not True:
            return False

        # Verify the value actually stuck. A framework that rejects or
        # reformats the input (masked fields, controlled components with
        # validation) leaves the DOM value different from what we wrote —
        # reporting success there would hide the failure from the caller.
        verify = f"""
        (function() {{
            {self._RESOLVER_JS}
            var el = __rb_resolve({json.dumps(selector)});
            if (!el) return false;
            var actual = el.isContentEditable ? el.textContent : el.value;
            return actual === {json.dumps(value)};
        }})()
        """
        if await self.evaluate_bool(verify) is not True:
            logger.warning(
                "fill: %r did not retain the value (controlled/masked input?)", selector
            )
            return False
        return True

    async def _wait_for_url_stability(self, url_before: str) -> None:
        """Poll location.href until the URL stops changing or times out.

        Used after click() to detect SSO/redirect chains triggered by form
        submissions or link clicks. If the URL changed, waits for it to
        stabilise (same URL on two consecutive polls, up to 8s default).
        """
        stability_timeout = getattr(self, "_url_stability_timeout", 8.0)
        last_url = ""
        deadline = _time.monotonic() + stability_timeout
        polled_once = False
        while _time.monotonic() < deadline:
            try:
                cur = await self.evaluate("location.href")
                if cur is None:
                    continue
                polled_once = True
                if cur == last_url:
                    if cur and cur != url_before:
                        logger.debug("URL stabilised after click: %s", cur)
                        self._current_url = cur
                    return
                last_url = cur
            except Exception:
                if polled_once:
                    return
                pass
            await _aio.sleep(0.5)

    async def get_dom(self) -> str:
        """Return the full rendered DOM HTML."""
        return await self.evaluate("document.documentElement.outerHTML") or ""

    async def get_cookies(self) -> list[dict]:
        """Get all cookies from the browser context."""
        try:
            result = await self._cdp.send("Network.getCookies")
            return result.get("cookies", [])
        except CDPError:
            return []

    async def set_cookies(self, cookies: list[dict]) -> None:
        """Set cookies in the browser context."""
        try:
            await self._cdp.send("Network.setCookies", {"cookies": cookies})
        except CDPError as exc:
            logger.warning("set_cookies failed: %s", exc)

    async def close(self) -> None:
        """Close the session and its CDP connection."""
        if self._cdp and not self._cdp.is_closed:
            await self._cdp.close()
