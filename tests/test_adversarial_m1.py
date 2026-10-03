"""Adversarial Stress Test Suite for Milestone 1 (SignalPost Strategies Harness).

Author: Empirical Challenger M1-1
Roles: critic, specialist

Stress-tests:
1. Concurrency Stress: High-contention multi-threaded writes to AttemptStorage (16 threads,
   partitioned & global logs, concurrent reader/writer contention).
2. StrategyRegistry Boundary Stress: Budget exhausted, simulated HTTP timeouts, malformed
   HTML / JSON-LD payloads, missing optional keys in org payload, and uncaught exception isolation.
3. Grounding Validator Stress: Strict rejection of empty strings, short spans (<5 chars),
   missing URLs, invalid snapshot hashes, and edge-case whitespace/non-numeric inputs.
"""
from __future__ import annotations

import concurrent.futures
import json
import shutil
import tempfile
import time
import unittest
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from strategies.base import (
    AttemptStatus,
    ExecutionContext,
    RouteCategory,
    StrategyAttemptResult,
    StrategyRoute,
)
from strategies.registry import (
    DEFAULT_REGISTRY,
    JsonLdOpenGraphRoute,
    LeaderBridgeRoute,
    PdfFallbackRoute,
    RegistrySiteRoute,
    StaticHomepageRoute,
    StrategyRegistry,
    build_default_registry,
)
from strategies.attempts import (
    AttemptStorage,
    StrategyAttemptRecord,
    validate_attempt_record,
)


class ConcurrencyStressTests(unittest.TestCase):
    """Area 1: Concurrency and contention stress testing for AttemptStorage."""

    def setUp(self) -> None:
        self.temp_dir = Path(tempfile.mkdtemp(prefix="stress_storage_"))
        self.storage = AttemptStorage(
            base_dir=self.temp_dir,
            partition_by_company=True,
            lock_timeout_seconds=20.0,
        )

    def tearDown(self) -> None:
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_high_contention_16_threads_writing_simultaneously(self) -> None:
        """Verify 16 threads writing simultaneously causes zero line corruption or lost writes."""
        num_threads = 16
        writes_per_thread = 20
        total_expected = num_threads * writes_per_thread

        def worker(thread_idx: int) -> list[str]:
            written_ids = []
            for i in range(writes_per_thread):
                orgnr = f"99900{thread_idx % 4}"
                att_id = f"att_th{thread_idx}_{i}"
                rec = StrategyAttemptRecord(
                    attempt_id=att_id,
                    run_id="run_stress_16",
                    organisation_number=orgnr,
                    company_name=f"Company {thread_idx}",
                    route_name="static_homepage",
                    version="1.0.0",
                    timestamp="2026-10-02T15:00:00Z",
                    status="success",
                    requested_urls=["https://example.com"],
                    redirect_chain=["https://example.com"],
                    snapshot_hash="a" * 64,
                    candidate_domains=["example.com"],
                    accepted_claims=[{
                        "claim_id": f"c_{thread_idx}_{i}",
                        "field": "test_field",
                        "value": "val",
                        "confidence": 1.0,
                        "evidence_span": "Valid evidence span >= 5 chars",
                        "evidence_ids": ["ev-1"],
                        "source_url": "https://example.com",
                    }],
                    rejected_claims=[],
                    exact_identity_evidence={"is_exact": True},
                    runtime_seconds=0.05,
                    request_count=1,
                    cost_usd=0.0,
                    quarantine_flag=False,
                )
                self.storage.write_attempt(rec)
                written_ids.append(att_id)
            return written_ids

        with concurrent.futures.ThreadPoolExecutor(max_workers=num_threads) as executor:
            futures = [executor.submit(worker, t) for t in range(num_threads)]
            all_ids = []
            for f in futures:
                all_ids.extend(f.result())

        self.assertEqual(len(all_ids), total_expected)

        # 1. Verify global log exists and has exact line count
        self.assertTrue(self.storage.global_log.exists())
        with open(self.storage.global_log, "r", encoding="utf-8") as f:
            lines = [line.strip() for line in f if line.strip()]

        self.assertEqual(len(lines), total_expected, f"Expected {total_expected} lines, got {len(lines)}")

        # 2. Verify every line is valid JSON with zero corruption
        parsed_records = []
        for idx, line in enumerate(lines):
            try:
                data = json.loads(line)
                parsed_records.append(data)
            except json.JSONDecodeError as exc:
                self.fail(f"Line {idx} corrupted: {exc}\nLine content: {line[:100]}")

        # 3. Verify all attempt_ids are unique and present
        saved_ids = {r["attempt_id"] for r in parsed_records}
        self.assertEqual(len(saved_ids), total_expected)
        self.assertEqual(saved_ids, set(all_ids))

        # 4. Verify company partition logs match
        total_partition_lines = 0
        for orgnr_idx in range(4):
            part_file = self.storage.attempts_dir / f"99900{orgnr_idx}.jsonl"
            self.assertTrue(part_file.exists(), f"Partition file for 99900{orgnr_idx} missing")
            with open(part_file, "r", encoding="utf-8") as f:
                p_lines = [l.strip() for l in f if l.strip()]
                total_partition_lines += len(p_lines)
        self.assertEqual(total_partition_lines, total_expected)

    def test_concurrent_readers_and_writers_no_deadlock(self) -> None:
        """Verify concurrent readers (load_attempts/summarize) and writers do not deadlock."""
        num_writers = 12
        num_readers = 6
        writes_per_thread = 15
        total_expected = num_writers * writes_per_thread

        def writer(thread_idx: int) -> None:
            for i in range(writes_per_thread):
                rec = StrategyAttemptRecord(
                    attempt_id=f"rw_att_{thread_idx}_{i}",
                    run_id="run_rw_stress",
                    organisation_number="998877665",
                    company_name="RW Corp",
                    route_name="sitemap_static",
                    version="1.0.0",
                    timestamp="2026-10-02T15:00:00Z",
                    status="success",
                    requested_urls=["https://rw.no"],
                    redirect_chain=["https://rw.no"],
                    snapshot_hash="b" * 64,
                    candidate_domains=["rw.no"],
                    accepted_claims=[{
                        "claim_id": f"c_{thread_idx}_{i}",
                        "field": "rw_field",
                        "value": "rw_val",
                        "confidence": 1.0,
                        "evidence_span": "Evidence span for RW Corp",
                        "evidence_ids": ["ev-rw"],
                        "source_url": "https://rw.no",
                    }],
                    rejected_claims=[],
                    exact_identity_evidence={"is_exact": True},
                    runtime_seconds=0.01,
                    request_count=1,
                    cost_usd=0.0,
                    quarantine_flag=False,
                )
                self.storage.write_attempt(rec)

        def reader(reader_idx: int) -> None:
            for _ in range(8):
                self.storage.load_attempts(run_id="run_rw_stress")
                self.storage.summarize_attempts(run_id="run_rw_stress")
                time.sleep(0.005)

        with concurrent.futures.ThreadPoolExecutor(max_workers=num_writers + num_readers) as executor:
            w_futs = [executor.submit(writer, w) for w in range(num_writers)]
            r_futs = [executor.submit(reader, r) for r in range(num_readers)]
            for fut in w_futs + r_futs:
                fut.result(timeout=15.0)

        summary = self.storage.summarize_attempts(run_id="run_rw_stress")
        self.assertEqual(summary["total_attempts"], total_expected)

    def test_vulnerability_record_attempt_from_result_missing_time_import(self) -> None:
        """VERIFY FIX: record_attempt_from_result generates attempt_id when empty without NameError."""
        result_without_attempt_id = StrategyAttemptResult(
            attempt_id="",  # Route run() functions return empty attempt_id!
            organisation_number="985589003",
            route_name="static_homepage",
            version="1.0.0",
            status=AttemptStatus.ABSTAINED.value,
        )

        record = self.storage.record_attempt_from_result(
            result=result_without_attempt_id,
            org="985589003",
            run_id="run_bug_verify",
        )
        self.assertTrue(record.attempt_id.startswith("att_run_bug_verify_985589003_static_homepage_"))


class StrategyRegistryBoundaryStressTests(unittest.TestCase):
    """Area 2: Boundary stress testing on StrategyRegistry and routes."""

    def setUp(self) -> None:
        self.registry = build_default_registry()

    def test_budget_exhausted_blocks_eligible_routes(self) -> None:
        """When budget.can_proceed() returns False, eligible routes must return BLOCKED status."""
        mock_budget = MagicMock()
        mock_budget.can_proceed.return_value = False

        context = ExecutionContext(
            run_id="run_budget_exhausted",
            budget=mock_budget,
            timeout=5.0,
        )
        org = {
            "organisation_number": "985589003",
            "name": "TEST AS",
            "website": "https://test.no",
        }

        result = self.registry.execute_route("registry_site", org, context)
        self.assertEqual(result.status, AttemptStatus.BLOCKED.value)
        self.assertEqual(result.error, "budget_exhausted")
        self.assertEqual(result.requests_count, 0)

    def test_simulated_http_timeout_isolated_as_failed(self) -> None:
        """Simulated HTTP/socket timeout in route execution must be isolated as FAILED status."""
        class TimeoutSimulatingRoute(StrategyRoute):
            name = "timeout_sim"
            version = "1.0.0"

            def can_execute(self, org: dict, context: ExecutionContext) -> tuple[bool, str | None]:
                return True, None

            def run(self, org: dict, context: ExecutionContext) -> StrategyAttemptResult:
                raise TimeoutError("HTTP request timed out after 15.0 seconds")

        test_registry = StrategyRegistry()
        test_registry.register(TimeoutSimulatingRoute())

        ctx = ExecutionContext(run_id="run_timeout_test", timeout=0.1)
        res = test_registry.execute_route("timeout_sim", {"organisation_number": "123"}, ctx)

        self.assertEqual(res.status, AttemptStatus.FAILED.value)
        self.assertIn("TimeoutError", res.error or "")
        self.assertIn("timed out", res.error or "")

    def test_malformed_html_and_jsonld_graceful_handling(self) -> None:
        """JsonLdOpenGraphRoute must handle broken, unparseable JSON-LD without crashing."""
        malformed_html = """
        <!DOCTYPE html>
        <html>
        <head>
            <script type="application/ld+json">
                { "unclosed": "string, "broken": [1, 2, }
            </script>
            <meta property="og:title" content="Malformed Test">
        </head>
        <body>Broken DOM &lt;&gt;&amp;</body>
        </html>
        """
        route = JsonLdOpenGraphRoute()
        ctx = ExecutionContext(
            run_id="run_malformed_jsonld",
            shared_artifacts={
                "homepage_url": "https://example.com",
                "homepage_html": malformed_html,
            },
        )
        result = route.execute({"organisation_number": "999888777"}, ctx)
        self.assertEqual(result.status, AttemptStatus.SUCCESS.value)
        self.assertIsInstance(result.accepted_claims, list)

    def test_empty_org_payload_all_routes_return_abstained_or_fallback(self) -> None:
        """Empty org dict {} should not cause uncaught exceptions on any of the 11 routes."""
        ctx = ExecutionContext(run_id="run_empty_org")
        for route_name in self.registry.list_route_names():
            try:
                res = self.registry.execute_route(route_name, {}, ctx)
                self.assertIn(
                    res.status,
                    {AttemptStatus.ABSTAINED.value, AttemptStatus.SUCCESS.value, AttemptStatus.FAILED.value},
                    f"Route {route_name} returned unexpected status {res.status}",
                )
            except Exception as exc:
                self.fail(f"Route '{route_name}' crashed with unhandled {type(exc).__name__}: {exc}")

    def test_vulnerability_none_evidence_crashes_can_execute(self) -> None:
        """VERIFY FIX: Passing evidence=None does not crash registry_site, leader_bridge, or pdf_fallback."""
        ctx = ExecutionContext(run_id="run_evidence_none")
        org_with_none_evidence = {"organisation_number": "123456789", "evidence": None}

        for route_name in ["registry_site", "leader_bridge", "pdf_fallback"]:
            res = self.registry.execute_route(route_name, org_with_none_evidence, ctx)
            self.assertIn(
                res.status,
                {AttemptStatus.ABSTAINED.value, AttemptStatus.FAILED.value, AttemptStatus.SUCCESS.value},
            )

    def test_vulnerability_none_org_crashes_execute(self) -> None:
        """VERIFY FIX: Passing org=None does not crash execute() on routes."""
        ctx = ExecutionContext(run_id="run_none_org")
        res = self.registry.execute_route("registry_site", None, ctx)  # type: ignore
        self.assertIn(
            res.status,
            {AttemptStatus.ABSTAINED.value, AttemptStatus.FAILED.value, AttemptStatus.SUCCESS.value},
        )


class GroundingValidatorStressTests(unittest.TestCase):
    """Area 3: Strictness stress testing for validate_attempt_record."""

    def _make_valid_record_dict(self) -> dict:
        return {
            "attempt_id": "att_valid_001",
            "run_id": "run_001",
            "organisation_number": "985589003",
            "company_name": "Valid AS",
            "route_name": "static_homepage",
            "version": "1.0.0",
            "status": "success",
            "requested_urls": ["https://valid.no"],
            "redirect_chain": ["https://valid.no"],
            "snapshot_hash": "e" * 64,
            "candidate_domains": ["valid.no"],
            "accepted_claims": [{
                "field": "company_mission",
                "value": "Clean energy systems",
                "confidence": 0.95,
                "evidence_span": "Clean energy systems for the future",
                "evidence_ids": ["ev-1"],
                "source_url": "https://valid.no",
            }],
            "rejected_claims": [],
            "exact_identity_evidence": {"is_exact": True},
            "runtime_seconds": 0.1,
            "request_count": 1,
            "cost_usd": 0.0,
            "quarantine_flag": False,
        }

    def test_valid_record_passes_cleanly(self) -> None:
        rec = self._make_valid_record_dict()
        errors = validate_attempt_record(rec)
        self.assertEqual(errors, [])

    def test_rejects_empty_and_whitespace_evidence_span(self) -> None:
        rec = self._make_valid_record_dict()
        rec["accepted_claims"][0]["evidence_span"] = ""
        errors = validate_attempt_record(rec)
        self.assertTrue(any("non-empty 'evidence_span'" in e for e in errors))

        rec["accepted_claims"][0]["evidence_span"] = "     "
        errors = validate_attempt_record(rec)
        self.assertTrue(any("non-empty 'evidence_span'" in e for e in errors))

        rec["accepted_claims"][0]["evidence_span"] = None
        errors = validate_attempt_record(rec)
        self.assertTrue(any("non-empty 'evidence_span'" in e for e in errors))

    def test_rejects_suspiciously_short_evidence_span(self) -> None:
        rec = self._make_valid_record_dict()
        for short_span in ["a", "ab", "abc", "abcd"]:
            rec["accepted_claims"][0]["evidence_span"] = short_span
            errors = validate_attempt_record(rec)
            self.assertTrue(
                any("suspiciously short (< 5 chars)" in e for e in errors),
                f"Failed to reject short span '{short_span}'",
            )

        # 5 characters should pass length check
        rec["accepted_claims"][0]["evidence_span"] = "abcde"
        errors = validate_attempt_record(rec)
        self.assertEqual(errors, [])

    def test_rejects_missing_source_url(self) -> None:
        rec = self._make_valid_record_dict()
        rec["accepted_claims"][0]["source_url"] = ""
        errors = validate_attempt_record(rec)
        self.assertTrue(any("lacks required 'source_url'" in e for e in errors))

        rec["accepted_claims"][0]["source_url"] = None
        errors = validate_attempt_record(rec)
        self.assertTrue(any("lacks required 'source_url'" in e for e in errors))

    def test_vulnerability_whitespace_source_url_not_rejected(self) -> None:
        """VERIFY FIX: Whitespace-only source_url is properly rejected."""
        rec = self._make_valid_record_dict()
        rec["accepted_claims"][0]["source_url"] = "   "
        errors = validate_attempt_record(rec)
        self.assertTrue(any("lacks required 'source_url'" in e for e in errors))

    def test_rejects_missing_evidence_ids(self) -> None:
        rec = self._make_valid_record_dict()
        rec["accepted_claims"][0]["evidence_ids"] = []
        errors = validate_attempt_record(rec)
        self.assertTrue(any("lacks required 'evidence_ids'" in e for e in errors))

        rec["accepted_claims"][0]["evidence_ids"] = None
        errors = validate_attempt_record(rec)
        self.assertTrue(any("lacks required 'evidence_ids'" in e for e in errors))

    def test_rejects_missing_rejection_reason_on_rejected_claims(self) -> None:
        rec = self._make_valid_record_dict()
        rec["rejected_claims"] = [{
            "field": "unverified_social",
            "candidate_value": "https://fb.com/fake",
            "rejection_reason": "",  # Empty!
        }]
        errors = validate_attempt_record(rec)
        self.assertTrue(any("lacks required 'rejection_reason'" in e for e in errors))

        rec["rejected_claims"][0]["rejection_reason"] = "   "  # Whitespace!
        errors = validate_attempt_record(rec)
        self.assertTrue(any("lacks required 'rejection_reason'" in e for e in errors))

    def test_rejects_invalid_snapshot_hash(self) -> None:
        rec = self._make_valid_record_dict()
        rec["snapshot_hash"] = "z" * 64  # Non-hex characters
        errors = validate_attempt_record(rec)
        self.assertTrue(any("must be a 64-char SHA-256 hexadecimal string" in e for e in errors))

        rec["snapshot_hash"] = "a" * 63  # 63 chars (wrong length)
        errors = validate_attempt_record(rec)
        self.assertTrue(any("must be a 64-char SHA-256 hexadecimal string" in e for e in errors))

        rec["snapshot_hash"] = ""  # Missing on active success status
        errors = validate_attempt_record(rec)
        self.assertTrue(any("Active attempt record lacks required 'snapshot_hash'" in e for e in errors))

    def test_vulnerability_non_numeric_budget_crashes_validator(self) -> None:
        """VERIFY FIX: Non-numeric runtime_seconds appends validation error instead of crashing with uncaught ValueError."""
        rec = self._make_valid_record_dict()
        rec["runtime_seconds"] = "not_a_float"
        errors = validate_attempt_record(rec)
        self.assertTrue(any("runtime_seconds must be a valid number" in e for e in errors))


if __name__ == "__main__":
    unittest.main()
