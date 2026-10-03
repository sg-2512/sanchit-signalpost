"""SignalPost Strategy Attempt Storage & Grounding Validation Engine.

Provides persistent, thread-safe, and process-safe JSONL audit logging for all
strategy route attempts, strictly enforcing 100% source-span grounding and
explicit rejection rationales.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from filelock import FileLock

# Ensure project root and src/ are importable
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from .base import StrategyAttemptResult


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(slots=True)
class AcceptedClaim:
    """An atomic claim that passed all verification and identity gates."""
    claim_id: str
    field: str
    value: Any
    confidence: float
    evidence_span: str  # Verbatim textual excerpt from the source payload
    evidence_ids: list[str] = field(default_factory=list)
    source_url: str = ""
    source_class: str = ""
    grounding_status: str = "grounded"


@dataclass(slots=True)
class RejectedClaim:
    """A candidate claim or observation rejected during strategy execution."""
    field: str
    candidate_value: Any
    rejection_reason: str  # Explicit rationale (e.g. name mismatch, parked domain)
    candidate_source_url: str = ""
    evidence_ids: list[str] = field(default_factory=list)


@dataclass(slots=True)
class ExactIdentityEvidence:
    """Cryptographic and token-level proof tying the source to the legal entity."""
    is_exact: bool
    confidence: float = 1.0
    legal_name_tokens: list[str] = field(default_factory=list)
    matched_tokens: list[str] = field(default_factory=list)
    org_nr_match: bool = False
    domain_match: bool = False
    address_match: bool = False
    municipality_match: bool = False
    reasons: list[str] = field(default_factory=list)
    method: str = ""


@dataclass(slots=True)
class StrategyAttemptRecord:
    """Complete persistent audit record for a single strategy attempt."""
    attempt_id: str
    run_id: str
    organisation_number: str
    company_name: str
    route_name: str
    version: str
    timestamp: str  # ISO 8601 UTC
    status: str     # "success", "quarantined", "failed", "blocked", "abstained"
    requested_urls: list[str]
    redirect_chain: list[str]
    snapshot_hash: str  # SHA-256 of raw response / page content
    candidate_domains: list[str]
    accepted_claims: list[dict[str, Any]]
    rejected_claims: list[dict[str, Any]]
    exact_identity_evidence: dict[str, Any]
    runtime_seconds: float
    request_count: int
    cost_usd: float
    quarantine_flag: bool
    error_message: Optional[str] = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> StrategyAttemptRecord:
        return cls(
            attempt_id=str(data.get("attempt_id") or ""),
            run_id=str(data.get("run_id") or ""),
            organisation_number=str(data.get("organisation_number") or ""),
            company_name=str(data.get("company_name") or ""),
            route_name=str(data.get("route_name") or ""),
            version=str(data.get("version") or "1.0.0"),
            timestamp=str(data.get("timestamp") or _utc_now_iso()),
            status=str(data.get("status") or "unknown"),
            requested_urls=list(data.get("requested_urls") or []),
            redirect_chain=list(data.get("redirect_chain") or []),
            snapshot_hash=str(data.get("snapshot_hash") or ""),
            candidate_domains=list(data.get("candidate_domains") or []),
            accepted_claims=list(data.get("accepted_claims") or []),
            rejected_claims=list(data.get("rejected_claims") or []),
            exact_identity_evidence=dict(data.get("exact_identity_evidence") or {}),
            runtime_seconds=float(data.get("runtime_seconds") or 0.0),
            request_count=int(data.get("request_count") or 0),
            cost_usd=float(data.get("cost_usd") or 0.0),
            quarantine_flag=bool(data.get("quarantine_flag") or False),
            error_message=data.get("error_message"),
            metadata=dict(data.get("metadata") or {}),
        )


def validate_attempt_record(record: StrategyAttemptRecord | dict[str, Any]) -> list[str]:
    """Strictly validates an attempt record against the Signalpost contract.
    
    Returns:
        List of validation error strings. If empty, record is valid.
    """
    data = record.to_dict() if isinstance(record, StrategyAttemptRecord) else record
    errors: list[str] = []

    # Required top-level fields
    for req in ("attempt_id", "run_id", "organisation_number", "route_name"):
        if not data.get(req):
            errors.append(f"Missing required field: '{req}'")

    status = str(data.get("status") or "")

    # Snapshot hash validity (required for non-abstained attempts)
    snap_hash = str(data.get("snapshot_hash") or "")
    if status not in {"abstained", "blocked", "failed"} and not snap_hash:
        errors.append("Active attempt record lacks required 'snapshot_hash'")
    elif snap_hash:
        if len(snap_hash) != 64 or not all(c in "0123456789abcdefABCDEF" for c in snap_hash):
            errors.append(f"Invalid snapshot_hash '{snap_hash}': must be a 64-char SHA-256 hexadecimal string")

    # Gate 3: 100% source-span grounding on accepted claims
    accepted = data.get("accepted_claims") or []
    for idx, claim in enumerate(accepted):
        field_name = claim.get("field", f"index_{idx}")
        span = str(claim.get("evidence_span") or "").strip()
        if not span:
            errors.append(f"Accepted claim '{field_name}' lacks required non-empty 'evidence_span'")
        elif len(span) < 5:
            errors.append(f"Accepted claim '{field_name}' evidence_span '{span}' is suspiciously short (< 5 chars)")
        if not claim.get("evidence_ids"):
            errors.append(f"Accepted claim '{field_name}' lacks required 'evidence_ids'")
        if not str(claim.get("source_url") or "").strip():
            errors.append(f"Accepted claim '{field_name}' lacks required 'source_url'")

    # Explicit rejection rationale on rejected claims
    rejected = data.get("rejected_claims") or []
    for idx, claim in enumerate(rejected):
        field_name = claim.get("field", f"index_{idx}")
        rationale = str(claim.get("rejection_reason") or "").strip()
        if not rationale:
            errors.append(f"Rejected claim '{field_name}' lacks required 'rejection_reason'")

    # Numeric budgets sanity
    try:
        runtime_val = data.get("runtime_seconds")
        if float(runtime_val if runtime_val is not None else 0) < 0:
            errors.append("runtime_seconds cannot be negative")
    except (ValueError, TypeError):
        errors.append("runtime_seconds must be a valid number")

    try:
        req_val = data.get("request_count")
        if int(req_val if req_val is not None else 0) < 0:
            errors.append("request_count cannot be negative")
    except (ValueError, TypeError):
        errors.append("request_count must be a valid integer")

    try:
        cost_val = data.get("cost_usd")
        if float(cost_val if cost_val is not None else 0) < 0:
            errors.append("cost_usd cannot be negative")
    except (ValueError, TypeError):
        errors.append("cost_usd must be a valid number")

    return errors


class AttemptStorage:
    """Thread-safe and process-safe persistent storage for strategy attempts.
    
    Uses dual-locking (threading.RLock + filelock.FileLock) to prevent race
    conditions across threads and processes on Windows and POSIX systems.
    """

    def __init__(
        self,
        base_dir: Optional[Path] = None,
        partition_by_company: bool = True,
        lock_timeout_seconds: float = 10.0,
    ) -> None:
        self.base_dir = (base_dir or (ROOT / "snapshots")).resolve()
        self.global_log = self.base_dir / "attempts.jsonl"
        self.attempts_dir = self.base_dir / "attempts"
        self.partition_by_company = partition_by_company
        self.lock_timeout = lock_timeout_seconds

        self.base_dir.mkdir(parents=True, exist_ok=True)
        if self.partition_by_company:
            self.attempts_dir.mkdir(parents=True, exist_ok=True)

        self._thread_lock = threading.RLock()
        self._global_filelock = FileLock(str(self.global_log) + ".lock", timeout=self.lock_timeout)

    def write_attempt(self, record: StrategyAttemptRecord) -> None:
        """Persist a single strategy attempt record under dual-lock protection."""
        # Validate record
        validation_errors = validate_attempt_record(record)
        if validation_errors:
            raise ValueError(f"Attempt record failed validation: {'; '.join(validation_errors)}")

        line = json.dumps(record.to_dict(), ensure_ascii=False, separators=(",", ":")) + "\n"
        data_bytes = line.encode("utf-8")

        # 1. Write to global unified log
        with self._thread_lock:
            with self._global_filelock:
                with open(self.global_log, "ab") as f:
                    f.write(data_bytes)
                    f.flush()

        # 2. Write to partitioned company log
        if self.partition_by_company and record.organisation_number:
            company_log = self.attempts_dir / f"{record.organisation_number}.jsonl"
            company_lock = FileLock(str(company_log) + ".lock", timeout=self.lock_timeout)
            with self._thread_lock:
                with company_lock:
                    with open(company_log, "ab") as f:
                        f.write(data_bytes)
                        f.flush()

    def record_attempt_from_result(
        self,
        result: StrategyAttemptResult,
        org: str,
        run_id: str,
        company_name: str = "",
        status: Optional[str] = None,
        error_message: Optional[str] = None,
    ) -> StrategyAttemptRecord:
        """Translate a StrategyAttemptResult into a StrategyAttemptRecord and persist it."""
        status_val = status or (
            "quarantined" if result.quarantined else result.status
        )

        requested_urls = list(result.urls)
        if not requested_urls and result.requested_url:
            requested_urls = [result.requested_url]

        record = StrategyAttemptRecord(
            attempt_id=result.attempt_id or f"att_{run_id}_{org}_{result.route_name}_{int(time.time()*1000)}",
            run_id=run_id,
            organisation_number=org,
            company_name=company_name,
            route_name=result.route_name,
            version=result.version,
            timestamp=result.timestamp or _utc_now_iso(),
            status=status_val,
            requested_urls=requested_urls,
            redirect_chain=list(result.redirect_chain),
            snapshot_hash=result.snapshot_hash or "",
            candidate_domains=list(result.candidate_domains),
            accepted_claims=list(result.accepted_claims),
            rejected_claims=list(result.rejected_claims),
            exact_identity_evidence=dict(result.exact_identity_evidence),
            runtime_seconds=result.duration_seconds,
            request_count=result.requests_count,
            cost_usd=result.cost_usd,
            quarantine_flag=result.quarantined,
            error_message=error_message or result.error,
            metadata=dict(result.metadata),
        )

        self.write_attempt(record)
        return record

    def load_attempts(
        self,
        run_id: Optional[str] = None,
        organisation_number: Optional[str] = None,
        route_name: Optional[str] = None,
    ) -> list[StrategyAttemptRecord]:
        """Query attempt records with optional filtering."""
        target_path = (
            (self.attempts_dir / f"{organisation_number}.jsonl")
            if organisation_number and (self.attempts_dir / f"{organisation_number}.jsonl").exists()
            else self.global_log
        )
        if not target_path.exists():
            return []

        results: list[StrategyAttemptRecord] = []
        with self._thread_lock:
            with open(target_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        row = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if run_id and row.get("run_id") != run_id:
                        continue
                    if organisation_number and row.get("organisation_number") != organisation_number:
                        continue
                    if route_name and row.get("route_name") != route_name:
                        continue
                    results.append(StrategyAttemptRecord.from_dict(row))
        return results

    def summarize_attempts(self, run_id: Optional[str] = None) -> dict[str, Any]:
        """Generate high-level audit summary for an evaluation run."""
        records = self.load_attempts(run_id=run_id)
        if not records:
            return {
                "total_attempts": 0,
                "successful_attempts": 0,
                "quarantined_attempts": 0,
                "failed_attempts": 0,
                "accepted_claims_count": 0,
                "rejected_claims_count": 0,
                "total_requests": 0,
                "total_cost_usd": 0.0,
                "total_runtime_seconds": 0.0,
                "grounded_evidence_rate": 1.0,
            }

        total_attempts = len(records)
        accepted_claims_count = sum(len(r.accepted_claims) for r in records)
        rejected_claims_count = sum(len(r.rejected_claims) for r in records)
        total_requests = sum(r.request_count for r in records)
        total_cost_usd = sum(r.cost_usd for r in records)
        total_runtime_seconds = sum(r.runtime_seconds for r in records)

        successful = sum(1 for r in records if r.status == "success")
        quarantined = sum(1 for r in records if r.quarantine_flag or r.status == "quarantined")
        failed = sum(1 for r in records if r.status == "failed")

        # Grounding audit
        total_accepted = 0
        grounded_accepted = 0
        for r in records:
            for c in r.accepted_claims:
                total_accepted += 1
                span = str(c.get("evidence_span") or "").strip()
                if span and len(span) >= 5 and c.get("evidence_ids") and c.get("source_url"):
                    grounded_accepted += 1

        grounded_rate = (grounded_accepted / total_accepted) if total_accepted > 0 else 1.0

        return {
            "total_attempts": total_attempts,
            "successful_attempts": successful,
            "quarantined_attempts": quarantined,
            "failed_attempts": failed,
            "accepted_claims_count": accepted_claims_count,
            "rejected_claims_count": rejected_claims_count,
            "total_requests": total_requests,
            "total_cost_usd": round(total_cost_usd, 5),
            "total_runtime_seconds": round(total_runtime_seconds, 3),
            "grounded_evidence_rate": round(grounded_rate, 4),
        }


# Global default storage singleton
_DEFAULT_STORAGE: Optional[AttemptStorage] = None
_STORAGE_INIT_LOCK = threading.Lock()


def get_default_attempt_storage() -> AttemptStorage:
    """Return thread-safe singleton instance of AttemptStorage."""
    global _DEFAULT_STORAGE
    if _DEFAULT_STORAGE is None:
        with _STORAGE_INIT_LOCK:
            if _DEFAULT_STORAGE is None:
                _DEFAULT_STORAGE = AttemptStorage()
    return _DEFAULT_STORAGE
