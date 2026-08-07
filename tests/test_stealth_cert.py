"""Tests for TLS-certificate trust handling in Chrome launch flags.

A MITM intercept proxy (miniproxy/mitmproxy) presents its own self-signed CA,
so Chrome rejects every HTTPS response with NET::ERR_CERT_AUTHORITY_INVALID
unless launched with --ignore-certificate-errors. ``get_stealth_args`` enables
that automatically whenever a proxy is configured, and lets callers override.
"""

from ricibrowser.stealth import get_stealth_args

_CERT_FLAG = "--ignore-certificate-errors"
_TEST_TYPE = "--test-type"
_INSECURE_LH = "--allow-insecure-localhost"


def test_proxy_auto_enables_cert_bypass():
    """A configured proxy auto-trusts its CA (the MITM case)."""
    args = get_stealth_args(proxy="http://127.0.0.1:43893")
    assert _CERT_FLAG in args
    assert _TEST_TYPE in args  # required for --headless=new to honour the flag
    assert _INSECURE_LH in args
    assert "--proxy-server=http://127.0.0.1:43893" in args


def test_no_proxy_preserves_cert_validation():
    """Ordinary (non-proxied) browsing keeps full certificate validation."""
    args = get_stealth_args()
    assert _CERT_FLAG not in args


def test_explicit_false_overrides_proxy_auto():
    """An explicit False wins even when a proxy is set."""
    args = get_stealth_args(proxy="http://x:1", ignore_cert_errors=False)
    assert _CERT_FLAG not in args


def test_explicit_true_without_proxy():
    """An explicit True enables the bypass with no proxy."""
    args = get_stealth_args(ignore_cert_errors=True)
    assert _CERT_FLAG in args
    assert _TEST_TYPE in args


def test_no_duplicate_flags_when_present_in_extra():
    """Flags supplied via ``extra`` are not duplicated by the auto logic."""
    args = get_stealth_args(
        proxy="http://x:1",
        extra=[_TEST_TYPE, _CERT_FLAG, _INSECURE_LH],
    )
    assert args.count(_CERT_FLAG) == 1
    assert args.count(_TEST_TYPE) == 1
    assert args.count(_INSECURE_LH) == 1


def test_bypass_applies_to_both_stealth_and_basic():
    """Cert bypass is independent of the stealth/basic flag profile."""
    for stealth in (True, False):
        args = get_stealth_args(stealth=stealth, proxy="http://x:1")
        assert _CERT_FLAG in args, f"missing cert flag with stealth={stealth}"
