from ricibrowser.stealth_benchmark import analyze_observation, browser_probe_script


def test_benchmark_reports_high_risk_automation_signal():
    report = analyze_observation({"webdriver": True, "cdp_artifact": True})
    assert report.score <= 60
    assert {signal.name for signal in report.failures} == {"navigator.webdriver", "cdp_artifact"}


def test_benchmark_reports_consistent_observation_cleanly():
    report = analyze_observation({
        "webdriver": False, "user_agent": True, "client_hints_consistent": True,
        "locale_timezone_consistent": True, "webgl_available": True,
        "canvas_stable": True, "audio_stable": True, "plugins_realistic": True,
        "cdp_artifact": False, "tls_consistent": True,
    })
    assert report.score == 100
    assert report.failures == []


def test_benchmark_marks_missing_signals_as_unobserved():
    report = analyze_observation({})
    assert report.score == 100
    assert all(signal.observed is None for signal in report.signals)


def test_probe_script_is_read_only_and_bounded():
    script = browser_probe_script()
    assert "navigator.webdriver" in script
    assert "fetch(" not in script
    assert "XMLHttpRequest" not in script


def test_benchmark_is_explicitly_not_a_detection_guarantee():
    report = analyze_observation({}).to_dict()
    assert "not proof of invisibility" in report["limitation"]
