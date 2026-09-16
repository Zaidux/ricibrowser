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
import hashlib
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
    accessibility_snapshot: dict[str, Any] | None = None
    """Hybrid accessibility/DOM snapshot when requested."""

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
            "accessibility_snapshot": self.accessibility_snapshot,
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
        self._last_status_code: int = 0
        self._navigation_id: int = 0
        self._snapshot_id: str = ""
        self._snapshot_refs: dict[str, dict[str, Any]] = {}
        # Last JS evaluation exception (None when the last evaluate succeeded).
        # Lets callers distinguish "expression threw" from "returned undefined".
        self._last_eval_error: str | None = None
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
                self._snapshot_id = ""
                self._snapshot_refs = {}
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
                # Page.enable is required for navigation/load events. The old
                # code swallowed every error and continued, causing a second
                # doomed command and a multi-minute outer timeout. Optional
                # domains (Network, extensions) may degrade; Page may not.
                raise

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
        # Stale status from a previous navigation must not leak into this one.
        self._last_status_code = 0

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

        # ── HTTP status capture ────────────────────────────────────────
        # CDP exposes no "give me the current page's status code" call, so the
        # only way to report a real status is to observe the document response
        # as it arrives. We collect every ``type == "Document"`` response and
        # pick the one belonging to the main frame afterwards. Registering the
        # handler BEFORE Page.navigate matters: the response can land before the
        # navigate command's own reply, and a late subscription would miss it.
        doc_responses: list[dict[str, Any]] = []

        def _on_response(params: dict) -> None:
            if params.get("type") != "Document":
                return
            response = params.get("response") or {}
            doc_responses.append({
                "frameId": params.get("frameId", ""),
                "status": int(response.get("status") or 0),
                "url": response.get("url", ""),
            })

        def _on_load(params: dict) -> None:
            if not load_future.done():
                load_future.set_result(params)

        def _on_frame_stopped(params: dict) -> None:
            fid = params.get("frameId", "")
            if not load_future.done() and (not expected_frame["id"] or fid == expected_frame["id"]):
                load_future.set_result(params)

        self._cdp._event_handlers.setdefault("Page.loadEventFired", []).append(_on_load)
        self._cdp._event_handlers.setdefault("Page.frameStoppedLoading", []).append(_on_frame_stopped)
        self._cdp._event_handlers.setdefault("Network.responseReceived", []).append(_on_response)

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
                             ("Page.frameStoppedLoading", _on_frame_stopped),
                             ("Network.responseReceived", _on_response)):
                handlers = self._cdp._event_handlers.get(name)
                if handlers and cb in handlers:
                    handlers.remove(cb)

        # Resolve the main-frame document status from the collected responses.
        # Prefer an exact frameId match; the frame id can be replaced mid-flight
        # (cross-origin redirect chains mint a new one), so fall back to the last
        # document response seen, which is the final hop of the chain.
        self._last_status_code = self._resolve_status(doc_responses)

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

    def _resolve_status(self, doc_responses: list[dict[str, Any]]) -> int:
        """Pick the main-frame document status from observed responses.

        ``Network.responseReceived`` fires for every response, including
        subframes and each hop of a redirect chain. The status we want is the
        one for the top-level document that actually committed:

        1. The last response whose ``frameId`` matches the main frame — a
           redirect chain reports 301/302 hops on the same frame, so the last
           one is the final document.
        2. Otherwise the last document response seen at all. Cross-origin
           navigations can replace the main frame id mid-flight, leaving no
           exact match; the final hop is still the right answer.
        """
        if not doc_responses:
            return 0
        for entry in reversed(doc_responses):
            if self._frame_id and entry.get("frameId") == self._frame_id:
                return int(entry.get("status") or 0)
        return int(doc_responses[-1].get("status") or 0)

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

        # Relative hrefs resolve against the document that actually rendered
        # them, which after a redirect is final_url — not the requested url.
        # Using the original url produced links pointing at the wrong host
        # whenever a navigation redirected (SSO flows, http→https, /→/en/).
        links = extract_links(html, final_url or url)

        # HTTP status observed via Network.responseReceived during navigate().
        # Stays 0 when the page was reached some other way (direct capture,
        # engines without the Network domain).
        status_code = self._last_status_code

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

    async def evaluate(self, expression: str) -> Any:
        """Evaluate JavaScript in an isolated world and return the result.

        NEVER calls Runtime.enable on the main world — uses
        Page.createIsolatedWorld to create a separate execution context.
        If the isolated context is unavailable (before first navigate, or
        after an unobserved frame change), returns None with a warning
        rather than silently falling back to the main world.
        """
        return await self.evaluate_value(expression)

    @property
    def last_eval_error(self) -> str | None:
        """The exception text from the most recent evaluate, if it threw."""
        return self._last_eval_error

    async def capture_storage(self) -> tuple[str, dict[str, str]]:
        result = await self.evaluate_value("""({
          origin: location.origin,
          storage: Object.fromEntries(Object.entries(localStorage))
        })""")
        if not isinstance(result, dict):
            return "", {}
        origin = str(result.get("origin") or "")
        storage = result.get("storage")
        return origin, dict(storage) if isinstance(storage, dict) else {}

    async def restore_storage(self, storage: dict[str, str]) -> None:
        if not storage:
            return
        payload = json.dumps(storage)
        await self.evaluate_value(
            f"Object.entries({payload}).forEach(([k,v]) => localStorage.setItem(k, v)); true"
        )

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
            # Await promise-returning expressions so `fetch(...).then(...)`
            # resolves to the final value instead of an opaque `{}`. Non-promise
            # expressions are unaffected.
            "awaitPromise": True,
        }
        if context_id is not None:
            params["contextId"] = context_id

        try:
            result = await self._cdp.send("Runtime.evaluate", params)
            # Surface JS exceptions: CDP returns exceptionDetails alongside a
            # null value when the expression throws. Swallowing it made every
            # JS error an unexplained None — the agent's top browse struggle.
            exception = result.get("exceptionDetails")
            if exception:
                details = exception.get("exception") or {}
                text = str(
                    details.get("description")
                    or details.get("value")
                    or exception.get("text")
                    or "JavaScript evaluation threw"
                )[:500]
                self._last_eval_error = text
                return None
            self._last_eval_error = None
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

    async def screenshot(self, path: str | None = None, full_page: bool = False) -> str | None:
        """Take a screenshot and save to a PNG file.

        Note: screenshots require a rendering engine. Lightpanda does NOT
        support this — the caller should use CDPChromeEngine for screenshots.

        Returns the path to the saved PNG, or ``None`` if the capture failed.
        A failed capture leaves no file behind: the empty temp file we created
        up front is removed, because a 0-byte PNG masquerading as a successful
        screenshot is worse than an explicit failure.
        """
        created_temp = False
        if path is None:
            fd, path = tempfile.mkstemp(suffix=".png", prefix="ricibrowser_")
            os.close(fd)
            created_temp = True

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
            logger.warning("Screenshot returned no data")
        except CDPError as exc:
            logger.warning("Screenshot failed: %s", exc)

        if created_temp:
            try:
                os.unlink(path)
            except OSError:
                pass
        return None

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

        // 4. Visible-text match on actionable elements.
        //    "click Continue with Email" is the most natural instruction an
        //    agent can write, but steps 1-3 only ever resolve FORM FIELDS —
        //    so every button/link click by its label failed, and agents
        //    burned rounds hunting for CSS selectors that didn't exist
        //    (field session 4a2ac15c). Match buttons, links, tabs, menu
        //    items and ARIA button/checkbox roles by their visible text.
        //    Exact beats substring; the smallest match wins so a
        //    page-wrapping <div> cannot shadow the real control; genuine
        //    interactive tags get a bonus over role-carrying containers.
        var ACTIONABLE = 'button, a, summary, [role="button"], [role="link"], '
            + '[role="tab"], [role="menuitem"], [role="option"], [role="treeitem"], '
            + '[role="switch"], [role="checkbox"], [role="radio"], '
            + 'input[type="submit"], input[type="button"], input[type="reset"]';
        var actionable = allWithShadow(ACTIONABLE);
        var best = null, bestScore = -Infinity;
        for (var k = 0; k < actionable.length; k++) {
            var cand = actionable[k];
            if (!visible(cand)) continue;
            if (cand.disabled || cand.getAttribute('aria-disabled') === 'true') continue;
            var label;
            if ((cand.tagName || '').toLowerCase() === 'input') {
                label = cand.value || cand.getAttribute('aria-label') || '';
            } else {
                label = cand.getAttribute('aria-label')
                     || cand.innerText || cand.textContent || '';
            }
            label = String(label).trim();
            if (!label) continue;
            var low = label.toLowerCase();
            var exact = low === needle;
            if (!exact && !(needle.length > 2 && low.indexOf(needle) !== -1)) continue;
            var score = (exact ? 100000 : 0) - label.length;
            var tag = (cand.tagName || '').toLowerCase();
            if (tag === 'button' || tag === 'a' || tag === 'summary') score += 10000;
            if (cand.getAttribute('role')) score += 1000;
            if (score > bestScore) { bestScore = score; best = cand; }
        }
        if (best) return best;

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
        // Refuse obviously non-fillable targets. The resolver now also
        // matches buttons/links by visible text (for click), so a fill
        // aimed at a button label would otherwise write .value, verify
        // .value, and report a false success.
        var _tag = (el.tagName || '').toLowerCase();
        var _type = String((el.getAttribute && el.getAttribute('type')) || '').toLowerCase();
        var _fillable = el.isContentEditable
            || _tag === 'textarea' || _tag === 'select'
            || (_tag === 'input'
                && _type !== 'submit' && _type !== 'button'
                && _type !== 'reset' && _type !== 'image' && _type !== 'file');
        if (!_fillable) return false;
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
        expr = self._RESOLVER_JS + f"\nreturn __rb_resolve({json.dumps(selector)}) !== null"
        while True:
            if await self.evaluate_bool(f"(function() {{ {expr} }})()") is True:
                return True
            if _time.monotonic() >= deadline:
                return False
            await _aio.sleep(0.25)

    async def click(self, selector: str, timeout: float = 5.0, wait_for_navigation: bool = True) -> bool:
        """Click an element matching a CSS selector or a label/placeholder.

        Waits up to *timeout* seconds for the target to appear before giving
        up. Dispatches a **trusted** CDP mouse event at the element's center
        (React/Vue routers ignore untrusted ``el.click()`` dispatches) and
        falls back to a synthetic click when coordinates or input dispatch
        are unavailable. If *wait_for_navigation* is True (default), polls
        location.href after the click to detect SSO/redirect chains and waits
        for URL stability. Returns True if the click succeeded.
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

        rect_js = f"""
        (function() {{
            {self._RESOLVER_JS}
            var el = __rb_resolve({json.dumps(selector)});
            if (!el) return null;
            if (typeof el.scrollIntoView === 'function') {{
                el.scrollIntoView({{block: 'center', inline: 'center'}});
            }}
            var r = el.getBoundingClientRect();
            if (r.width <= 0 || r.height <= 0) return null;
            return {{x: r.left + r.width / 2, y: r.top + r.height / 2}};
        }})()
        """
        clicked = False
        try:
            coords = await self.evaluate_value(rect_js)
        except Exception:
            coords = None
        if isinstance(coords, dict) and isinstance(coords.get("x"), (int, float)):
            clicked = await self._dispatch_mouse_click(
                float(coords["x"]), float(coords["y"]),
            )
        if not clicked:
            # Fallback: synthetic DOM click (untrusted). Some frameworks
            # accept it; a hidden/zero-size element only works this way.
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
            clicked = await self.evaluate_bool(js) is True
        if clicked and wait_for_navigation and url_before:
            await self._wait_for_url_stability(url_before)
        return clicked

    async def _dispatch_mouse_click(self, x: float, y: float) -> bool:
        """Dispatch a trusted (CDP Input) left-click at viewport coordinates."""
        base = {"button": "left", "clickCount": 1, "x": x, "y": y}
        try:
            await self._cdp.send("Input.dispatchMouseEvent", {**base, "type": "mousePressed"})
            await self._cdp.send("Input.dispatchMouseEvent", {**base, "type": "mouseReleased"})
            return True
        except Exception as exc:
            logger.debug("Trusted mouse dispatch unavailable (%s); using synthetic click", exc)
            return False

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

    # ── Form-state harvest / restore ─────────────────────────────────
    #
    # Long interact sequences die mid-form (timeouts, CDP hiccups, hard
    # navigations) and the filled values die with the page. These two
    # methods make that state capturable and replayable: harvest reads
    # every programmatically re-fillable field on the page in ONE
    # evaluate round-trip; restore re-applies a field list through the
    # same fill machinery (label resolver + framework event dispatch +
    # verification) the caller used originally.

    _FORM_HARVEST_JS = """
    (function() {
        function selFor(el) {
            try {
                if (el.id) {
                    var byId = '#' + CSS.escape(el.id);
                    if (document.querySelectorAll(byId).length === 1) return byId;
                }
                var n = el.getAttribute('name');
                if (n) {
                    var byName = el.tagName.toLowerCase() + '[name=' + JSON.stringify(n) + ']';
                    if (document.querySelectorAll(byName).length === 1) return byName;
                }
                // Uniqueness matters as much here as for id/name: two fields
                // sharing a placeholder would produce identical selectors and
                // silently restore into the first one twice.
                var al = el.getAttribute('aria-label');
                if (al) {
                    var byAria = '[aria-label=' + JSON.stringify(al) + ']';
                    if (document.querySelectorAll(byAria).length === 1) return byAria;
                }
                var ph = el.getAttribute('placeholder');
                if (ph) {
                    var byPh = '[placeholder=' + JSON.stringify(ph) + ']';
                    if (document.querySelectorAll(byPh).length === 1) return byPh;
                }
            } catch (e) { return null; }
            return null;  // not reliably re-targetable
        }
        var out = [];
        var els = document.querySelectorAll(
            'input, textarea, select, [contenteditable="true"]');
        for (var i = 0; i < els.length && out.length < 60; i++) {
            var el = els[i];
            var tag = el.tagName.toLowerCase();
            var type = (el.getAttribute('type') || (tag === 'input' ? 'text' : tag)).toLowerCase();
            if (type === 'file' || type === 'hidden' || el.disabled) continue;
            // Button-ish inputs carry labels, not data — restoring them is
            // meaningless noise (and clicking them is what got us here).
            if (type === 'submit' || type === 'reset' || type === 'button' || type === 'image') continue;
            var s = selFor(el);
            if (!s) continue;
            var rec = {selector: s, tag: tag, type: type};
            if (type === 'checkbox' || type === 'radio') {
                rec.checked = !!el.checked;
                if (type === 'radio' && !el.checked) continue;  // only the chosen one matters
            } else if (tag === 'select' && el.multiple) {
                rec.multiple = true;
                rec.value = JSON.stringify(
                    Array.prototype.map.call(el.selectedOptions, function(o) { return o.value; })
                );
            } else {
                rec.value = el.isContentEditable ? (el.textContent || '') : String(el.value != null ? el.value : '');
            }
            out.push(rec);
        }
        return out;
    })()
    """

    async def snapshot_form_state(self) -> list[dict[str, Any]]:
        """Harvest all re-fillable form fields on the current page.

        Returns a list of ``{selector, value|checked, type, tag, multiple?}``
        records suitable for :meth:`restore_form_state`. Fields that cannot
        be re-targeted reliably (no unique id/name/aria-label/placeholder)
        and file/hidden/button inputs are skipped by design; the harvest is
        capped at 60 records. Note: password fields ARE captured — the
        store is local to the operator's workspace, same as the cookie jar.
        """
        try:
            rows = await self.evaluate_value(self._FORM_HARVEST_JS)
        except Exception as exc:
            logger.debug("form-state harvest failed: %s", exc)
            return []
        if not isinstance(rows, list):
            return []
        return [
            row for row in rows
            if isinstance(row, dict) and row.get("selector")
        ]

    async def restore_form_state(
        self, fields: list[dict[str, Any]], timeout: float = 5.0,
    ) -> list[dict[str, Any]]:
        """Re-apply harvested/filled fields; returns per-field results.

        Text-like fields go through :meth:`fill` (label resolution +
        framework event dispatch + value verification). Checkboxes/radios
        are toggled via a synthetic click; multi-selects select options
        directly and dispatch change events. Every branch waits for its
        element to mount first (up to *timeout*), matching fill()'s
        behaviour on client-rendered pages. Malformed records (non-dict,
        missing selector) are reported as failures rather than raised, so
        one bad record can never abort the replay of the rest.
        """
        results: list[dict[str, Any]] = []
        for rec in fields or []:
            if not isinstance(rec, dict):
                # Reached before any attribute access — a corrupted or
                # older-format persisted record must not abort the replay.
                results.append({"selector": "", "ok": False,
                                "note": "malformed record (not an object)"})
                continue
            selector = str(rec.get("selector", ""))
            ftype = str(rec.get("type", "")).lower()
            ok = False
            try:
                if not selector:
                    # Nothing to target — report rather than raise, so one
                    # malformed record can't abort the rest of the replay.
                    entry = {"selector": "", "ok": False,
                             "note": "record has no selector"}
                    if ftype:
                        entry["type"] = ftype
                    results.append(entry)
                    continue
                if ftype in ("checkbox", "radio"):
                    # Same mount-wait fill() gets: SPA controls render late,
                    # and restore-after-navigation is this feature's main use.
                    if not await self.wait_for_selector(selector, timeout):
                        results.append({"selector": selector, "ok": False,
                                        "type": ftype,
                                        "note": "element did not mount in time"})
                        continue
                    want = bool(rec.get("checked"))
                    js = f"""
                    (function() {{
                        {self._RESOLVER_JS}
                        var el = __rb_resolve({json.dumps(selector)});
                        if (!el) return false;
                        if (!!el.checked !== {json.dumps(want)}) el.click();
                        return !!el.checked === {json.dumps(want)};
                    }})()
                    """
                    ok = await self.evaluate_bool(js) is True
                elif rec.get("multiple"):
                    if not await self.wait_for_selector(selector, timeout):
                        entry = {"selector": selector, "ok": False,
                                 "note": "element did not mount in time"}
                        if ftype:
                            entry["type"] = ftype
                        results.append(entry)
                        continue
                    raw_val = rec.get("value")
                    if isinstance(raw_val, list):
                        values = [str(v) for v in raw_val]
                    else:
                        try:
                            values = json.loads(str(raw_val or "[]"))
                        except (ValueError, TypeError):
                            # An unparseable stored value must never CLEAR
                            # the live selection — leave the field untouched.
                            entry = {"selector": selector, "ok": False,
                                     "note": "stored value unparseable; left untouched"}
                            if ftype:
                                entry["type"] = ftype
                            results.append(entry)
                            continue
                    js = f"""
                    (function() {{
                        {self._RESOLVER_JS}
                        var el = __rb_resolve({json.dumps(selector)});
                        if (!el) return false;
                        var want = {json.dumps(values)};
                        for (var i = 0; i < el.options.length; i++) {{
                            el.options[i].selected = want.indexOf(el.options[i].value) !== -1;
                        }}
                        el.dispatchEvent(new Event('input', {{bubbles: true}}));
                        el.dispatchEvent(new Event('change', {{bubbles: true}}));
                        return true;
                    }})()
                    """
                    ok = await self.evaluate_bool(js) is True
                else:
                    ok = await self.fill(
                        selector, str(rec.get("value", "")), timeout=timeout,
                    )
            except Exception:
                ok = False
            entry = {"selector": selector, "ok": ok}
            if ftype:
                entry["type"] = ftype
            if not ok and ftype == "file":
                entry["note"] = "file inputs cannot be re-filled programmatically"
            if not ok:
                logger.debug(
                    "restore_form_state: %r (type=%s) did not re-apply",
                    selector, ftype or "?",
                )
            results.append(entry)
        return results

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

    async def accessibility_snapshot(
        self, *, interactive_only: bool = False, max_nodes: int = 200,
        max_depth: int = 12, include_values: bool = False,
        include_bounds: bool = False,
    ) -> dict[str, Any]:
        """Return a hybrid CDP accessibility tree enriched with DOM references."""
        self._navigation_id += 1
        snapshot_id = "snap_" + hashlib.sha256(
            f"{self._current_url}:{self._navigation_id}:{_time.monotonic_ns()}".encode()
        ).hexdigest()[:16]
        nodes: list[dict[str, Any]] = []
        cdp_available = True
        try:
            tree = await self._cdp.send("Accessibility.getFullAXTree")
            for raw in (tree.get("nodes") or []):
                role = str((raw.get("role") or {}).get("value") or "generic")
                name = str((raw.get("name") or {}).get("value") or "")
                props = {item.get("name"): item.get("value", {}).get("value") for item in raw.get("properties", [])}
                if interactive_only and role not in {"button", "link", "textbox", "checkbox", "radio", "combobox", "listbox", "option", "menuitem", "tab", "searchbox"}:
                    continue
                if len(nodes) >= max_nodes:
                    break
                ref = f"e{len(nodes) + 1}"
                node = {"ref": ref, "role": role, "name": name,
                        "disabled": props.get("disabled", False),
                        "checked": props.get("checked", False),
                        "level": props.get("level")}
                if include_values:
                    node["value"] = str((raw.get("value") or {}).get("value") or "")[:1000]
                if include_bounds and raw.get("backendDOMNodeId"):
                    node["backend_node_id"] = raw["backendDOMNodeId"]
                nodes.append(node)
        except Exception as exc:
            cdp_available = False
            logger.debug("CDP accessibility snapshot unavailable: %s", exc)

        # DOM/ARIA enrichment supplies stable selectors and shadow-aware labels.
        # Each interactive element gets a computed CSS path (nth-of-type based,
        # id/data-testid preferred) so references resolve even on ID-less
        # React/SPA pages where '#id' selectors never exist.
        _DOM_ENRICHMENT_JS = """(function(){
            function cssPath(el){
                if (el.id) return '#' + CSS.escape(el.id);
                var parts = [];
                var node = el;
                while (node && node.nodeType === 1 && parts.length < 12){
                    var seg = node.tagName.toLowerCase();
                    if (node.id){ parts.unshift('#' + CSS.escape(node.id)); break; }
                    var parent = node.parentNode;
                    if (parent){
                        var same = Array.prototype.filter.call(
                            parent.children,
                            function(c){ return c.tagName === node.tagName; }
                        );
                        if (same.length > 1){
                            seg += ':nth-of-type(' + (same.indexOf(node) + 1) + ')';
                        }
                    }
                    parts.unshift(seg);
                    node = parent;
                }
                return parts.join(' > ');
            }
            return {
                url: location.href,
                elements: Array.from(document.querySelectorAll(
                    'a,button,input,textarea,select,[role],[contenteditable="true"],[contenteditable=""]'
                )).slice(0, 500).map(function(el){
                    return {
                        tag: el.tagName.toLowerCase(),
                        role: el.getAttribute('role') || '',
                        name: (el.getAttribute('aria-label') || el.getAttribute('name') ||
                               el.getAttribute('placeholder') ||
                               (el.textContent || '').trim().slice(0, 120)) || '',
                        id: el.id || '',
                        testid: el.getAttribute('data-testid') || '',
                        path: cssPath(el),
                        disabled: !!el.disabled
                    };
                })
            };
        })()"""
        dom_result = await self.evaluate_value(_DOM_ENRICHMENT_JS)
        dom_elements = dom_result.get("elements", []) if isinstance(dom_result, dict) else []

        if not nodes:
            # No CDP AX tree (unavailable or everything filtered out): fall
            # back to DOM-only nodes so read_page still returns actionable refs.
            tag_roles = {"a": "link", "button": "button", "input": "textbox",
                         "textarea": "textbox", "select": "combobox"}
            for dom in dom_elements[:max_nodes]:
                role = dom.get("role") or tag_roles.get(dom.get("tag"), dom.get("tag") or "generic")
                if interactive_only and role not in {"button", "link", "textbox", "checkbox", "radio", "combobox", "listbox", "option", "menuitem", "tab", "searchbox"}:
                    continue
                nodes.append({
                    "ref": f"e{len(nodes) + 1}",
                    "role": role, "name": dom.get("name") or "",
                    "disabled": bool(dom.get("disabled")),
                    "selector": self._best_dom_selector(dom),
                    "dom_role": dom.get("role") or dom.get("tag"),
                    "dom_name": dom.get("name"),
                })
        else:
            # Match CDP nodes to DOM elements by (role, name) first, falling
            # back to document order. Index-only matching was wrong whenever
            # interactive_only filtered the CDP list but not the DOM list.
            def _norm(value: Any) -> str:
                return str(value or "").strip().lower()

            consumed: set[int] = set()

            def _claim_match(role: str, name: str) -> dict | None:
                for idx, dom in enumerate(dom_elements):
                    if idx in consumed:
                        continue
                    dom_role = dom.get("role") or self._TAG_ROLE_ALIASES.get(dom.get("tag"))
                    if dom_role != role:
                        continue
                    if _norm(dom.get("name")) == _norm(name):
                        consumed.add(idx)
                        return dom
                return None

            next_seq = 0
            for node in nodes:
                dom = _claim_match(node.get("role", ""), node.get("name", ""))
                if dom is None:
                    while next_seq < len(dom_elements) and next_seq in consumed:
                        next_seq += 1
                    dom = dom_elements[next_seq] if next_seq < len(dom_elements) else None
                    if dom is not None:
                        consumed.add(next_seq)
                        next_seq += 1
                if dom is not None:
                    node["selector"] = self._best_dom_selector(dom)
                    node["dom_role"] = dom.get("role") or dom.get("tag")
                    node["dom_name"] = dom.get("name")
        self._snapshot_id = snapshot_id
        self._snapshot_refs = {node["ref"]: node for node in nodes}
        return {"snapshot_id": snapshot_id, "navigation_id": self._navigation_id,
                "url": self._current_url, "nodes": nodes[:max_nodes],
                "node_count": len(nodes), "truncated": len(nodes) >= max_nodes,
                "source": "cdp_accessibility+dom_aria" if cdp_available and nodes else "dom_aria"}

    _TAG_ROLE_ALIASES = {
        "a": "link", "button": "button", "input": "textbox",
        "textarea": "textbox", "select": "combobox",
    }

    @staticmethod
    def _best_dom_selector(dom: dict) -> str:
        """Pick the most stable selector for a DOM element record."""
        if dom.get("id"):
            return f"#{dom['id']}"
        if dom.get("testid"):
            return f"[data-testid=\"{dom['testid']}\"]"
        return dom.get("path") or ""

    async def act_reference(self, ref: str, action: str, value: str = "", snapshot_id: str = "") -> dict[str, Any]:
        """Act on a snapshot reference, rejecting stale references explicitly."""
        if not snapshot_id or snapshot_id != self._snapshot_id or ref not in self._snapshot_refs:
            return {"status": "error", "success": False, "error_type": "stale_reference",
                    "message": "Reference is stale or belongs to another page snapshot.",
                    "required_action": "read_page"}
        node = self._snapshot_refs[ref]
        selector = node.get("selector") or node.get("dom_name")
        if not selector:
            return {"status": "error", "success": False, "error_type": "reference_unresolved",
                    "message": f"Reference {ref} has no DOM selector; read_page with enrichment again."}
        if action == "click":
            ok = await self.click(selector)
        elif action == "fill":
            ok = await self.fill(selector, value)
        else:
            return {"status": "error", "success": False, "message": f"Unsupported reference action: {action}"}
        return {"status": "ok" if ok else "error", "success": bool(ok), "ref": ref, "action": action,
                "snapshot_id": snapshot_id, "message": "Action completed" if ok else "Action failed"}

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
