"""miniproxy integration — route browser traffic through miniproxy's mitmproxy.

miniproxy (at /root/projects/miniproxy/) is a mitmproxy-based HTTP/HTTPS
interception proxy. When the browser is configured to route through miniproxy
(``--proxy-server=http://127.0.0.1:8080``), all HTTP/HTTPS traffic is
intercepted at the HTTP layer and captured in SQLite.

This module provides helpers to:
  1. Start/stop miniproxy (via subprocess)
  2. Build the proxy URL for Chrome launch args
  3. Read captured flows from miniproxy's SQLite DB
  4. Sync cookies from miniproxy's captured Set-Cookie headers into CookieJar
"""

from __future__ import annotations

import logging
import os
import sqlite3
import subprocess
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_PROXY_PORT = 8080
DEFAULT_PROXY_HOST = "127.0.0.1"


class MiniProxyIntegration:
    """Manages the miniproxy process and provides capture access.

    Usage::

        proxy = MiniProxyIntegration(proxy_dir="/root/projects/miniproxy")
        await proxy.start()
        # ... browser launched with proxy_url = proxy.proxy_url
        flows = proxy.get_captured_flows()
        proxy.stop()
    """

    def __init__(
        self,
        proxy_dir: str | None = None,
        port: int = DEFAULT_PROXY_PORT,
        host: str = DEFAULT_PROXY_HOST,
    ):
        self.proxy_dir = proxy_dir or os.environ.get(
            "RICIBROWSER_MINIPROXY_DIR",
            "/root/projects/miniproxy",
        )
        self.port = port
        self.host = host
        self._proc: subprocess.Popen | None = None

    @property
    def proxy_url(self) -> str:
        """The proxy URL to pass to Chrome's --proxy-server flag."""
        return f"http://{self.host}:{self.port}"

    @property
    def db_path(self) -> str:
        """Path to miniproxy's SQLite capture database."""
        return os.path.join(self.proxy_dir, "proxy.db")

    def is_proxy_available(self) -> bool:
        """Check if miniproxy's mitmdump is available."""
        import shutil
        if shutil.which("mitmdump"):
            return True
        # Check if the proxy_dir has a mitmdump or start script
        start_sh = os.path.join(self.proxy_dir, "start.sh")
        return os.path.isfile(start_sh)

    def start(self, capture: bool = True) -> None:
        """Start the miniproxy mitmdump process.

        Args:
            capture: If True, use the capture addon (passive logging).
                     If False, use the intercept addon (active interception).
        """
        if self._proc and self._proc.poll() is None:
            return  # Already running

        import shutil
        mitmdump = shutil.which("mitmdump")
        if not mitmdump:
            raise RuntimeError(
                "mitmdump not found on $PATH. Install mitmproxy "
                "(https://mitmproxy.org/) first."
            )

        addon = os.path.join(self.proxy_dir, "proxy.py")
        if not os.path.isfile(addon):
            raise RuntimeError(f"miniproxy addon not found at {addon}")

        args = [
            mitmdump,
            "-s", addon,
            "--set", "block_global=false",
            "--listen-host", self.host,
            "--listen-port", str(self.port),
        ]

        logger.info("Starting miniproxy on %s:%d", self.host, self.port)
        self._proc = subprocess.Popen(
            args,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
            cwd=self.proxy_dir,
        )

    def stop(self) -> None:
        """Stop the miniproxy process."""
        if self._proc and self._proc.poll() is None:
            import signal
            try:
                os.killpg(os.getpgid(self._proc.pid), signal.SIGTERM)
                self._proc.wait(timeout=5)
            except (ProcessLookupError, subprocess.TimeoutExpired):
                try:
                    os.killpg(os.getpgid(self._proc.pid), signal.SIGKILL)
                except ProcessLookupError:
                    pass
        self._proc = None

    def get_captured_flows(self, limit: int = 100, since_id: int = 0) -> list[dict[str, Any]]:
        """Read captured request/response flows from miniproxy's SQLite DB.

        Returns a list of dicts with: id, method, url, status_code, headers,
        request_body, response_body, content_type, created_at.
        """
        if not os.path.exists(self.db_path):
            return []

        try:
            conn = sqlite3.connect(self.db_path)
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM requests WHERE id > ? ORDER BY id DESC LIMIT ?",
                (since_id, limit),
            ).fetchall()
            conn.close()
            return [dict(r) for r in rows]
        except Exception as exc:
            logger.warning("Failed to read miniproxy DB: %s", exc)
            return []

    def get_captured_endpoints(self) -> list[dict[str, str]]:
        """Deduplicate captured traffic into an attack-surface inventory.

        Returns unique endpoints as [{method, url, has_params}, ...].
        """
        flows = self.get_captured_flows(limit=1000)
        seen: set[str] = set()
        endpoints: list[dict[str, str]] = []
        for f in flows:
            method = f.get("method", "GET")
            url = f.get("url", "")
            if not url:
                continue
            # Strip query string for dedup, but note if params were present
            base_url = url.split("?")[0]
            has_params = "?" in url
            key = f"{method}:{base_url}"
            if key not in seen:
                seen.add(key)
                endpoints.append({"method": method, "url": base_url, "has_params": has_params})
        return endpoints
