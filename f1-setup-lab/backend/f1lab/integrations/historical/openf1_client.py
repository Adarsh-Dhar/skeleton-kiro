"""OpenF1 HTTP client: rate limited, disk cached, retrying. Synchronous; call it from a worker thread.

OpenF1 (https://openf1.org) serves historical Formula 1 data from 2023 onwards for free and without authentication.
The service documents no hard request quota, so the client stays conservative: at most 3 requests per second and 30 per
minute, a retry with back-off on 429/5xx (honouring ``Retry-After``), and every non-empty answer is cached on disk so an
identical request is never sent twice.
"""

from __future__ import annotations

import hashlib
import json
import ssl
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from collections.abc import Callable, Sequence
from pathlib import Path
from urllib.parse import quote

from f1lab.integrations.historical.importer import (
    INTERNAL,
    NETWORK_ERROR,
    NETWORK_TIMEOUT,
    SESSION_UNAVAILABLE,
    HistoricalImportError,
    ImportCancelled,
)

BASE_URL = "https://api.openf1.org/v1"
MIN_YEAR = 2023  # OpenF1 historical coverage starts with the 2023 season
USER_AGENT = "f1-setup-lab (historical analysis; +https://openf1.org)"
RETRY_STATUSES = (429, 500, 502, 503, 504)
MAX_RETRY_AFTER_S = 60.0

Fetch = Callable[[str, float], "tuple[int, dict[str, str], bytes]"]  # (url, timeout) -> (status, headers, body)


class OpenF1Error(HistoricalImportError):
    """An OpenF1 request failed. ``code`` is one of the importer error codes."""

    def __init__(self, message: str, code: str) -> None:
        super().__init__(code, message)


class RateLimiter:
    """Sliding-window limiter: at most ``per_second`` requests in any second and ``per_minute`` in any minute."""

    def __init__(self, per_second: int = 3, per_minute: int = 30, clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self.per_second, self.per_minute = per_second, per_minute
        self._clock, self._sleep = clock, sleep
        self._stamps: deque[float] = deque()
        self._lock = threading.Lock()

    def acquire(self, cancel: Callable[[], bool] | None = None) -> None:
        """Block until a request may be sent, then record it. Raises ``ImportCancelled`` if ``cancel()`` turns true."""
        while True:
            if cancel and cancel():
                raise ImportCancelled("import cancelled")
            with self._lock:
                now = self._clock()
                while self._stamps and now - self._stamps[0] >= 60.0:
                    self._stamps.popleft()
                wait = 0.0
                if len(self._stamps) >= self.per_minute:
                    wait = max(wait, 60.0 - (now - self._stamps[0]))
                recent = [t for t in self._stamps if now - t < 1.0]
                if len(recent) >= self.per_second:
                    wait = max(wait, 1.0 - (now - recent[0]))
                if wait <= 0:
                    self._stamps.append(now)
                    return
            self._sleep(min(wait, 0.5) if cancel else wait)  # short naps keep cancellation responsive


def _ssl_context() -> ssl.SSLContext:
    """Verified TLS using certifi's CA bundle when installed (python.org builds on macOS ship without system CAs)."""
    try:
        import certifi

        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:  # pragma: no cover - certifi comes with requests (a FastF1 dependency)
        return ssl.create_default_context()


def _urllib_fetch(url: str, timeout: float) -> tuple[int, dict[str, str], bytes]:
    req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_ssl_context()) as resp:  # noqa: S310 - fixed https base URL
            return resp.status, {k.lower(): v for k, v in resp.headers.items()}, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, {k.lower(): v for k, v in (exc.headers or {}).items()}, exc.read() or b""


def build_url(endpoint: str, filters: dict[str, object], ranges: Sequence[tuple[str, str, str]] = ()) -> str:
    """``filters`` become ``key=value``; ``ranges`` are ``(field, operator, value)`` such as ``("date", ">=", "...")``."""
    parts = [f"{k}={quote(str(v), safe=':-.')}" for k, v in sorted(filters.items())]
    parts += [f"{f}{op}{quote(str(v), safe=':-.')}" for f, op, v in ranges]
    return f"{BASE_URL}/{endpoint}" + ("?" + "&".join(parts) if parts else "")


def cache_stats(cache_dir: str | Path) -> dict:
    root = Path(cache_dir)
    files = n = 0
    if root.is_dir():
        for p in root.rglob("*.json"):
            files += 1
            n += p.stat().st_size
    return {"files": files, "bytes": n, "dir": str(root)}


class OpenF1Client:
    def __init__(self, cache_dir: str | Path, fetch: Fetch | None = None, limiter: RateLimiter | None = None, retries: int = 4,
                 backoff_s: float = 2.0, timeout_s: float = 30.0, sleep: Callable[[float], None] = time.sleep) -> None:
        self.cache_dir = Path(cache_dir)
        self._fetch = fetch or _urllib_fetch
        self.limiter = limiter or RateLimiter()
        self.retries, self.backoff_s, self.timeout_s, self._sleep = retries, backoff_s, timeout_s, sleep
        self.network_requests = 0
        self.cache_hits = 0

    # ------------------------------------------------------------------ cache
    def _cache_path(self, url: str) -> Path:
        h = hashlib.sha256(url.encode()).hexdigest()
        return self.cache_dir / h[:2] / f"{h}.json"

    def _cache_read(self, url: str) -> list | None:
        p = self._cache_path(url)
        try:
            data = json.loads(p.read_text())
        except (OSError, ValueError):
            return None
        return data if isinstance(data, list) else None

    def _cache_write(self, url: str, rows: list) -> None:
        p = self._cache_path(url)
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            tmp = p.with_suffix(".tmp")
            tmp.write_text(json.dumps(rows))
            tmp.replace(p)
        except OSError:
            pass  # a cache that cannot be written only costs a repeat download

    # ------------------------------------------------------------------ requests
    def _pause(self, seconds: float, cancel: Callable[[], bool] | None) -> None:
        left = seconds
        while left > 0:
            if cancel and cancel():
                raise ImportCancelled("import cancelled")
            nap = min(left, 0.5)
            self._sleep(nap)
            left -= nap

    def get(self, endpoint: str, filters: dict[str, object], ranges: Sequence[tuple[str, str, str]] = (),
            cancel: Callable[[], bool] | None = None) -> list[dict]:
        """GET one endpoint and return its rows. Empty answers (also HTTP 404 "no results") return ``[]``."""
        url = build_url(endpoint, filters, ranges)
        cached = self._cache_read(url)
        if cached is not None:
            self.cache_hits += 1
            return cached
        last: OpenF1Error | None = None
        for attempt in range(self.retries + 1):
            self.limiter.acquire(cancel)
            self.network_requests += 1
            try:
                status, headers, body = self._fetch(url, self.timeout_s)
            except TimeoutError as exc:
                last = OpenF1Error(f"OpenF1 timed out ({endpoint}): {exc}", NETWORK_TIMEOUT)
            except OSError as exc:
                last = OpenF1Error(f"could not reach OpenF1 ({endpoint}): {exc}", NETWORK_ERROR)
            else:
                if status == 200:
                    rows = self._rows(endpoint, body)
                    if rows:  # empty results are not cached: the data may simply not be published yet
                        self._cache_write(url, rows)
                    return rows
                if status == 404 and b"no results" in body.lower():
                    return []
                if status not in RETRY_STATUSES:
                    raise OpenF1Error(f"OpenF1 {endpoint} returned HTTP {status}: {body[:200].decode('utf-8', 'replace')}", SESSION_UNAVAILABLE)
                last = OpenF1Error(f"OpenF1 {endpoint} returned HTTP {status} after {attempt + 1} attempt(s)",
                                   NETWORK_ERROR if status >= 500 else SESSION_UNAVAILABLE)
                retry_after = headers.get("retry-after", "")
                if attempt < self.retries:
                    try:
                        wait = min(float(retry_after), MAX_RETRY_AFTER_S) if retry_after else self.backoff_s * 2 ** attempt
                    except ValueError:
                        wait = self.backoff_s * 2 ** attempt
                    self._pause(wait, cancel)
                continue
            if attempt < self.retries:
                self._pause(self.backoff_s * 2 ** attempt, cancel)
        assert last is not None
        raise last

    @staticmethod
    def _rows(endpoint: str, body: bytes) -> list[dict]:
        try:
            data = json.loads(body)
        except ValueError as exc:
            raise OpenF1Error(f"OpenF1 {endpoint} returned invalid JSON", INTERNAL) from exc
        if isinstance(data, list):
            return [r for r in data if isinstance(r, dict)]
        if isinstance(data, dict) and "no results" in str(data.get("detail", "")).lower():
            return []
        raise OpenF1Error(f"OpenF1 {endpoint} returned an unexpected response: {str(data)[:200]}", SESSION_UNAVAILABLE)
