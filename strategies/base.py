"""SignalPost Strategy Base Abstraction.

Defines core data contracts, execution contexts, and lifecycle protocols
for Builderr's Signalpost learning harness and strategy registry.
"""
from __future__ import annotations

import hashlib
import sys
import time
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Optional

# Ensure project root and src/ are importable
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _utc_now_iso() -> str:
    """Return ISO 8601 UTC timestamp."""
    return datetime.now(timezone.utc).isoformat()


class AttemptStatus(str, Enum):
    """Lifecycle status for an individual strategy execution attempt."""
    SUCCESS = "success"
    ABSTAINED = "abstained"
    BLOCKED = "blocked"
    QUARANTINED = "quarantined"
    FAILED = "failed"


class RouteCategory(str, Enum):
    """Category classification for strategy routes."""
    OFFICIAL = "official"
    CRAWL = "crawl"
    STRUCTURED = "structured"
    DISCOVERY = "discovery"
    FALLBACK = "fallback"


@dataclass
class ExecutionContext:
    """Thread-safe execution context passed to all strategy routes.
    
    Includes run identifiers, atomic budget tracker, run-scoped shared artifact bus,
    and timeout configuration.
    """
    run_id: str
    budget: Any = None  # Duck-typed BudgetTracker
    timeout: float = 15.0
    options: dict[str, Any] = field(default_factory=dict)
    shared_artifacts: dict[str, Any] = field(default_factory=dict)
    cache_dir: Optional[Path] = None
    created_at: str = field(default_factory=_utc_now_iso)


@dataclass
class StrategyAttemptResult:
    """Standardized result of a single strategy route execution."""
    attempt_id: str
    organisation_number: str
    route_name: str
    version: str
    status: str  # AttemptStatus value
    requested_url: Optional[str] = None
    final_url: Optional[str] = None
    redirect_chain: list[str] = field(default_factory=list)
    http_status: Optional[int] = None
    snapshot_hash: Optional[str] = None  # 64-hex SHA-256 digest
    candidate_domains: list[str] = field(default_factory=list)
    accepted_claims: list[dict[str, Any]] = field(default_factory=list)
    rejected_claims: list[dict[str, Any]] = field(default_factory=list)
    exact_identity_evidence: dict[str, Any] = field(default_factory=dict)
    quarantined: bool = False
    quarantine_reason: Optional[str] = None
    requests_count: int = 0
    cost_usd: float = 0.0
    bytes_received: int = 0
    duration_seconds: float = 0.0
    error: Optional[str] = None
    metadata: dict[str, Any] = field(default_factory=dict)
    timestamp: str = field(default_factory=_utc_now_iso)

    @property
    def urls(self) -> list[str]:
        """Convenience property for list of requested URLs."""
        if self.requested_url:
            return [self.requested_url]
        return []

    def to_dict(self) -> dict[str, Any]:
        """Convert to JSON-serializable dictionary."""
        return {
            "attempt_id": self.attempt_id,
            "organisation_number": self.organisation_number,
            "route_name": self.route_name,
            "version": self.version,
            "status": self.status,
            "requested_url": self.requested_url,
            "final_url": self.final_url,
            "redirect_chain": list(self.redirect_chain),
            "http_status": self.http_status,
            "snapshot_hash": self.snapshot_hash,
            "candidate_domains": list(self.candidate_domains),
            "accepted_claims": [dict(c) for c in self.accepted_claims],
            "rejected_claims": [dict(c) for c in self.rejected_claims],
            "exact_identity_evidence": dict(self.exact_identity_evidence),
            "quarantined": self.quarantined,
            "quarantine_reason": self.quarantine_reason,
            "requests_count": self.requests_count,
            "cost_usd": round(self.cost_usd, 5),
            "bytes_received": self.bytes_received,
            "duration_seconds": round(self.duration_seconds, 3),
            "error": self.error,
            "metadata": dict(self.metadata),
            "timestamp": self.timestamp,
        }

    def to_attempt_log(self) -> dict[str, Any]:
        """Format for flat JSONL attempt audit logging conforming to Signalpost contract."""
        payload = self.to_dict()
        payload["runtime_ms"] = int(round(self.duration_seconds * 1000))
        return payload


class StrategyRoute(ABC):
    """Abstract Base Class for an executable strategy route."""
    name: str
    version: str = "1.0.0"
    description: str = ""
    category: RouteCategory = RouteCategory.CRAWL
    estimated_requests: int = 1
    estimated_cost_usd: float = 0.0
    enabled: bool = True

    @abstractmethod
    def can_execute(self, org: dict[str, Any], context: ExecutionContext) -> tuple[bool, Optional[str]]:
        """Check prerequisites, input validity, and API keys before running.
        
        Returns:
            (can_run, reason_if_cannot)
        """
        pass

    @abstractmethod
    def run(self, org: dict[str, Any], context: ExecutionContext) -> StrategyAttemptResult:
        """Execute route logic. Must be overridden by subclasses."""
        pass

    def execute(self, org: dict[str, Any], context: ExecutionContext) -> StrategyAttemptResult:
        """Protected execution wrapper with pre-checks, timing, budget guards, and exception isolation."""
        safe_org = org if isinstance(org, dict) else {}
        orgnr = str(safe_org.get("organisation_number") or "unknown")
        epoch_ms = int(time.time() * 1000)
        attempt_id = f"att-{orgnr}-{self.name}-{epoch_ms}"
        started = time.monotonic()

        try:
            # 1. Pre-execution check
            can_run, abstain_reason = self.can_execute(safe_org, context)
            if not can_run:
                return StrategyAttemptResult(
                    attempt_id=attempt_id,
                    organisation_number=orgnr,
                    route_name=self.name,
                    version=self.version,
                    status=AttemptStatus.ABSTAINED.value,
                    error=abstain_reason,
                    duration_seconds=0.0,
                )

            # 2. Budget check
            if context.budget is not None and hasattr(context.budget, "can_proceed"):
                if not context.budget.can_proceed():
                    return StrategyAttemptResult(
                        attempt_id=attempt_id,
                        organisation_number=orgnr,
                        route_name=self.name,
                        version=self.version,
                        status=AttemptStatus.BLOCKED.value,
                        error="budget_exhausted",
                        duration_seconds=0.0,
                    )

            # 3. Execution with isolation
            result = self.run(safe_org, context)
            result.duration_seconds = time.monotonic() - started
            if not result.attempt_id:
                result.attempt_id = attempt_id
            if not result.organisation_number:
                result.organisation_number = orgnr
            if not result.route_name:
                result.route_name = self.name
            if not result.version:
                result.version = self.version
            return result
        except Exception as exc:
            duration = time.monotonic() - started
            return StrategyAttemptResult(
                attempt_id=attempt_id,
                organisation_number=orgnr,
                route_name=self.name,
                version=self.version,
                status=AttemptStatus.FAILED.value,
                error=f"{type(exc).__name__}: {str(exc)}",
                duration_seconds=duration,
            )
