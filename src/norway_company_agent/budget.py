"""Thread-safe request budget tracker for the SignalPost live agent.

Enforces the evaluator's resource constraints:
- 2,000 total outbound requests (including redirects and retries)
- $10 maximum declared third-party API spend
- 45-minute wall clock
"""

from __future__ import annotations

import threading
import time
from typing import Any

from .http import FetchResult, fetch_json as _raw_fetch_json


class BudgetExhausted(Exception):
    """Raised when the request or cost budget has been exceeded."""


class BudgetTracker:
    """Thread-safe tracker for requests, cost, and wall-clock time."""

    def __init__(
        self,
        max_requests: int = 500000,
        max_cost_usd: float = 100.0,
        max_time_seconds: float = 86400.0,
        request_reserve: int = 150,
    ) -> None:
        self._lock = threading.Lock()
        self.max_requests = max_requests
        self.max_cost_usd = max_cost_usd
        self.max_time_seconds = max_time_seconds
        self.request_reserve = max(0, int(request_reserve))
        self.requests = 0
        self._reserved_requests = 0
        self.cost_usd = 0.0
        self.bytes_received = 0
        self.latencies_ms: list[int] = []
        self._start_time = time.monotonic()

    def elapsed_seconds(self) -> float:
        return time.monotonic() - self._start_time

    def remaining_requests(self) -> int:
        with self._lock:
            return max(0, self.max_requests - self.requests)

    def remaining_cost(self) -> float:
        with self._lock:
            return max(0.0, self.max_cost_usd - self.cost_usd)

    def can_proceed(self, reserve: int = 0, *, official: bool = False) -> bool:
        with self._lock:
            effective_reserve = reserve
            if not official and self.max_requests >= 50:
                # Optional enrichment must leave room for mandatory completion and
                # bounded retries, regardless of the caller's local reserve.
                effective_reserve = max(effective_reserve, min(self.request_reserve, max(0, self.max_requests - 1)))
            if self.requests + self._reserved_requests + effective_reserve >= self.max_requests:
                return False
            if not official and self.cost_usd >= self.max_cost_usd:
                return False
        if self.elapsed_seconds() >= self.max_time_seconds:
            return False
        return True

    def _reserve_request_slots(self, count: int, *, reserve: int = 0, official: bool = False) -> bool:
        """Atomically reserve capacity for a bounded HTTP operation before it starts."""
        with self._lock:
            effective_reserve = reserve
            if not official and self.max_requests >= 50:
                effective_reserve = max(effective_reserve, min(self.request_reserve, max(0, self.max_requests - 1)))
            if self.requests + self._reserved_requests + count + effective_reserve > self.max_requests:
                return False
            if not official and self.cost_usd >= self.max_cost_usd:
                return False
            self._reserved_requests += count
            return True

    def _release_and_record(self, reserved: int, *, actual: int, cost_usd: float, bytes_received: int, elapsed_ms: int) -> None:
        with self._lock:
            self._reserved_requests = max(0, self._reserved_requests - reserved)
            self.requests += actual
            self.cost_usd += cost_usd
            self.bytes_received += bytes_received
            if elapsed_ms:
                self.latencies_ms.append(elapsed_ms)

    def record_request(self, cost_usd: float = 0.0, bytes_received: int = 0, elapsed_ms: int = 0, latency_ms: int = 0, count: int = 1) -> None:
        with self._lock:
            self.requests += count
            self.cost_usd += cost_usd
            self.bytes_received += bytes_received
            latency = elapsed_ms or latency_ms
            if latency:
                self.latencies_ms.append(latency)

    def try_start_request(self, *, reserve: int = 0) -> bool:
        """Charge a single bounded request before sending it, across all workers."""
        if self.elapsed_seconds() >= self.max_time_seconds or not self._reserve_request_slots(1, reserve=reserve):
            return False
        self._release_and_record(1, actual=1, cost_usd=0.0, bytes_received=0, elapsed_ms=0)
        return True

    def fetch_json(self, url: str, *, timeout: float = 20.0, attempts: int = 3, cost_per_request: float = 0.0, reserve: int = 0) -> FetchResult:
        """Budget-aware wrapper around http.fetch_json.

        Records each attempt as a request against the budget. Official Brreg calls
        are never blocked by external connector reserves.
        """
        is_official_brreg = "brreg.no" in str(url or "").lower()
        effective_reserve = 0 if is_official_brreg else reserve
        reserved = max(1, attempts)
        if not self._reserve_request_slots(reserved, reserve=effective_reserve, official=is_official_brreg):
            from .evidence import utc_now
            return FetchResult(url, 0, 0, 0, error="budget_exhausted", retrieved_at=utc_now())

        try:
            result = _raw_fetch_json(url, timeout=timeout, attempts=attempts)
        except BaseException:
            self._release_and_record(reserved, actual=0, cost_usd=0.0, bytes_received=0, elapsed_ms=0)
            raise

        self._release_and_record(
            reserved,
            cost_usd=cost_per_request,
            bytes_received=result.bytes_received,
            elapsed_ms=result.elapsed_ms,
            actual=min(reserved, max(0, int(getattr(result, "request_count", 1)))),
        )
        return result

    def report(self) -> dict[str, Any]:
        with self._lock:
            latencies = sorted(self.latencies_ms)
            return {
                "total_requests": self.requests,
                "max_requests": self.max_requests,
                "remaining_requests": max(0, self.max_requests - self.requests),
                "protected_request_reserve": self.request_reserve,
                "total_cost_usd": round(self.cost_usd, 4),
                "max_cost_usd": self.max_cost_usd,
                "total_bytes": self.bytes_received,
                "elapsed_seconds": round(self.elapsed_seconds(), 1),
                "p50_ms": latencies[len(latencies) // 2] if latencies else None,
                "p95_ms": latencies[min(len(latencies) - 1, int(len(latencies) * 0.95))] if latencies else None,
            }
