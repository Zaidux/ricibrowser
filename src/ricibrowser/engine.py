"""Engine facade — auto-selects Lightpanda (fast) or CDP-Chrome (thorough).

The engine is the public entry point for ricibrowser. It manages the lifecycle
of browser processes and CDP connections, and auto-falls-back from Lightpanda
to Chrome when Lightpanda is unavailable or a feature isn't supported (e.g.
screenshots).
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import subprocess
import tempfile
import copy
import inspect
from typing import Any

from ricibrowser.chrome_launcher import find_free_port, get_debug_url, launch_chrome, stop_chrome
from ricibrowser.cdp_client import CDPClient, CDPError
from ricibrowser.config import EngineConfig, EngineType
from ricibrowser.cookie_jar import CookieJar
from ricibrowser.fingerprint import FingerprintShield
from ricibrowser.geolocation import GEO_PROFILES, GeolocationManager
from ricibrowser.captcha import CaptchaHandler, CloudflareAutoSolver
from ricibrowser.headers import HeaderManager
from ricibrowser.input import HumanMouse
from ricibrowser.lightpanda import LightpandaEngine
from ricibrowser.network import NetworkCapture
from ricibrowser.session import Page, Session
from ricibrowser.utils import truncate, validate_url


async def _run_blocking(fn, *args):
    """Run a blocking callable off the event loop.

    Chrome shutdown (SIGTERM, then a bounded wait, then SIGKILL) and
    profile-directory removal are both synchronous and can take seconds.
    Calling them directly from an async close() froze the whole loop —
    measured 5s of total stall for one Chrome that ignored SIGTERM — which
    stalls every concurrent session on the same loop.
    """
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, fn, *args)


_PROFILE_PREFIX = "ricibrowser_profile_"


def _sweep_stale_profiles(max_age_seconds: int = 6 * 3600) -> int:
    """Delete orphaned ``ricibrowser_profile_*`` dirs left by crashed runs.

    close() removes the profile on a clean exit, but a SIGKILL or a
    ``kill -9`` skips it — leaving a Chrome profile (session cookies,
    cf_clearance, localStorage) in /tmp indefinitely. Directories are
    PID-stamped, so a live sibling engine's profile is identifiable and
    skipped; anything older than *max_age_seconds* is removed as a
    backstop for a PID that was reused.

    Returns the number of directories removed. Never raises.
    """
    import glob
    import time

    removed = 0
    tmp = tempfile.gettempdir()
    cutoff = time.time() - max_age_seconds
    for entry in glob.glob(os.path.join(tmp, _PROFILE_PREFIX + "*")):
        rest = os.path.basename(entry)[len(_PROFILE_PREFIX):]
        pid_part = rest.split("_", 1)[0]
        try:
            pid = int(pid_part)
        except ValueError:
            continue
        if pid == os.getpid():
            continue
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            pass            # owner is gone — safe to delete
        except OSError:
            continue        # alive but not ours (EPERM) — leave it alone
        else:
            continue        # process is still running
        try:
            if os.path.getmtime(entry) > cutoff:
                continue
            shutil.rmtree(entry, ignore_errors=True)
            removed += 1
        except OSError:
            continue
    return removed

logger = logging.getLogger(__name__)


def _make_profile_dir() -> str:
    """Sweep orphans, then create this engine's temp profile dir (blocking).

    PID-stamped so a later run can tell a dead owner's leftovers from a
    live sibling engine's profile, and 0700 because the profile holds
    session cookies and cf_clearance.
    """
    _sweep_stale_profiles()
    path = tempfile.mkdtemp(prefix=f"{_PROFILE_PREFIX}{os.getpid()}_")
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass
    return path


def _remove_profile_dir(path: str) -> None:
    """Remove an owned temporary profile and verify its path is gone."""
    if not os.path.lexists(path):
        return
    shutil.rmtree(path)
    if os.path.lexists(path):
        raise RuntimeError(f"Chrome profile still exists after removal: {path}")


class Engine:
    """Two-engine browser facade.

    Auto-selects the appropriate engine based on the operation:
      - fast_browse() → Lightpanda (fast path, crawl/recon)
      - create_session() → CDP-Chrome (thorough path, DAST/JS-heavy)

    Falls back from Lightpanda → Chrome when Lightpanda is unavailable or
    doesn't support the requested action (e.g. screenshots).
    """

    def __init__(self, config: EngineConfig | None = None):
        self.config = config or EngineConfig()
        self._lightpanda: LightpandaEngine | None = None
        self._chrome_proc: subprocess.Popen | None = None
        # Per-instance Chrome isolation. Two Engines used to share the fixed
        # default port (9223) and, with no user_data_dir configured, a shared
        # profile: the second Engine's "launch" health check found the first
        # Engine's Chrome already answering on that port and attached to it.
        # Both then drove the same browser — tabs, cookies and navigations
        # interleaved across supposedly independent engines. Each instance now
        # gets its own port and its own profile directory.
        self._chrome_port: int = self.config.chrome_debug_port or 0
        self._temp_user_data_dir: str | None = None
        self._chrome_lock: asyncio.Lock | None = None
        self._close_lock: asyncio.Lock | None = None
        self._active_sessions: set[Session] = set()
        self._closing = False
        self._cookie_jar = CookieJar(self.config.cookie_jar_path)
        self._cookie_jar.load()
        self._network = NetworkCapture(enabled=self.config.debug_network)
        # ── Extension modules ──────────────────────────────────────
        self._fingerprint = FingerprintShield(enabled=self.config.fingerprint_shield)
        geo_profile = None
        if self.config.geolocation_profile:
            geo_profile = GEO_PROFILES.get(self.config.geolocation_profile)
        self._geolocation = GeolocationManager(profile=geo_profile)
        self._captcha_handler = CaptchaHandler(
            auto_solver=CloudflareAutoSolver() if self.config.cloudflare_auto_resolve else None,
        )
        self._header_manager = HeaderManager()
        self._human_mouse: HumanMouse | None = None  # Per-session

    @property
    def cookie_jar(self) -> CookieJar:
        return self._cookie_jar

    @property
    def network(self) -> NetworkCapture:
        return self._network

    @property
    def captcha_handler(self) -> CaptchaHandler:
        """Get the CAPTCHA handler for detecting/solving challenges."""
        return self._captcha_handler

    @property
    def human_mouse(self) -> HumanMouse | None:
        """Get the human mouse simulator (None if not enabled)."""
        return self._human_mouse

    @property
    def capabilities(self) -> dict[str, Any]:
        """Serializable Engine capabilities for CLI/runtime introspection."""
        return {
            "schema_version": 1,
            "engines": ["lightpanda", "cdp_chrome"],
            "rendering": True,
            "screenshots": True,
            "http_status": True,
            "trusted_input": True,
            "accessibility_tree": True,
            "isolated_evaluation": True,
            "storage_persistence": True,
            "network_metadata": bool(self._network.enabled),
            "network_bodies": False,
            "proxy_mode": "route_only" if self.config.proxy_url else "none",
            "network": self._network.capabilities,
        }

    def _get_chrome_lock(self) -> asyncio.Lock:
        # Construct lazily in the running loop.  Engine can be instantiated in
        # synchronous code and later used by asyncio.run().
        if self._chrome_lock is None:
            self._chrome_lock = asyncio.Lock()
        return self._chrome_lock

    def _get_close_lock(self) -> asyncio.Lock:
        if self._close_lock is None:
            self._close_lock = asyncio.Lock()
        return self._close_lock

    @staticmethod
    async def _session_cookie_snapshot(session: Session) -> tuple[bool, list[dict]]:
        """Capture cookies with native failure provenance and adapter fallback."""
        native = getattr(session, "_get_cookies_snapshot", None)
        if native is not None:
            return await native()
        # Backwards-compatible Session adapters only expose get_cookies().
        # They cannot distinguish an empty jar from a capture failure, but this
        # keeps the established adapter contract without weakening the native
        # Session's deletion-safe path.
        cookies = await session.get_cookies()
        return True, cookies if isinstance(cookies, list) else []

    async def fast_browse(self, url: str, max_chars: int = 6000, **kwargs) -> Page:
        """Browse a URL using the fast engine (Lightpanda by default).

        Falls back to CDP-Chrome if Lightpanda is unavailable or the page
        needs a screenshot.
        """
        url = validate_url(url)

        # Try Lightpanda first
        engine_type = self.config.fast_engine_resolved
        if engine_type == EngineType.LIGHTPANDA:
            try:
                page = await self._browse_lightpanda(url, max_chars, **kwargs)
                if page is not None:
                    # Save cookies from the browse
                    if page.cookies:
                        self._cookie_jar.update_cookies(page.cookies)
                        self._cookie_jar.save()
                    return page
            except Exception as exc:
                logger.warning("Lightpanda browse failed, falling back to Chrome: %s", exc)

        # Fallback to CDP-Chrome
        return await self._browse_chrome(url, max_chars, **kwargs)

    def fast_browse_sync(self, url: str, **kwargs) -> Page:
        """Synchronous wrapper for fast_browse (for CLI use)."""
        return asyncio.run(self.fast_browse(url, **kwargs))

    async def create_session(self, engine: EngineType | None = None) -> Session:
        """Create a new browser session using the thorough engine (CDP-Chrome).

        The session provides navigate/evaluate/screenshot/click/fill methods.
        Cookies are loaded from the jar on creation and saved on close.
        """
        engine_type = engine or self.config.thorough_engine
        if engine_type == EngineType.LIGHTPANDA:
            return await self._create_lightpanda_session()
        async with self._get_close_lock():
            if self._closing:
                raise RuntimeError("Engine is closing")
            return await self._create_chrome_session()

    async def close(self) -> None:
        """Finalize Sessions and stop owned browser processes.

        Raises ``RuntimeError`` when cleanup cannot be confirmed and retains
        process/profile/session ownership handles so a later call can retry.
        """
        # A few source-compatible consumers/tests construct Engine via
        # ``__new__`` and populate only the historical private ownership
        # fields.  Keep close robust for those objects.
        if not hasattr(self, "_close_lock"):
            self._close_lock = None
        if not hasattr(self, "_active_sessions"):
            self._active_sessions = set()
        if not hasattr(self, "_closing"):
            self._closing = False
        if not hasattr(self, "_chrome_lock"):
            self._chrome_lock = None
        # Engines constructed through the normal initializer own their Chrome
        # lifecycle and preserve it when a Session finalizer fails.  Legacy
        # ``__new__`` test doubles have no config and retain the historical
        # direct-process cleanup behavior.
        normal_engine = hasattr(self, "config")
        async with self._get_close_lock():
            self._closing = True
            errors: list[Exception] = []
            try:
                # Sessions own sockets, capture registrations and page targets;
                # retire those before stopping their backing browser.
                sessions = list(getattr(self, "_active_sessions", ()))
                if sessions:
                    results = await asyncio.gather(
                        *(session.close() for session in sessions),
                        return_exceptions=True,
                    )
                    errors.extend(item for item in results if isinstance(item, Exception))

                if self._lightpanda:
                    lightpanda = self._lightpanda
                    try:
                        await lightpanda.stop()
                    except Exception as exc:
                        logger.warning("Lightpanda stop failed: %s", exc)
                        errors.append(exc)
                    else:
                        self._lightpanda = None

                async with self._get_chrome_lock():
                    proc = self._chrome_proc
                    # Chrome-backed Session finalizers may still need the live
                    # process to retry auth-state/capture retirement.  A failed
                    # finalizer therefore blocks only Chrome/profile teardown;
                    # Lightpanda above remains independent.
                    if (not errors or not normal_engine) and proc is not None:
                        try:
                            await _run_blocking(stop_chrome, proc)
                        except Exception as exc:
                            # Retain process/profile ownership so close() can be
                            # retried and a possibly-live browser never writes
                            # into a directory we prematurely removed.
                            logger.warning("Chrome stop failed: %s", exc)
                            errors.append(exc)
                        else:
                            self._chrome_proc = None

                    if ((not errors or not normal_engine)
                            and self._chrome_proc is None
                            and self._temp_user_data_dir):
                        profile = self._temp_user_data_dir
                        try:
                            await _run_blocking(_remove_profile_dir, profile)
                        except Exception as exc:
                            logger.warning("Chrome profile cleanup failed: %s", exc)
                            errors.append(exc)
                        else:
                            self._temp_user_data_dir = None
            finally:
                self._closing = False
            if errors:
                raise RuntimeError(
                    "Browser cleanup incomplete: "
                    + "; ".join(str(error) for error in errors)
                ) from errors[0]

    # ── Lightpanda path ────────────────────────────────────────────

    async def _get_lightpanda(self) -> LightpandaEngine:
        if self._lightpanda is None or not self._lightpanda.is_started:
            self._lightpanda = LightpandaEngine(
                ws_url=self.config.lightpanda_url,
                timeout=self.config.timeout,
            )
            await self._lightpanda.start()
        return self._lightpanda

    async def _browse_lightpanda(self, url: str, max_chars: int, **kwargs) -> Page | None:
        """Browse via Lightpanda. Returns None if Lightpanda isn't available."""
        if not await LightpandaEngine.is_available(self.config.lightpanda_url):
            logger.info("Lightpanda not available, will use Chrome")
            return None
        lp = await self._get_lightpanda()
        return await lp.browse(url, max_chars=max_chars, cookies=self._cookie_jar.cookies or None, **kwargs)

    async def _create_lightpanda_session(self) -> Session:
        """Create a session via Lightpanda (limited — no screenshots)."""
        lp = await self._get_lightpanda()
        client = lp._client
        if client is None:
            raise RuntimeError("Lightpanda client not initialized")
        session = Session(client, engine_name="lightpanda")
        session._url_stability_timeout = self.config.url_stability_timeout
        session._url_change_timeout = self.config.url_change_timeout
        session._nav_timeout = self.config.nav_timeout
        session._stealth = None
        session._network_capture_enabled = False
        session._proxy_configured = False
        # Preload cookies from jar (same as Chrome path)
        if self._cookie_jar.cookies:
            try:
                await session.set_cookies(self._cookie_jar.cookies)
            except Exception:
                pass
        return session

    # ── CDP-Chrome path ────────────────────────────────────────────

    async def _ensure_chrome(self) -> str:
        """Ensure Chrome is running and return its debug URL.

        Launches Chrome if needed, then polls until the CDP HTTP endpoint is
        reachable (Chrome takes ~1-2s to start listening). Also detects
        zombie/hung Chrome processes — reuses existing process ONLY if it
        responds to a health check.

        This fixes the "All connection attempts failed" error when Chrome
        is slow to start, and the silent hang when Chrome is alive but
        unresponsive.
        """
        async with self._get_chrome_lock():
            if self._closing:
                raise RuntimeError("Engine is closing")

            # An already-running Chrome for THIS engine keeps its port; a fresh
            # launch takes a newly reserved one.
            if self._chrome_proc and self._chrome_proc.poll() is None:
                debug_url = get_debug_url(self._chrome_port)
                import httpx
                try:
                    async with httpx.AsyncClient(timeout=2.0) as http:
                        resp = await http.get(f"{debug_url}/json/version")
                        if resp.status_code == 200:
                            return debug_url
                except Exception:
                    logger.warning("Chrome process alive but not responding — restarting")
                proc = self._chrome_proc
                try:
                    await _run_blocking(stop_chrome, proc)
                except Exception:
                    # Do not overwrite ownership with a second process when the
                    # first process could not be confirmed stopped.
                    raise
                self._chrome_proc = None

            if self._chrome_proc is not None:
                proc = self._chrome_proc
                try:
                    await _run_blocking(stop_chrome, proc)
                except Exception:
                    # An exited leader can still have live descendants. Keep
                    # ownership instead of launching another group over it.
                    raise
                self._chrome_proc = None

            # Build candidate state in locals and publish only after launch
            # succeeds.  The lock makes startup true single-flight and avoids
            # concurrent calls observing an overwritten shared port/profile.
            candidate_port = self.config.chrome_debug_port or find_free_port()
            debug_url = get_debug_url(candidate_port)
            candidate_profile: str | None = None
            user_data_dir = self.config.user_data_dir
            if not user_data_dir:
                if self._temp_user_data_dir:
                    user_data_dir = self._temp_user_data_dir
                else:
                    candidate_profile = await _run_blocking(_make_profile_dir)
                    user_data_dir = candidate_profile

            candidate_proc: subprocess.Popen | None = None
            try:
                candidate_proc = launch_chrome(
                    port=candidate_port,
                    proxy=self.config.proxy_url,
                    stealth=self.config.stealth,
                    user_data_dir=user_data_dir,
                    extra_args=self.config.extra_chrome_args,
                    viewport_width=self.config.viewport_width,
                    viewport_height=self.config.viewport_height,
                    user_agent=self.config.user_agent,
                    ignore_cert_errors=self.config.ignore_cert_errors,
                )

                import httpx
                async with httpx.AsyncClient(timeout=2.0) as http:
                    startup_timeout = getattr(self.config, "chrome_startup_timeout", 20.0)
                    poll_interval = 0.25
                    polls = max(1, int(startup_timeout / poll_interval))
                    for attempt in range(polls):
                        if candidate_proc.poll() is not None:
                            raise RuntimeError(
                                f"Chrome exited during startup (code {candidate_proc.returncode}). "
                                "Check Chrome flags / install. Try headless=new with --no-sandbox."
                            )
                        try:
                            resp = await http.get(f"{debug_url}/json/version")
                            if resp.status_code == 200:
                                self._chrome_port = candidate_port
                                self._chrome_proc = candidate_proc
                                if candidate_profile:
                                    self._temp_user_data_dir = candidate_profile
                                logger.info("Chrome ready after %.1fs", (attempt + 1) * poll_interval)
                                return debug_url
                        except Exception:
                            pass
                        await asyncio.sleep(poll_interval)
                raise RuntimeError(f"Chrome did not become ready after {startup_timeout:.0f}s")
            except BaseException:
                if candidate_proc is not None:
                    try:
                        await _run_blocking(stop_chrome, candidate_proc)
                    except Exception as cleanup_exc:
                        # Failed candidate cleanup is still owned by this
                        # Engine; retain handles rather than launching over it.
                        self._chrome_proc = candidate_proc
                        self._chrome_port = candidate_port
                        if candidate_profile:
                            self._temp_user_data_dir = candidate_profile
                        raise RuntimeError(
                            f"Chrome startup failed and candidate cleanup was incomplete: {cleanup_exc}"
                        ) from cleanup_exc
                if candidate_profile:
                    if candidate_proc is None:
                        # Preserve the historical private ownership handle for
                        # callers that inspect/retry after launch_chrome itself
                        # raises; close() will remove this empty profile.
                        self._temp_user_data_dir = candidate_profile
                    else:
                        try:
                            await _run_blocking(_remove_profile_dir, candidate_profile)
                        except Exception as profile_exc:
                            self._temp_user_data_dir = candidate_profile
                            raise RuntimeError(
                                f"Chrome startup cleanup could not remove profile: {profile_exc}"
                            ) from profile_exc
                raise

    async def _browse_chrome(self, url: str, max_chars: int, **kwargs) -> Page:
        """Browse via CDP-Chrome (thorough path)."""
        async with self._get_close_lock():
            if self._closing:
                raise RuntimeError("Engine is closing")
            debug_url = await self._ensure_chrome()
            client = await CDPClient.connect_to_target(
                debug_url, command_timeout=self.config.cdp_command_timeout,
                create_new=True,
            )
            target_id = str(getattr(client, "_target_id", ""))
            session = Session(client, engine_name="cdp_chrome")
            self._configure_session(session, debug_url=debug_url, target_id=target_id)
            self._active_sessions.add(session)
        result_page: Page | None = None
        cookie_baseline = copy.deepcopy(self._cookie_jar.cookies)
        storage_baseline = self._cookie_jar.storage
        try:
            # Load cookies from jar
            if cookie_baseline:
                await session.set_cookies(cookie_baseline)

            # Install all origin-specific localStorage before navigation.  The
            # new-document hook selects by location.origin and runs before the
            # target application's startup scripts.
            install_restore = getattr(session, "install_storage_restore", None)
            if install_restore is not None:
                await install_restore(self._cookie_jar.storage)

            # Start network capture if enabled
            if self._network.enabled:
                await self._network.start(
                    client, session_id=session.session_id, target_id=target_id,
                )

            # When proxied, avoid "networkidle" — a slow proxy can keep
            # in-flight resource counters from ever settling. Fall back to
            # "load" (Page.loadEventFired) which still fires reliably.
            _want_networkidle = self._network.enabled and not self.config.proxy_url
            wait_until = kwargs.pop(
                "wait_until", "networkidle" if _want_networkidle else "load",
            )
            wait_for = kwargs.pop("wait_for", None)
            timeout = kwargs.pop("timeout", None)
            if kwargs:
                names = ", ".join(sorted(kwargs))
                raise TypeError(f"Unsupported fast_browse option(s) for Chrome: {names}")
            navigate_options: dict[str, Any] = {
                "wait_until": wait_until,
                "wait_for": wait_for,
                "timeout": timeout,
                "max_chars": max_chars,
            }
            # Source-compatible test doubles and old Session adapters may not
            # yet accept additive optional arguments.  Real Session always
            # receives them; only omit None-valued options that the callable's
            # explicit signature does not expose.
            try:
                signature = inspect.signature(session.navigate)
                accepts_kwargs = any(
                    p.kind == inspect.Parameter.VAR_KEYWORD
                    for p in signature.parameters.values()
                )
                if not accepts_kwargs:
                    navigate_options = {
                        key: value for key, value in navigate_options.items()
                        if key in signature.parameters and value is not None
                    }
            except (TypeError, ValueError):
                pass
            result_page = await session.navigate(url, **navigate_options)

            # Save cookies back to jar
            cookie_ok, cookies = await self._session_cookie_snapshot(session)
            if cookie_ok:
                self._cookie_jar.sync_cookies(cookies, baseline=cookie_baseline)
            origin, storage = await session.capture_storage()
            if origin:
                self._cookie_jar.sync_storage(
                    origin, storage, baseline=storage_baseline.get(origin, {}),
                )
            self._cookie_jar.save()

            return result_page
        finally:
            cleanup_error: Exception | None = None
            try:
                if self._network.enabled:
                    await self._network.stop(client)
            except Exception as exc:
                cleanup_error = exc
            try:
                await client.close()
            except Exception as exc:
                cleanup_error = cleanup_error or exc
            if target_id:
                try:
                    await CDPClient.close_target(debug_url, target_id)
                except Exception as exc:
                    logger.debug("Could not close fast-browse target %s: %s", target_id, exc)
                    cleanup_error = cleanup_error or exc
            self._active_sessions.discard(session)
            # Do not turn a successfully captured Page into a false failure
            # merely because target retirement raced Chrome shutdown.
            if cleanup_error is not None and result_page is None:
                raise cleanup_error

    async def _create_chrome_session(self) -> Session:
        """Create a persistent session via CDP-Chrome with all extensions applied."""
        debug_url = await self._ensure_chrome()
        client = await CDPClient.connect_to_target(
            debug_url, command_timeout=self.config.cdp_command_timeout,
            create_new=True,
        )
        session = Session(client, engine_name="cdp_chrome")
        target_id = str(getattr(client, "_target_id", ""))
        self._configure_session(session, debug_url=debug_url, target_id=target_id)
        cookie_baseline = copy.deepcopy(self._cookie_jar.cookies)
        storage_baseline = self._cookie_jar.storage
        self._active_sessions.add(session)

        try:
            # Load cookies and install localStorage before any target app can
            # execute.  Cookies are browser-global within this Engine; targets
            # are unique tabs, not separate browser identities.
            if cookie_baseline:
                await session.set_cookies(cookie_baseline)
            await session.install_storage_restore(storage_baseline)

            if self._network.enabled:
                await self._network.start(
                    client, session_id=session.session_id, target_id=target_id,
                )

            # ── Apply extension modules ────────────────────────────────
            if self._fingerprint.enabled:
                await self._fingerprint.apply(client)

            # Geolocation consistency (timezone/locale/geo override) — only
            # when the operator explicitly requested a geographic profile.
            if self.config.geolocation_profile:
                await self._geolocation.apply(client)

            # Header consistency (Sec-CH-UA matches UA)
            if self.config.header_consistency:
                await self._header_manager.apply(client)

            # Human mouse input (bezier-curve movement). NOTE: this is
            # Engine-scoped, so with concurrent sessions the value reflects the
            # most recently created one.
            if self.config.human_input:
                self._human_mouse = HumanMouse(
                    client,
                    viewport_width=self.config.viewport_width,
                    viewport_height=self.config.viewport_height,
                )

            async def _finalize(owned_session: Session) -> None:
                cookie_ok, cookies = await self._session_cookie_snapshot(owned_session)
                if not cookie_ok:
                    raise RuntimeError("Could not capture browser cookies before Session close")
                self._cookie_jar.sync_cookies(cookies, baseline=cookie_baseline)
                origin, storage = await owned_session.capture_storage()
                if origin:
                    self._cookie_jar.sync_storage(
                        origin, storage,
                        baseline=storage_baseline.get(origin, {}),
                    )
                elif owned_session._current_url and not owned_session._last_capture_succeeded:
                    raise RuntimeError("Could not capture localStorage before Session close")
                self._cookie_jar.save()
                if self._network.enabled:
                    await self._network.stop(client)

            async def _after_close(owned_session: Session) -> None:
                if target_id:
                    await CDPClient.close_target(debug_url, target_id)
                self._active_sessions.discard(owned_session)

            async def _checkpoint(owned_session: Session) -> None:
                origin, storage = await owned_session.capture_storage()
                if origin:
                    self._cookie_jar.sync_storage(
                        origin, storage,
                        baseline=storage_baseline.get(origin, {}),
                    )

            session._close_hook = _finalize
            session._after_close_hook = _after_close
            session._checkpoint_hook = _checkpoint
            return session
        except BaseException:
            self._active_sessions.discard(session)
            if self._network.enabled:
                await self._network.stop(client)
            await client.close()
            if target_id:
                try:
                    await CDPClient.close_target(debug_url, target_id)
                except Exception:
                    pass
            raise

    def _configure_session(self, session: Session, *, debug_url: str, target_id: str) -> None:
        session._url_stability_timeout = self.config.url_stability_timeout
        session._url_change_timeout = self.config.url_change_timeout
        session._nav_timeout = self.config.nav_timeout
        session._target_id = target_id
        session._debug_url = debug_url
        session._stealth = self.config.stealth
        session._network_capture_enabled = self._network.enabled
        session._proxy_configured = bool(self.config.proxy_url)
