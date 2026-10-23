from __future__ import annotations

import json
import hashlib
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any


@dataclass
class FetchResult:
    url: str
    status: int
    elapsed_ms: int
    bytes_received: int
    body: Any = None
    error: str | None = None
    content_sha256: str | None = None
    retrieved_at: str | None = None
    effective_at: str | None = None


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def fetch_json(url: str, *, timeout: float = 20.0, attempts: int = 3) -> FetchResult:
    last_error = "request failed"
    for attempt in range(attempts):
        started = time.monotonic()
        request = urllib.request.Request(
            url,
            headers={"Accept": "application/json", "User-Agent": "builderr-signalpost-poc/0.1 (+https://builderr.ai)"},
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read()
                elapsed = int((time.monotonic() - started) * 1000)
                return FetchResult(url, response.status, elapsed, len(raw), json.loads(raw), content_sha256=hashlib.sha256(raw).hexdigest(), retrieved_at=_utc_now())
        except urllib.error.HTTPError as exc:
            elapsed = int((time.monotonic() - started) * 1000)
            raw = exc.read()
            if exc.code in {404, 410}:
                return FetchResult(url, exc.code, elapsed, len(raw), error=f"HTTP {exc.code}", content_sha256=hashlib.sha256(raw).hexdigest(), retrieved_at=_utc_now())
            if exc.code == 429:
                retry_after = exc.headers.get("Retry-After") if hasattr(exc, "headers") and exc.headers else None
                sleep_s = None
                if retry_after:
                    try:
                        sleep_s = float(retry_after)
                    except (ValueError, TypeError):
                        pass
                wait_time = min(max(sleep_s, 1.0), 15.0) if sleep_s is not None else min(2.0 * (2**attempt), 15.0)
                time.sleep(wait_time)
                last_error = f"HTTP 429 (backed off {wait_time:.1f}s)"
                continue
            last_error = f"HTTP {exc.code}"
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {str(exc)[:120]}"
        if attempt + 1 < attempts:
            time.sleep(0.4 * (2**attempt))
    return FetchResult(url, 0, 0, 0, error=last_error, retrieved_at=_utc_now())
