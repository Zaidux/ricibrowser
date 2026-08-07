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

# Flags that cap disk/memory footprint — critical on phones (Termux/Android)
# where the on-disk cache + GPU shader cache + crash dumps can grow to tens of
# MB per session. Applied to BOTH stealth and basic profiles.
#   --disk-cache-size / --media-cache-size: hard-cap the HTTP + media caches
#       (bytes). We keep a tiny 5 MB cache rather than 0 so pages still load
#       fast within a session, but it can't balloon.
#   --disable-gpu-shader-disk-cache: no persistent GLSL cache on disk.
#   --disable-breakpad / --disable-crash-reporter: no crash-dump minidumps.
#   --disable-component-update: don't download component blobs in the bg.
#   --disable-back-forward-cache: frees per-tab memory.
#   --aggressive-cache-discard / --disable-application-cache: shed memory.
_FOOTPRINT_FLAGS: list[str] = [
    "--disk-cache-size=5242880",       # 5 MB HTTP cache cap
    "--media-cache-size=5242880",      # 5 MB media cache cap
    "--disable-gpu-shader-disk-cache",
    "--disable-breakpad",
    "--disable-crash-reporter",
    "--disable-component-update",
    "--disable-back-forward-cache",
    "--disable-application-cache",
    "--disable-hang-monitor",
    "--disable-logging",
    "--disable-domain-reliability",
    "--no-pings",
]

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
] + _FOOTPRINT_FLAGS

# Basic flags for non-stealth headless operation (less anti-detection, faster).
BASIC_FLAGS: list[str] = [
    "--no-first-run",
    "--disable-default-apps",
    "--disable-extensions",
    "--disable-background-networking",
    "--no-sandbox",
    "--disable-dev-shm-usage",
    "--disable-gpu",
] + _FOOTPRINT_FLAGS


def get_stealth_args(stealth: bool = True, proxy: str | None = None,
                     extra: list[str] | None = None,
                     ignore_cert_errors: bool | None = None) -> list[str]:
    """Build Chrome launch argument list.

    Args:
        stealth: If True, use full stealth flags. If False, use basic flags.
        proxy: Optional proxy URL (e.g. http://127.0.0.1:8080).
        extra: Additional flags to append.
        ignore_cert_errors: Trust otherwise-invalid TLS certs for this session.
            ``None`` (default) means *auto*: enabled whenever ``proxy`` is set,
            disabled otherwise. A MITM intercept proxy (miniproxy/mitmproxy)
            presents its own self-signed CA, so without this Chrome rejects every
            HTTPS response with ``NET::ERR_CERT_AUTHORITY_INVALID``. Pass an
            explicit bool to override the auto behaviour.

    Returns:
        List of Chrome command-line arguments.
    """
    args = list(STEALTH_FLAGS if stealth else BASIC_FLAGS)
    if proxy:
        args.append(f"--proxy-server={proxy}")
    if extra:
        args.extend(extra)

    # Auto-trust the proxy's CA when going through a proxy, unless the caller
    # explicitly overrides. Scoped this way so ordinary (non-proxied) browsing
    # keeps full certificate validation and fidelity. Added last, after `extra`,
    # so the membership guards dedupe against every other flag too.
    if ignore_cert_errors is None:
        ignore_cert_errors = bool(proxy)
    if ignore_cert_errors:
        # --ignore-certificate-errors: blanket-accept invalid certs. Paired with
        #   --test-type so recent Chrome (111+) actually honours it in
        #   --headless=new instead of silently dropping the flag.
        # --allow-insecure-localhost: covers proxies bound to localhost.
        for flag in (
            "--ignore-certificate-errors",
            "--allow-insecure-localhost",
            "--test-type",
        ):
            if flag not in args:
                args.append(flag)

    return args
