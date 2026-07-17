"""Stealth configuration — suppress automation detection.

Key principle: ALL stealth is achieved via Chrome launch flags, NOT via JS
injection. JS-override of navigator.webdriver is detectable (the override
itself can be fingerprinted). Launch flags are invisible to the page.

Approach:
  - ``--disable-blink-features=AutomationControlled`` — suppresses
    navigator.webdriver at the Blink level. This is the standard Chrome flag
    for automation; when combined with a real Chrome binary (not bundled
    Chromium), the TLS/JA3 fingerprint also matches a real Chrome release.
  - ``--no-first-run`` / ``--disable-default-apps`` — prevent Chrome from
    showing first-run UI or installing default extensions (headless artifacts).
  - No ``--enable-automation`` flag — its ABSENCE prevents the
    "Chrome is being controlled by automated test software" infobar and the
    associated CDP detection.

What we do NOT do (and why):
  - JS injection to set ``navigator.webdriver = false`` — detectable via
    ``Object.getOwnPropertyDescriptor(navigator, 'webdriver')`` checks.
  - JS injection to fake ``navigator.plugins`` — real Chrome has real plugins;
    patching them creates a fingerprint MORE unique than the default.
  - JS injection to fake ``window.chrome`` — present in real Chrome already.
  - ``Console.enable`` by default — a known CDP detection vector. Only enabled
    in explicit debug mode.
  - ``Runtime.enable`` on the main world — we use isolated worlds only.
"""

from __future__ import annotations

# Chrome launch flags that suppress automation detection WITHOUT JS injection.
# These are applied at the Blink/compiler level, invisible to the page.
STEALTH_FLAGS: list[str] = [
    "--disable-blink-features=AutomationControlled",
    "--no-first-run",
    "--disable-default-apps",
    "--disable-extensions",
    "--disable-background-networking",
    "--disable-sync",
    "--disable-translate",
    "--mute-audio",
    "--no-sandbox",
    "--disable-dev-shm-usage",
    "--disable-features=IsolateOrigins,site-per-process",
    "--disable-site-isolation-trials",
]

# Basic flags for non-stealth headless operation (less anti-detection, faster).
BASIC_FLAGS: list[str] = [
    "--no-first-run",
    "--disable-default-apps",
    "--disable-extensions",
    "--disable-background-networking",
    "--no-sandbox",
    "--disable-dev-shm-usage",
    "--disable-gpu",
]


def get_stealth_args(stealth: bool = True, proxy: str | None = None,
                     extra: list[str] | None = None) -> list[str]:
    """Build Chrome launch argument list.

    Args:
        stealth: If True, use full stealth flags. If False, use basic flags.
        proxy: Optional proxy URL (e.g. http://127.0.0.1:8080).
        extra: Additional flags to append.

    Returns:
        List of Chrome command-line arguments.
    """
    args = list(STEALTH_FLAGS if stealth else BASIC_FLAGS)
    if proxy:
        args.append(f"--proxy-server={proxy}")
    if extra:
        args.extend(extra)
    return args
