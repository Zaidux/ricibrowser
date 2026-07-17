"""Engine configuration and auto-selection logic."""

from __future__ import annotations

import enum
import os
from dataclasses import dataclass, field


class EngineType(enum.Enum):
    """Browser engine selection."""

    AUTO = "auto"
    """Auto-select: Lightpanda for fast_browse, CDP-Chrome for sessions."""

    LIGHTPANDA = "lightpanda"
    """Fast path — Zig-based headless engine via CDP."""

    CDP_CHROME = "cdp_chrome"
    """Thorough path — custom CDP client driving real installed Chrome."""


@dataclass
class EngineConfig:
    """Configuration for the two-engine browser layer.

    Attributes:
        fast_engine: Engine for crawl/recon/fast browse (default AUTO → Lightpanda).
        thorough_engine: Engine for DAST/sessions (default CDP_CHROME).
        lightpanda_url: CDP WebSocket URL for Lightpanda (default ws://127.0.0.1:9222).
        chrome_debug_port: Remote debugging port for Chrome (default 9223).
        proxy_url: Optional proxy URL (e.g. http://127.0.0.1:8080 for miniproxy).
        cookie_jar_path: Path to persistent cookie/localStorage JSON file.
        debug_network: If True, capture request/response via CDP Network domain.
            Off by default — Network.enable is a detection vector.
        stealth: If True, suppress navigator.webdriver via launch flags (no JS injection).
        user_data_dir: Optional Chrome user-data-dir for profile persistence (cf_clearance).
        viewport_width: viewport width (default 1920).
        viewport_height: viewport height (default 1080).
        user_agent: Optional custom User-Agent. If None, uses Chrome's default.
        timeout: Default navigation timeout in seconds (default 30).
    """

    fast_engine: EngineType = EngineType.AUTO
    thorough_engine: EngineType = EngineType.CDP_CHROME
    lightpanda_url: str = "ws://127.0.0.1:9222"
    chrome_debug_port: int = 9223
    proxy_url: str | None = None
    cookie_jar_path: str | None = None
    debug_network: bool = False
    stealth: bool = True
    user_data_dir: str | None = None
    viewport_width: int = 1920
    viewport_height: int = 1080
    user_agent: str | None = None
    timeout: float = 30.0
    extra_chrome_args: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if isinstance(self.fast_engine, str):
            self.fast_engine = EngineType(self.fast_engine)
        if isinstance(self.thorough_engine, str):
            self.thorough_engine = EngineType(self.thorough_engine)

        # Resolve default lightpanda URL from env
        env_url = os.environ.get("RICIBROWSER_LIGHTPANDA_URL")
        if env_url:
            self.lightpanda_url = env_url

        # Resolve default cookie jar path
        if self.cookie_jar_path is None:
            env_path = os.environ.get("RICIBROWSER_COOKIE_JAR")
            if env_path:
                self.cookie_jar_path = env_path

    @property
    def fast_engine_resolved(self) -> EngineType:
        """Resolve AUTO to the actual engine."""
        if self.fast_engine == EngineType.AUTO:
            return EngineType.LIGHTPANDA
        return self.fast_engine
