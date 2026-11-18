from __future__ import annotations

import json
import hashlib
import socket
import ssl
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
    attempts_made = 0
    for attempt in range(attempts):
        attempts_made = attempt + 1
        started = time.monotonic()
        request = urllib.request.Request(
            url,
            headers={"Accept": "application/json", "User-Agent": "builderr-signalpost-poc/0.1 (+https://builderr.ai)"},
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read()
                elapsed = int((time.monotonic() - started) * 1000)
                result = FetchResult(response.geturl(), response.status, elapsed, len(raw), json.loads(raw), content_sha256=hashlib.sha256(raw).hexdigest(), retrieved_at=_utc_now())
                result.request_count = attempt + 1
                return result
        except urllib.error.HTTPError as exc:
            elapsed = int((time.monotonic() - started) * 1000)
            raw = exc.read()
            if exc.code in {404, 410}:
                # HTTP 404/410 is a recorded absence, never positive source
                # evidence. In particular, do not emit sha256(empty bytes)
                # as if a supporting official group document was retrieved.
                result = FetchResult(url, exc.code, elapsed, len(raw), error=f"HTTP {exc.code}", content_sha256=None, retrieved_at=_utc_now())
                result.request_count = attempt + 1
                return result
            if exc.code == 429:
                if attempt + 1 >= attempts:
                    last_error = "HTTP 429 (retry limit reached)"
                    break
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
            # Retry only transient upstream failures. Other 4xx responses are
            # deterministic for this request and waste the shared run budget.
            if not 500 <= exc.code < 600:
                break
        except urllib.error.URLError as exc:
            reason = getattr(exc, "reason", None)
            last_error = f"{type(exc).__name__}: {str(exc)[:120]}"
            # A DNS resolution failure cannot recover by repeating the same URL
            # within this company request; let later companies use the budget.
            if isinstance(reason, socket.gaierror):
                break
        except (socket.gaierror, ssl.SSLCertVerificationError) as exc:
            last_error = f"{type(exc).__name__}: {str(exc)[:120]}"
            break
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {str(exc)[:120]}"
        if attempt + 1 < attempts:
            time.sleep(0.4 * (2**attempt))
    result = FetchResult(url, 0, 0, 0, error=last_error, retrieved_at=_utc_now())
    result.request_count = attempts_made
    return result
