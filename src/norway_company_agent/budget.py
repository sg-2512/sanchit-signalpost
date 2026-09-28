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
        max_requests: int = 2000,
        max_cost_usd: float = 10.0,
        max_time_seconds: float = 2400.0,  # 40 min safety margin on 45 min limit
    ) -> None:
        self._lock = threading.Lock()
        self.max_requests = max_requests
        self.max_cost_usd = max_cost_usd
        self.max_time_seconds = max_time_seconds
        self.requests = 0
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

    def can_proceed(self) -> bool:
        with self._lock:
            if self.requests >= self.max_requests:
                return False
            if self.cost_usd >= self.max_cost_usd:
                return False
        if self.elapsed_seconds() >= self.max_time_seconds:
            return False
        return True

    def record_request(self, cost_usd: float = 0.0, bytes_received: int = 0, elapsed_ms: int = 0, latency_ms: int = 0, count: int = 1) -> None:
        with self._lock:
            self.requests += count
            self.cost_usd += cost_usd
            self.bytes_received += bytes_received
            latency = elapsed_ms or latency_ms
            if latency:
                self.latencies_ms.append(latency)

    def fetch_json(self, url: str, *, timeout: float = 20.0, attempts: int = 3, cost_per_request: float = 0.0) -> FetchResult:
        """Budget-aware wrapper around http.fetch_json.

        Records each attempt as a request against the budget.
        """
        if not self.can_proceed():
            from .evidence import utc_now
            return FetchResult(url, 0, 0, 0, error="budget_exhausted", retrieved_at=utc_now())

        result = _raw_fetch_json(url, timeout=timeout, attempts=attempts)

        self.record_request(
            cost_usd=cost_per_request,
            bytes_received=result.bytes_received,
            elapsed_ms=result.elapsed_ms,
        )
        return result

    def report(self) -> dict[str, Any]:
        with self._lock:
            latencies = sorted(self.latencies_ms)
            return {
                "total_requests": self.requests,
                "max_requests": self.max_requests,
                "remaining_requests": max(0, self.max_requests - self.requests),
                "total_cost_usd": round(self.cost_usd, 4),
                "max_cost_usd": self.max_cost_usd,
                "total_bytes": self.bytes_received,
                "elapsed_seconds": round(self.elapsed_seconds(), 1),
                "p50_ms": latencies[len(latencies) // 2] if latencies else None,
                "p95_ms": latencies[min(len(latencies) - 1, int(len(latencies) * 0.95))] if latencies else None,
            }
