# Changelog

## 0.2.30 — 2026-11-09

- Creates a distinct owned page target for each Chrome Session/fast browse and
  serializes startup and session admission against Engine close.
- Verifies owned process-group retirement, including surviving descendants,
  and retains process/profile ownership when shutdown or profile removal fails.
- Invalidates references/context/status on document replacement; records
  navigation/status provenance and evaluation exceptions on retry/fallback paths.
- Adds supported `snapshot_current_page()` and serializable capability metadata;
  Page JSON reports actual stealth and truthful unknown/error outcomes.
- Restores origin localStorage before application scripts, persists state on
  close, reconciles deletions and merges CookieJar writes under per-path/process
  locks without changing the existing disk schema.
- Retains bounded completed network metadata after capture stops, with session
  attribution and drop/omission diagnostics. Response bodies remain unsupported.
- Honors Chrome fast-browse wait options, uses the actual DOMContentLoaded event,
  and adds a bounded delayed URL-change window.
- Makes CAPTCHA result paths total and respects explicit auto-resolve disabling.

Compatibility: historical private ownership fields and entry points are retained.
Unknown in-memory HTTP status remains zero; serialized outcomes may now be
`partial`/`error`. Close can raise on incomplete persistence/retirement. Sessions
share one profile/auth identity despite distinct tabs. Cross-process CookieJar
locking is POSIX-specific; Lightpanda/platform live certification remains limited.
