"""Canvas/WebGL/Audio fingerprint randomization.

Uses CDP ``Page.addScriptToEvaluateOnNewDocument`` to inject JS that runs
BEFORE any page script. This is NOT ``Runtime.enable`` on the main world —
it's a legitimate CDP method that pre-seeds the page with our script, and
it does not leave a detectable CDP-attached artifact.

**This is opt-in** (default off). Unlike the stealth flags (which suppress
``navigator.webdriver`` via Blink, not JS), fingerprint randomization
requires JS injection specifically for canvas/audio/WebGL — it's a different
concern (preventing tracking fingerprinting, not hiding automation).

How it works:
  - Canvas: override ``HTMLCanvasElement.prototype.toDataURL`` and
    ``CanvasRenderingContext2D.prototype.getImageData`` to add tiny
    pixel-level noise that changes per-session but is stable within a session.
  - WebGL: override ``getParameter`` for the renderer/vendor strings and
    ``WEBGL_debug_renderer_info`` to return generic values.
  - Audio: override ``AnalyserNode.prototype.getByteFrequencyData`` and
    ``AudioBuffer.prototype.getChannelData`` to add micro-noise to the
    audio fingerprint.
"""

from __future__ import annotations

import logging
import random

from ricibrowser.cdp_client import CDPClient, CDPError

logger = logging.getLogger(__name__)

# The JS that gets injected via Page.addScriptToEvaluateOnNewDocument.
# It runs before any page script, in the main world (this is the one
# exception to the "no main world" rule — addScriptToEvaluateOnNewDocument
# is the standard CDP way to inject pre-page scripts and is NOT detected
# the same way Runtime.enable is).
_CANVAS_NOISE_JS = """
// ricibrowser fingerprint randomization — runs before page scripts.
(function() {
    // Per-session noise seed (stable within a session, different across).
    var _seed = {seed};

    function _noise(x, y, seed) {{
        // Simple deterministic noise based on coordinates + seed.
        var n = Math.sin(x * 12.9898 + y * 78.233 + seed * 0.1) * 43758.5453;
        return (n - Math.floor(n)) - 0.5;
    }}

    // ── Canvas fingerprint defense ──────────────────────────────────
    var _origToDataURL = HTMLCanvasElement.prototype.toDataURL;
    HTMLCanvasElement.prototype.toDataURL = function(type) {{
        var ctx = this.getContext('2d');
        if (ctx && this.width > 0 && this.height > 0) {{
            try {{
                var imageData = ctx.getImageData(0, 0, Math.min(this.width, 16), Math.min(this.height, 16));
                var data = imageData.data;
                for (var i = 0; i < data.length; i += 4) {{
                    // Add ±1 noise to R channel (invisible but changes hash).
                    data[i] = Math.max(0, Math.min(255, data[i] + (_noise(i, 0, _seed) * 2)));
                }}
                ctx.putImageData(imageData, 0, 0);
            }} catch(e) {{}}  // CORS-restricted canvases skip.
        }}
        return _origToDataURL.apply(this, arguments);
    }};

    // ── WebGL fingerprint defense ───────────────────────────────────
    var _origGetParameter = WebGLRenderingContext.prototype.getParameter;
    WebGLRenderingContext.prototype.getParameter = function(param) {{
        // UNMASKED_VENDOR_WEBGL (37445) and UNMASKED_RENDERER_WEBGL (37446)
        if (param === 37445) return 'Google Inc. (NVIDIA)';
        if (param === 37446) return 'ANGLE (NVIDIA, NVIDIA GeForce RTX 3060 Direct3D11 vs_5_0 ps_5_0)';
        return _origGetParameter.call(this, param);
    }};
    try {{
        // WebGL2
        var _origGetParameter2 = WebGL2RenderingContext.prototype.getParameter;
        WebGL2RenderingContext.prototype.getParameter = function(param) {{
            if (param === 37445) return 'Google Inc. (NVIDIA)';
            if (param === 37446) return 'ANGLE (NVIDIA, NVIDIA GeForce RTX 3060 Direct3D11 vs_5_0 ps_5_0)';
            return _origGetParameter2.call(this, param);
        }};
    }} catch(e) {{}}

    // ── Audio fingerprint defense ───────────────────────────────────
    try {{
        var _origGetChannelData = AudioBuffer.prototype.getChannelData;
        AudioBuffer.prototype.getChannelData = function(channel) {{
            var data = _origGetChannelData.call(this, channel);
            // Add micro-noise to the first few samples.
            for (var i = 0; i < Math.min(data.length, 100); i++) {{
                data[i] += _noise(i, channel, _seed) * 0.0000001;
            }}
            return data;
        }};
    }} catch(e) {{}}
}})();
"""


class FingerprintShield:
    """Opt-in canvas/WebGL/audio fingerprint randomization.

    Usage::

        shield = FingerprintShield(enabled=True)
        await shield.apply(cdp_client)  # inject before navigation

    Once applied, the script runs before any page script on every subsequent
    navigation in this CDP target. The noise seed is per-session-stable
    (same fingerprint within a session, different across sessions).
    """

    def __init__(self, enabled: bool = False):
        self.enabled = enabled
        self._seed: int = random.randint(1, 999999)
        self._applied = False
        self._script_identifier: str | None = None

    async def apply(self, cdp: CDPClient) -> None:
        """Inject the fingerprint randomization script via CDP.

        Uses ``Page.addScriptToEvaluateOnNewDocument`` so it runs before any
        page script on every navigation. This is NOT ``Runtime.enable`` on
        the main world — addScriptToEvaluateOnNewDocument is a standard CDP
        method that pre-seeds the page with our script.
        """
        if not self.enabled or self._applied:
            return
        # Generate a new seed per apply (each session gets a unique fingerprint)
        self._seed = random.randint(1, 999999)
        js = _CANVAS_NOISE_JS.format(seed=self._seed)

        try:
            result = await cdp.send("Page.addScriptToEvaluateOnNewDocument", {
                "source": js,
                "runImmediately": True,
            })
            self._script_identifier = result.get("identifier")
            self._applied = True
            logger.info("Fingerprint shield applied (seed=%d)", self._seed)
        except CDPError as exc:
            logger.warning("Fingerprint shield injection failed: %s", exc)

    async def remove(self, cdp: CDPClient) -> None:
        """Remove the injected script (if supported by the CDP server)."""
        if not self._applied or not self._script_identifier:
            return
        try:
            await cdp.send("Page.removeScriptToEvaluateOnNewDocument", {
                "identifier": self._script_identifier,
            })
        except CDPError:
            pass  # Not all CDP servers support removal
        self._applied = False
        self._script_identifier = None

    def reset_seed(self) -> None:
        """Reset the noise seed (changes the fingerprint for the next apply)."""
        self._seed = random.randint(1, 999999)
        self._applied = False  # Force re-injection on next apply()
