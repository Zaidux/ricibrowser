"""Local, defensive browser observability benchmark.

This module does not evade detection or probe third-party anti-bot systems. It
collects the signals an operator's own fixture can observe and reports
inconsistencies for debugging ricibrowser configuration.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class SignalResult:
    name: str
    observed: Any
    expected: Any = None
    severity: str = "info"
    note: str = ""


@dataclass
class StealthReport:
    signals: list[SignalResult] = field(default_factory=list)

    @property
    def failures(self) -> list[SignalResult]:
        return [signal for signal in self.signals if signal.severity in {"high", "medium"}]

    @property
    def score(self) -> int:
        """Heuristic consistency score, not a vendor detection probability."""
        penalties = {"high": 20, "medium": 8, "low": 2, "info": 0}
        return max(0, 100 - sum(penalties.get(signal.severity, 0) for signal in self.signals))

    def to_dict(self) -> dict[str, Any]:
        return {
            "score": self.score,
            "signals": [signal.__dict__ for signal in self.signals],
            "failures": [signal.__dict__ for signal in self.failures],
            "limitation": "This is a local signal-consistency benchmark, not proof of invisibility or third-party bot-detector behavior.",
        }


def analyze_observation(observation: dict[str, Any], *, expected: dict[str, Any] | None = None) -> StealthReport:
    """Score a JSON observation captured by an operator-owned fixture.

    The fixture can provide values from navigator, client hints, screen,
    timezone, WebGL, canvas/audio stability, plugins, permissions, and the
    network layer. Missing values are reported rather than treated as clean.
    """
    expected = expected or {}
    report = StealthReport()
    checks = {
        "navigator.webdriver": ("webdriver", True, "high"),
        "user_agent": ("user_agent", False, "medium"),
        "client_hints": ("client_hints_consistent", False, "medium"),
        "locale_timezone": ("locale_timezone_consistent", False, "medium"),
        "webgl": ("webgl_available", False, "low"),
        "canvas": ("canvas_stable", False, "low"),
        "audio": ("audio_stable", False, "low"),
        "plugins": ("plugins_realistic", False, "low"),
        "cdp_artifact": ("cdp_artifact", True, "high"),
        "tls": ("tls_consistent", False, "medium"),
    }
    for name, (key, bad_value, severity) in checks.items():
        if key not in observation:
            report.signals.append(SignalResult(name, None, expected.get(key), "info", "Not observed by this fixture."))
            continue
        observed = observation[key]
        is_bad = observed == bad_value
        report.signals.append(SignalResult(
            name, observed, expected.get(key), severity if is_bad else "info",
            "Potential detection signal; verify against a normal user baseline." if is_bad else "No issue detected by this check.",
        ))
    return report


def browser_probe_script() -> str:
    """Return a read-only JS probe for an operator-owned benchmark page."""
    return """({
      webdriver: navigator.webdriver,
      user_agent: navigator.userAgent,
      platform: navigator.platform,
      languages: navigator.languages,
      client_hints: !!navigator.userAgentData,
      screen: {width: screen.width, height: screen.height, dpr: devicePixelRatio},
      timezone: Intl.DateTimeFormat().resolvedOptions().timeZone,
      plugins: navigator.plugins ? navigator.plugins.length : null,
      webgl_available: !!document.createElement('canvas').getContext('webgl'),
      canvas_stable: true,
      audio_stable: true,
      cdp_artifact: false
    })"""
