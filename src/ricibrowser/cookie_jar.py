"""Persistent cookie + localStorage jar — survives browser restarts.

Saves to JSON on disk so authenticated sessions (and cf_clearance cookies)
persist across browser sessions. Critical for scanning behind login and for
re-using solved Cloudflare challenges.
"""

from __future__ import annotations

import json
import logging
import os
import time
import threading
import copy
from contextlib import contextmanager
from typing import Any

logger = logging.getLogger(__name__)


# ``flock`` protects separate Python processes.  The per-path lock also keeps
# the fallback (platforms without fcntl) safe between CookieJar instances in
# one interpreter.
_path_locks_guard = threading.Lock()
_path_locks: dict[str, threading.RLock] = {}


def _path_lock(path: str) -> threading.RLock:
    with _path_locks_guard:
        return _path_locks.setdefault(path, threading.RLock())


def _norm_domain(domain: str) -> str:
    """Normalize a cookie domain: strip leading dot for dedup keys."""
    return domain.lstrip(".") if domain else ""


def _cookie_key(cookie: dict[str, Any]) -> tuple[str, str, str]:
    return (
        _norm_domain(str(cookie.get("domain", ""))),
        str(cookie.get("path", "/") or "/"),
        str(cookie.get("name", "")),
    )


def _cookie_changed(before: dict[str, Any], after: dict[str, Any]) -> bool:
    """Compare fields whose changes represent browser-side cookie mutation.

    Chrome adds several default fields when ``Network.getCookies`` reads back a
    cookie supplied through ``Network.setCookies``.  Comparing whole dicts
    would therefore mark an untouched stale cookie as an update and could
    resurrect it after another process logged out.  Values and explicit expiry
    or policy changes are the durable state that matters here.
    """
    fields = ("value", "expires", "secure", "httpOnly", "sameSite")
    return any(before.get(field) != after.get(field) for field in fields)


class CookieJar:
    """Persistent cookie + localStorage jar backed by a JSON file.

    Format::

        {
            "cookies": [
                {"name": "session", "value": "abc", "domain": ".example.com", ...},
                ...
            ],
            "storage": {
                "https://example.com": {"key": "value", ...},
                ...
            }
        }
    """

    def __init__(self, path: str | None = None):
        raw_path = path or "~/.config/ricibrowser/cookies.json"
        self.path = os.path.abspath(os.path.expanduser(raw_path)) if raw_path else raw_path
        self._data: dict[str, Any] = {"cookies": [], "storage": {}}
        self._lock = threading.RLock()
        self._baseline: dict[str, Any] = copy.deepcopy(self._data)
        self._cookie_upserts: dict[tuple[str, str, str], dict[str, Any]] = {}
        self._cookie_deletes: set[tuple[str, str, str]] = set()
        self._storage_updates: dict[str, dict[str, str]] = {}
        self._storage_deletes: set[str] = set()
        self._clear_all = False

    @contextmanager
    def _disk_lock(self):
        """Hold a cross-instance/process lock for a disk transaction."""
        if not self.path:
            yield
            return
        lock_path = self.path + ".lock"
        directory = os.path.dirname(lock_path) or "."
        os.makedirs(directory, exist_ok=True)
        local_lock = _path_lock(lock_path)
        with local_lock:
            fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
            try:
                try:
                    import fcntl
                    fcntl.flock(fd, fcntl.LOCK_EX)
                except (ImportError, OSError):
                    # The process-local lock above remains useful on platforms
                    # without fcntl.  ricibrowser's release/runtime platforms
                    # use flock, so cross-process behavior is covered there.
                    pass
                yield
            finally:
                try:
                    import fcntl
                    fcntl.flock(fd, fcntl.LOCK_UN)
                except (ImportError, OSError):
                    pass
                os.close(fd)

    @staticmethod
    def _normalise_data(value: Any) -> dict[str, Any]:
        if not isinstance(value, dict):
            return {"cookies": [], "storage": {}}
        cookies = value.get("cookies")
        storage = value.get("storage")
        return {
            "cookies": list(cookies) if isinstance(cookies, list) else [],
            "storage": dict(storage) if isinstance(storage, dict) else {},
        }

    def _read_disk(self) -> dict[str, Any]:
        if not self.path or not os.path.exists(self.path):
            return {"cookies": [], "storage": {}}
        with open(self.path, encoding="utf-8") as file:
            return self._normalise_data(json.load(file))

    def _reset_mutations(self) -> None:
        self._cookie_upserts.clear()
        self._cookie_deletes.clear()
        self._storage_updates.clear()
        self._storage_deletes.clear()
        self._clear_all = False

    def load(self) -> None:
        """Load the jar from disk (no-op if file doesn't exist)."""
        if not self.path or not os.path.exists(self.path):
            return
        try:
            with self._lock, self._disk_lock():
                self._data = self._read_disk()
                self._gc()
                self._baseline = copy.deepcopy(self._data)
                self._reset_mutations()
        except Exception as exc:
            logger.warning("CookieJar load failed: %s", exc)
            with self._lock:
                self._data = {"cookies": [], "storage": {}}
                self._baseline = copy.deepcopy(self._data)
                self._reset_mutations()

    def save(self) -> None:
        """Merge pending changes into disk under a process lock and save.

        The old implementation atomically replaced a stale whole-file snapshot:
        two Engines could each save successfully while the second silently
        erased the first one's cookies.  This is an atomic read/modify/write
        transaction.  Only changes relative to this instance's loaded baseline
        are applied, which also prevents an untouched stale cookie from
        resurrecting a logout performed by another process.
        """
        if not self.path:
            return
        with self._lock:
            self._gc()
            self._infer_direct_mutations()
            dir_path = os.path.dirname(self.path) or "."
            os.makedirs(dir_path, exist_ok=True)
            with self._disk_lock():
                try:
                    latest = self._read_disk()
                except Exception as exc:
                    logger.warning("CookieJar could not read latest state before save: %s", exc)
                    latest = {"cookies": [], "storage": {}}

                if self._clear_all:
                    latest = {"cookies": [], "storage": {}}

                cookies = {_cookie_key(c): c for c in latest.get("cookies", []) if isinstance(c, dict)}
                for key in self._cookie_deletes:
                    cookies.pop(key, None)
                cookies.update(copy.deepcopy(self._cookie_upserts))

                storage = dict(latest.get("storage", {}))
                for origin in self._storage_deletes:
                    storage.pop(origin, None)
                storage.update(copy.deepcopy(self._storage_updates))

                merged = {"cookies": list(cookies.values()), "storage": storage}
                self._write_atomic(merged, dir_path)
                self._data = merged
                self._baseline = copy.deepcopy(merged)
                self._reset_mutations()

    def _write_atomic(self, data: dict[str, Any], directory: str) -> None:
        import tempfile
        fd, tmp = tempfile.mkstemp(dir=directory, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as file:
                json.dump(data, file, indent=2, default=str)
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.path)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def _infer_direct_mutations(self) -> None:
        """Honor callers that historically assigned ``_data`` directly.

        Public mutators already record intent.  This comparison preserves the
        small amount of existing private-attribute usage without turning every
        unchanged item in a stale snapshot into an upsert.
        """
        before = {_cookie_key(c): c for c in self._baseline.get("cookies", []) if isinstance(c, dict)}
        after = {_cookie_key(c): c for c in self._data.get("cookies", []) if isinstance(c, dict)}
        for key in before.keys() - after.keys():
            self._cookie_deletes.add(key)
            self._cookie_upserts.pop(key, None)
        for key, cookie in after.items():
            if key not in before or cookie != before[key]:
                self._cookie_upserts[key] = copy.deepcopy(cookie)
                self._cookie_deletes.discard(key)

        before_storage = self._baseline.get("storage", {})
        after_storage = self._data.get("storage", {})
        for origin in before_storage.keys() - after_storage.keys():
            self._storage_deletes.add(origin)
            self._storage_updates.pop(origin, None)
        for origin, values in after_storage.items():
            if origin not in before_storage or values != before_storage[origin]:
                self._storage_updates[origin] = copy.deepcopy(values)
                self._storage_deletes.discard(origin)

    def _gc(self) -> None:
        """Remove expired cookies. Runs on load and before save."""
        now = time.time()
        cookies = self._data.get("cookies", [])
        if not cookies:
            return
        alive = []
        purged = 0
        for c in cookies:
            expires = c.get("expires")
            if expires and isinstance(expires, (int, float)) and expires > 0:
                if expires < now:
                    purged += 1
                    continue
            alive.append(c)
        if purged:
            self._data["cookies"] = alive
            logger.debug("CookieJar GC: purged %d expired, %d remain", purged, len(alive))

    @property
    def cookies(self) -> list[dict]:
        with self._lock:
            return copy.deepcopy(self._data.get("cookies", []))

    @property
    def storage(self) -> dict[str, dict[str, str]]:
        """Return a copy of all origin-scoped localStorage state."""
        with self._lock:
            return copy.deepcopy(self._data.get("storage", {}))

    def set_cookies(self, cookies: list[dict]) -> None:
        """Replace all cookies."""
        with self._lock:
            old = {_cookie_key(c) for c in self.cookies if isinstance(c, dict)}
            new = {_cookie_key(c): copy.deepcopy(c) for c in cookies if isinstance(c, dict)}
            self._cookie_deletes.update(old - new.keys())
            for key in old - new.keys():
                self._cookie_upserts.pop(key, None)
            for key, cookie in new.items():
                self._cookie_upserts[key] = cookie
                self._cookie_deletes.discard(key)
            self._data["cookies"] = list(new.values())

    def update_cookies(self, cookies: list[dict]) -> None:
        """Merge new cookies by (name, normalized_domain).

        Normalizes leading-dot domains so ``.example.com`` and ``example.com``
        are treated as the same logical cookie (per RFC 6265, the leading dot
        denotes a domain cookie matching all subdomains).
        """
        with self._lock:
            existing = {
                _cookie_key(c): c
                for c in self.cookies
            }
            for c in cookies:
                if not isinstance(c, dict):
                    continue
                key = _cookie_key(c)
                expires = c.get("expires")
                if isinstance(expires, (int, float)) and 0 < expires <= time.time():
                    existing.pop(key, None)
                    self._cookie_deletes.add(key)
                    self._cookie_upserts.pop(key, None)
                    continue
                existing[key] = copy.deepcopy(c)
                self._cookie_upserts[key] = copy.deepcopy(c)
                self._cookie_deletes.discard(key)
            self._data["cookies"] = list(existing.values())

    def sync_cookies(self, cookies: list[dict], baseline: list[dict] | None = None) -> None:
        """Reconcile a browser snapshot, including cookie deletions.

        ``baseline`` is what was loaded into that browser session.  Only
        browser-side differences are recorded.  This distinction is what lets
        an unchanged stale Engine coexist with another Engine that logged out:
        the stale Engine does not re-upsert cookies it never changed.
        """
        with self._lock:
            base = {
                _cookie_key(c): c for c in (baseline if baseline is not None else self.cookies)
                if isinstance(c, dict)
            }
            live = {_cookie_key(c): c for c in cookies if isinstance(c, dict)}
            current = {_cookie_key(c): c for c in self.cookies if isinstance(c, dict)}

            for key in base.keys() - live.keys():
                current.pop(key, None)
                self._cookie_deletes.add(key)
                self._cookie_upserts.pop(key, None)
            for key, cookie in live.items():
                if key not in base or _cookie_changed(base[key], cookie):
                    current[key] = copy.deepcopy(cookie)
                    self._cookie_upserts[key] = copy.deepcopy(cookie)
                    self._cookie_deletes.discard(key)
            self._data["cookies"] = list(current.values())

    def get_cookies_for_domain(self, domain: str) -> list[dict]:
        """Get cookies matching a domain (handles leading dot).

        Uses proper suffix-boundary matching: ``.example.com`` matches
        ``example.com`` and ``www.example.com`` but NOT ``notexample.com``.
        """
        result = []
        with self._lock:
            cookies = copy.deepcopy(self._data.get("cookies", []))
        for c in cookies:
            cdomain = c.get("domain", "")
            if _norm_domain(cdomain) == domain:
                result.append(c)
            elif cdomain.startswith("."):
                bare = cdomain[1:]
                if domain == bare or domain.endswith(cdomain):
                    result.append(c)
        return result

    def get_storage(self, origin: str) -> dict:
        """Get localStorage for an origin."""
        with self._lock:
            return copy.deepcopy(self._data.get("storage", {}).get(origin, {}))

    def set_storage(self, origin: str, storage: dict) -> None:
        """Set localStorage for an origin."""
        with self._lock:
            value = {str(k): str(v) for k, v in storage.items()}
            self._data.setdefault("storage", {})[origin] = value
            self._storage_updates[origin] = copy.deepcopy(value)
            self._storage_deletes.discard(origin)

    def sync_storage(
        self, origin: str, storage: dict[str, str], baseline: dict[str, str] | None = None,
    ) -> None:
        """Record localStorage only when it changed from the session baseline."""
        value = {str(k): str(v) for k, v in storage.items()}
        base = {str(k): str(v) for k, v in (baseline or {}).items()}
        with self._lock:
            if value == base:
                return
            self._data.setdefault("storage", {})[origin] = value
            self._storage_updates[origin] = copy.deepcopy(value)
            self._storage_deletes.discard(origin)

    def delete_storage(self, origin: str) -> None:
        """Remove an origin record entirely (distinct from an empty store)."""
        with self._lock:
            self._data.setdefault("storage", {}).pop(origin, None)
            self._storage_deletes.add(origin)
            self._storage_updates.pop(origin, None)

    def clear(self) -> None:
        """Clear all cookies and storage."""
        with self._lock:
            self._data = {"cookies": [], "storage": {}}
            self._cookie_upserts.clear()
            self._cookie_deletes.clear()
            self._storage_updates.clear()
            self._storage_deletes.clear()
            self._clear_all = True
