"""Chrome binary discovery and launch with remote-debugging-port.

Finds the user's REAL installed Chrome (not a bundled Chromium) so the TLS/JA3
fingerprint matches a real Chrome release. Launches with a remote debugging
port that our CDP client connects to.

This does NOT use ChromeDriver, selenium, or any automation driver — just a
plain Chrome process with --remote-debugging-port that speaks CDP directly.
"""

from __future__ import annotations

import logging
import os
import shutil
import signal
import socket
import subprocess
import time
from typing import Any

from ricibrowser.stealth import get_stealth_args

logger = logging.getLogger(__name__)

# Chrome binary names to search for (in priority order — stable channel first).
_CHROME_NAMES = [
    "google-chrome",
    "google-chrome-stable",
    "chrome",
    "chromium-browser",
    "chromium",
    "google-chrome-beta",
    "google-chrome-unstable",
    "brave-browser",  # Brave is Chromium-based, speaks CDP
    "microsoft-edge",  # Edge is Chromium-based, speaks CDP
    "microsoft-edge-stable",
]

# macOS app paths (if shutil.which doesn't find them)
_MACOS_PATHS = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
]


def _process_group_alive(pgid: int) -> bool:
    """Whether a POSIX process group still has a non-zombie member.

    ``killpg(pgid, 0)`` reports zombie-only groups as existing. Zombies cannot
    access a Chrome profile and may remain visible until an unrelated PID 1
    reaps them, so Linux uses procfs to distinguish them. Other POSIX systems
    conservatively fall back to ``killpg``.
    """
    proc_root = "/proc"
    if os.path.isdir(proc_root):
        try:
            entries = os.listdir(proc_root)
        except OSError:
            entries = []
        inspected = False
        for name in entries:
            if not name.isdigit():
                continue
            try:
                with open(os.path.join(proc_root, name, "stat"), encoding="utf-8") as file:
                    stat = file.read()
                # comm is parenthesized and may contain spaces/parentheses; the
                # fields after its final ')' start with state, ppid, pgrp.
                tail = stat[stat.rfind(")") + 2:].split()
                state, member_pgid = tail[0], int(tail[2])
                inspected = True
            except (OSError, ValueError, IndexError):
                continue
            if member_pgid == pgid and state != "Z":
                return True
        if inspected:
            return False
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _linux_process_start_ticks(pid: int) -> int | None:
    """Read Linux's boot-relative process creation tick for PID-reuse checks."""
    try:
        with open(f"/proc/{pid}/stat", encoding="utf-8") as file:
            stat = file.read()
        tail = stat[stat.rfind(")") + 2:].split()
        return int(tail[19])  # field 22 overall; tail starts at field 3
    except (OSError, ValueError, IndexError):
        return None


def _capture_group_identity(proc: subprocess.Popen, pgid: int) -> dict[str, Any]:
    """Capture the private group anchor while its original leader is alive."""
    identity: dict[str, Any] = {
        "pgid": int(pgid),
        "leader_pid": int(proc.pid),
        "leader_start_ticks": _linux_process_start_ticks(proc.pid),
    }
    try:
        with open("/proc/sys/kernel/random/boot_id", encoding="utf-8") as file:
            identity["boot_id"] = file.read().strip()
    except OSError:
        identity["boot_id"] = None
    return identity


def _validate_group_identity(identity: dict[str, Any]) -> None:
    """Refuse to signal an anchor whose leader PID has been visibly reused."""
    expected_boot = identity.get("boot_id")
    if expected_boot:
        try:
            with open("/proc/sys/kernel/random/boot_id", encoding="utf-8") as file:
                if file.read().strip() != expected_boot:
                    raise RuntimeError("Chrome process-group identity belongs to another boot")
        except OSError as exc:
            raise RuntimeError("Cannot verify Chrome process-group boot identity") from exc

    expected_start = identity.get("leader_start_ticks")
    if expected_start is None:
        return
    current_start = _linux_process_start_ticks(int(identity["leader_pid"]))
    if current_start is not None and current_start != expected_start:
        raise RuntimeError(
            "Chrome process-group leader PID was reused; refusing to signal an unrelated group"
        )


def find_chrome() -> str | None:
    """Find the user's real installed Chrome/Chromium/Edge binary.

    Returns the path to the binary, or None if not found.
    """
    # Check $PATH first
    for name in _CHROME_NAMES:
        path = shutil.which(name)
        if path:
            logger.debug("Found Chrome binary: %s", path)
            return path

    # Check macOS app paths
    if os.path.exists("/Applications"):
        for path in _MACOS_PATHS:
            if os.path.isfile(path):
                logger.debug("Found Chrome binary (macOS): %s", path)
                return path

    logger.warning("No Chrome/Chromium binary found on $PATH or /Applications")
    return None


def find_free_port() -> int:
    """Reserve and return a free localhost TCP port.

    Binding to port 0 lets the kernel hand out a port that is currently unused,
    which is how each :class:`~ricibrowser.engine.Engine` gets its own debugging
    port instead of every instance fighting over the fixed 9223. Two engines
    sharing 9223 meant the second launch silently attached to the FIRST
    Chrome's CDP endpoint, so concurrent sessions stomped on each other's tabs
    and cookies.

    There is an unavoidable race: the socket must be closed before Chrome can
    bind the port, so another process could claim it in between. In practice the
    kernel does not immediately recycle a just-released ephemeral port, and the
    caller's startup health check surfaces the failure if it ever happens.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def launch_chrome(
    port: int = 0,
    proxy: str | None = None,
    stealth: bool = True,
    user_data_dir: str | None = None,
    extra_args: list[str] | None = None,
    headless: bool = True,
    viewport_width: int = 1920,
    viewport_height: int = 1080,
    user_agent: str | None = None,
    ignore_cert_errors: bool | None = None,
) -> subprocess.Popen:
    """Launch Chrome with a remote debugging port for CDP access.

    Args:
        port: Remote debugging port. ``0`` (default) picks a free ephemeral
            port. Callers that need to connect afterwards should resolve the
            port themselves with :func:`find_free_port` and pass it in, since
            the launched process handle does not expose it.
        proxy: Optional proxy URL (e.g. http://127.0.0.1:8080 for miniproxy).
        stealth: If True, suppress navigator.webdriver via launch flags.
        user_data_dir: Optional Chrome profile dir for cookie/cf_clearance persistence.
        extra_args: Additional Chrome flags.
        headless: If True, launch in --headless=new mode (Chrome 112+).

    Returns:
        A subprocess.Popen handle for the Chrome process.

    Raises:
        RuntimeError: If no Chrome binary is found.
    """
    chrome_path = find_chrome()
    if not chrome_path:
        raise RuntimeError(
            "No Chrome/Chromium/Edge binary found. Install Google Chrome "
            "(https://www.google.com/chrome/) or set it on $PATH."
        )

    if not port:
        port = find_free_port()

    args = [chrome_path]

    # Headless mode: use the "new" headless (Chrome 112+) which is more
    # compatible with real Chrome behavior.
    if headless:
        args.append("--headless=new")

    args.append(f"--remote-debugging-port={port}")
    args.append("--remote-debugging-address=127.0.0.1")

    # Stealth / basic args
    args.extend(get_stealth_args(stealth=stealth, proxy=proxy, extra=extra_args,
                                  ignore_cert_errors=ignore_cert_errors))

    # User data dir for profile persistence (cf_clearance, site cookies)
    if user_data_dir:
        os.makedirs(user_data_dir, exist_ok=True)
        args.append(f"--user-data-dir={user_data_dir}")

    # Disable GPU. In headless containers there is usually no GPU; leaving GPU
    # enabled makes Chrome try (and fail) to init a GL context, which manifests
    # as flaky/aborted renders and occasional blank pages. --disable-gpu forces
    # the software rasterizer, which is deterministic in headless environments.
    if headless:
        args.append("--disable-gpu")

    # Apply viewport size
    if viewport_width and viewport_height:
        args.append(f"--window-size={viewport_width},{viewport_height}")

    # Apply custom user agent (if set)
    if user_agent:
        args.append(f"--user-agent={user_agent}")

    logger.info("Launching Chrome: %s (port %d)", chrome_path, port)
    kwargs: dict[str, Any] = {
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "stdin": subprocess.DEVNULL,
    }
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True  # setsid — survives parent exit
    proc = subprocess.Popen(args, **kwargs)
    # A private process-group anchor must be captured at creation time. Looking
    # up getpgid(proc.pid) after the leader exits either fails (while children
    # remain) or, after PID reuse, can identify an unrelated process group.
    if os.name != "nt":
        try:
            pgid = os.getpgid(proc.pid)
        except OSError:
            pgid = proc.pid
        proc._ricibrowser_pgid = pgid  # type: ignore[attr-defined]
        proc._ricibrowser_group_identity = _capture_group_identity(  # type: ignore[attr-defined]
            proc, pgid,
        )
    logger.info("Chrome launched (PID %d)", proc.pid)
    return proc


def stop_chrome(proc: subprocess.Popen) -> None:
    """Gracefully stop a Chrome process launched by :func:`launch_chrome`.

    Handles both POSIX (process group kill) and Windows (taskkill).
    """
    if os.name == "nt":
        if proc.poll() is not None:
            raise RuntimeError(
                "Chrome leader exited before its Windows process tree could be verified stopped"
            )
        # Windows: no process groups — use taskkill to kill the tree
        try:
            completed = subprocess.run(
                ["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=10,
            )
        except (subprocess.TimeoutExpired, FileNotFoundError):
            try:
                proc.kill()
            except ProcessLookupError:
                pass
        else:
            if completed.returncode != 0:
                raise RuntimeError(
                    f"taskkill could not verify Chrome process tree {proc.pid} stopped "
                    f"(exit {completed.returncode})"
                )
        try:
            proc.wait(timeout=5)
        except (subprocess.TimeoutExpired, ChildProcessError):
            pass
        if proc.poll() is None:
            raise RuntimeError(f"Chrome process tree {proc.pid} did not stop")
    else:
        # POSIX: kill the private process group.  Do this even when the leader
        # already exited: Chrome children can outlive it and continue using the
        # profile.  Never derive an ownership anchor from a potentially-reused
        # PID during shutdown.
        identity = getattr(proc, "_ricibrowser_group_identity", None)
        pgid = (identity or {}).get("pgid") or getattr(proc, "_ricibrowser_pgid", None)
        if pgid is None:
            # Backwards compatibility for externally supplied/fake Popen
            # handles. A live leader can still prove its current group.
            if proc.poll() is not None:
                raise RuntimeError(
                    "Chrome leader exited but no owned process-group anchor is available"
                )
            pgid = os.getpgid(proc.pid)
        elif identity:
            _validate_group_identity(identity)

        def _group_alive() -> bool:
            # poll() reaps the direct child when it has exited, preventing its
            # zombie record from keeping an otherwise-empty group "alive".
            proc.poll()
            return _process_group_alive(int(pgid))

        def _wait_group_gone(timeout: float) -> bool:
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                if not _group_alive():
                    return True
                time.sleep(0.05)
            return not _group_alive()

        # NOTE: this is a *synchronous* helper — callers on the event loop
        # must run it in an executor (Engine._ensure_chrome/close do).
        # Group checks are bounded so a Chrome that ignores SIGTERM cannot hold
        # the caller indefinitely.
        if not _group_alive():
            try:
                proc.wait(timeout=1)
            except (subprocess.TimeoutExpired, ChildProcessError):
                pass
            return
        try:
            os.killpg(int(pgid), signal.SIGTERM)
        except ProcessLookupError:
            pass
        if _wait_group_gone(5.0):
            try:
                proc.wait(timeout=1)
            except (subprocess.TimeoutExpired, ChildProcessError):
                pass
            return

        try:
            os.killpg(int(pgid), signal.SIGKILL)
        except ProcessLookupError:
            pass
        if _wait_group_gone(5.0):
            try:
                proc.wait(timeout=1)
            except (subprocess.TimeoutExpired, ChildProcessError):
                pass
            return
        raise RuntimeError(f"Chrome process group {pgid} did not stop after SIGKILL")


def get_debug_url(port: int) -> str:
    """Return the HTTP CDP discovery URL for a Chrome instance.

    The port is required: with ephemeral ports there is no meaningful default,
    and a defaulted one would silently point at another engine's browser.
    """
    return f"http://127.0.0.1:{port}"
