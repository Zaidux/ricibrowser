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
import subprocess
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


def launch_chrome(
    port: int = 9223,
    proxy: str | None = None,
    stealth: bool = True,
    user_data_dir: str | None = None,
    extra_args: list[str] | None = None,
    headless: bool = True,
) -> subprocess.Popen:
    """Launch Chrome with a remote debugging port for CDP access.

    Args:
        port: Remote debugging port (default 9223).
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

    args = [chrome_path]

    # Headless mode: use the "new" headless (Chrome 112+) which is more
    # compatible with real Chrome behavior.
    if headless:
        args.append("--headless=new")

    args.append(f"--remote-debugging-port={port}")
    args.append("--remote-debugging-address=127.0.0.1")

    # Stealth / basic args
    args.extend(get_stealth_args(stealth=stealth, proxy=proxy, extra=extra_args))

    # User data dir for profile persistence (cf_clearance, site cookies)
    if user_data_dir:
        os.makedirs(user_data_dir, exist_ok=True)
        args.append(f"--user-data-dir={user_data_dir}")

    # Disable GPU (headless environments often don't have GPU)
    if headless:
        args.append("--disable-gpu")

    logger.info("Launching Chrome: %s (port %d)", chrome_path, port)
    proc = subprocess.Popen(
        args,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
        start_new_session=True,  # survives parent exit (setsid)
    )
    logger.info("Chrome launched (PID %d)", proc.pid)
    return proc


def stop_chrome(proc: subprocess.Popen) -> None:
    """Gracefully stop a Chrome process launched by :func:`launch_chrome`."""
    if proc.poll() is not None:
        return  # Already exited
    try:
        # Try graceful shutdown first
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        proc.wait(timeout=5)
    except (ProcessLookupError, subprocess.TimeoutExpired):
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except ProcessLookupError:
            pass


def get_debug_url(port: int = 9223) -> str:
    """Return the HTTP CDP discovery URL for a Chrome instance."""
    return f"http://127.0.0.1:{port}"
