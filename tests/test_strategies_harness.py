"""Comprehensive Test Suite for SignalPost Strategies and Evaluation Harness.

Validates:
1. All 11 Strategy Routes & Registry lifecycle (execution, priority, freeze).
2. Attempt Storage with dual-locking, partitioning, and Gate 3 grounding validation.
3. Freeze Manifest generation, CLI invocation, and tamper verification.
4. Hierarchical 6-Gate Promotion Engine (Gate 1 fatal rejection, Gates 2-6 checks, promotion).
5. Evaluation Benchmark Runner CLI and metrics calculation.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
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
    CANONICAL_PRIORITY,
    DEFAULT_REGISTRY,
    BrowserFallbackRoute,
    GooglePlacesRoute,
    JsonLdOpenGraphRoute,
    LeaderBridgeRoute,
    NavJobsRoute,
    PdfFallbackRoute,
    RegistrySiteRoute,
    SearchCandidatesRoute,
    SitemapStaticRoute,
    StaticHomepageRoute,
    StrategyRegistry,
    TargetedPathsRoute,
    build_default_registry,
)
from strategies.attempts import (
    AcceptedClaim,
    AttemptStorage,
    ExactIdentityEvidence,
    RejectedClaim,
    StrategyAttemptRecord,
    validate_attempt_record,
)
from strategies.freeze import (
    FreezeManifest,
    VerificationResult,
    compute_file_sha256,
    freeze_strategies,
    generate_version_table,
    verify_strategies,
)
from eval.promotion_gate import (
    GateResult,
    GateStatus,
    GateVerdict,
    PromotionVerdictCode,
    evaluate_promotion,
)
from eval.run import evaluate_benchmark


class StrategyRoutesAndRegistryTests(unittest.TestCase):
    """Tests for all 11 Strategy Routes and StrategyRegistry."""

    def setUp(self) -> None:
        self.context = ExecutionContext(
            run_id="test-run-001",
            budget=MagicMock(can_proceed=MagicMock(return_value=True)),
            timeout=5.0,
        )
        self.sample_org = {
            "organisation_number": "888567232",
            "name": "AAS ELEKTRONIKK AS",
            "legal_form": "AS",
            "municipality": "ARENDAL",
            "website": "www.aelektronikk.no",
            "employees": None,
            "evidence": {
                "roles": {
                    "value": {
                        "roles": [
                            {"name": "Knut Aas", "role_code": "LEDE", "role": "Styrets leder"},
                            {"name": "Knut Aas", "role_code": "DAGL", "role": "Daglig leder"},
                        ]
                    }
                }
            },
        }

    def test_registry_contains_all_11_routes(self) -> None:
        registry = build_default_registry()
        routes = registry.list_routes()
        self.assertEqual(len(routes), 11)
        names = registry.list_route_names()
        expected_names = [
            "registry_site",
            "static_homepage",
            "sitemap_static",
            "targeted_paths",
            "jsonld_opengraph",
            "search_candidates",
            "nav_jobs",
            "google_places",
            "leader_bridge",
            "browser_fallback",
            "pdf_fallback",
        ]
        self.assertEqual(names, expected_names)

    def test_registry_freeze_blocks_new_registration(self) -> None:
        registry = build_default_registry()
        freeze_info = registry.freeze()
        self.assertIn("registry_sha256", freeze_info)
        self.assertEqual(freeze_info["total_routes"], 11)
        self.assertEqual(freeze_info["status"], "frozen")

        # Attempting to register another route raises RuntimeError
        class DummyRoute(StrategyRoute):
            name = "dummy_route"
            def can_execute(self, org, ctx): return True, None
            def run(self, org, ctx): return StrategyAttemptResult(attempt_id="1", organisation_number="1", route_name=self.name, version="1", status="success")

        with self.assertRaises(RuntimeError):
            registry.register(DummyRoute())

    def test_route_1_registry_site_execution(self) -> None:
        route = RegistrySiteRoute()
        can_run, reason = route.can_execute(self.sample_org, self.context)
        self.assertTrue(can_run)
        self.assertIsNone(reason)

        result = route.execute(self.sample_org, self.context)
        self.assertEqual(result.status, AttemptStatus.SUCCESS.value)
        self.assertEqual(result.route_name, "registry_site")
        self.assertEqual(len(result.accepted_claims), 1)
        self.assertTrue(result.accepted_claims[0]["evidence_span"])
        self.assertIn("homepage_url", self.context.shared_artifacts)

    def test_route_2_sitemap_static_execution(self) -> None:
        route = SitemapStaticRoute()
        self.context.shared_artifacts["homepage_url"] = "https://www.aelektronikk.no"
        can_run, _ = route.can_execute(self.sample_org, self.context)
        self.assertTrue(can_run)

        # Test with custom sitemap XML
        sitemap_xml = """<?xml version="1.0" encoding="UTF-8"?>
        <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
            <url><loc>https://www.aelektronikk.no/om-oss</loc></url>
            <url><loc>https://www.aelektronikk.no/kontakt</loc></url>
        </urlset>"""
        self.context.options["mock_sitemap_xml"] = sitemap_xml

        result = route.execute(self.sample_org, self.context)
        self.assertEqual(result.status, AttemptStatus.SUCCESS.value)
        self.assertEqual(len(result.accepted_claims), 1)
        discovered = self.context.shared_artifacts.get("discovered_sitemap_urls", [])
        self.assertIn("https://www.aelektronikk.no/om-oss", discovered)

    def test_route_3_static_homepage_execution_and_identity_gate(self) -> None:
        route = StaticHomepageRoute()
        self.context.shared_artifacts["homepage_url"] = "https://www.aelektronikk.no"

        # Case A: Legitimate homepage mentioning company name
        self.context.options["mock_homepage_html"] = "<html><body><h1>AAS ELEKTRONIKK AS</h1><p>Org: 888567232</p></body></html>"
        res_pass = route.execute(self.sample_org, self.context)
        self.assertEqual(res_pass.status, AttemptStatus.SUCCESS.value)
        self.assertFalse(res_pass.quarantined)
        self.assertEqual(len(res_pass.accepted_claims), 1)

        # Case B: Disconnected domain (parked or unrelated) -> quarantined!
        self.context.options["mock_homepage_html"] = "<html><body><h1>Completely Unrelated Flowershop</h1><p>Buy roses now!</p></body></html>"
        res_quarantine = route.execute(self.sample_org, self.context)
        self.assertEqual(res_quarantine.status, AttemptStatus.QUARANTINED.value)
        self.assertTrue(res_quarantine.quarantined)
        self.assertEqual(len(res_quarantine.accepted_claims), 0)
        self.assertEqual(len(res_quarantine.rejected_claims), 1)

    def test_route_4_targeted_paths_execution(self) -> None:
        route = TargetedPathsRoute()
        self.context.shared_artifacts["homepage_url"] = "https://www.aelektronikk.no"
        self.context.shared_artifacts["discovered_sitemap_urls"] = [
            "https://www.aelektronikk.no/om-oss",
            "https://www.aelektronikk.no/kontakt",
        ]
        res = route.execute(self.sample_org, self.context)
        self.assertEqual(res.status, AttemptStatus.SUCCESS.value)
        self.assertGreaterEqual(len(res.accepted_claims), 2)
        for c in res.accepted_claims:
            self.assertGreaterEqual(len(c["evidence_span"]), 5)

    def test_route_5_jsonld_opengraph_execution(self) -> None:
        route = JsonLdOpenGraphRoute()
        self.context.shared_artifacts["homepage_html"] = """
        <html><head>
        <script type="application/ld+json">
        {"@context": "https://schema.org", "@type": "Organization", "name": "Aas Elektronikk AS"}
        </script>
        </head><body></body></html>
        """
        self.context.shared_artifacts["homepage_url"] = "https://www.aelektronikk.no"
        res = route.execute(self.sample_org, self.context)
        self.assertEqual(res.status, AttemptStatus.SUCCESS.value)
        self.assertEqual(res.requests_count, 0)  # 0 requests: reuses cached HTML
        self.assertEqual(len(res.accepted_claims), 1)

    def test_route_6_search_candidates_execution(self) -> None:
        route = SearchCandidatesRoute()
        org_no_web = dict(self.sample_org)
        org_no_web["website"] = ""

        # Context without API key -> abstains cleanly
        ctx_no_key = ExecutionContext(run_id="run-1", budget=MagicMock(can_proceed=lambda: True))
        can_run, reason = route.can_execute(org_no_web, ctx_no_key)
        self.assertFalse(can_run)
        self.assertEqual(reason, "brave_search_api_key_missing")

        # Context with mock brave key -> executes
        ctx_mock = ExecutionContext(
            run_id="run-1",
            budget=MagicMock(can_proceed=lambda: True),
            options={"mock_brave": True, "mock_brave_domain": "aelektronikk.no"},
        )
        res = route.execute(org_no_web, ctx_mock)
        self.assertEqual(res.status, AttemptStatus.SUCCESS.value)
        self.assertEqual(res.cost_usd, 0.005)
        self.assertEqual(len(res.accepted_claims), 1)

    def test_route_7_nav_jobs_execution(self) -> None:
        route = NavJobsRoute()
        ctx_mock = ExecutionContext(
            run_id="run-1",
            budget=MagicMock(can_proceed=lambda: True),
            options={"mock_nav_hits": [{"title": "Elektronikkingeniør", "employer": {"orgnr": "888567232"}}]},
        )
        res = route.execute(self.sample_org, ctx_mock)
        self.assertEqual(res.status, AttemptStatus.SUCCESS.value)
        self.assertEqual(len(res.accepted_claims), 1)
        self.assertEqual(res.accepted_claims[0]["value"]["active_job_count"], 1)

    def test_route_8_google_places_reverse_proof_gate(self) -> None:
        route = GooglePlacesRoute()

        # Case A: Unverified candidate place in different municipality or name mismatch -> Quarantined!
        ctx_mismatch = ExecutionContext(
            run_id="run-1",
            budget=MagicMock(can_proceed=lambda: True),
            options={
                "mock_google_places": {
                    "displayName": {"text": "Oslo Pizzeria"},
                    "formattedAddress": "Torggata 10, 0181 Oslo, Norway",
                    "rating": 4.1,
                }
            },
        )
        res_mismatch = route.execute(self.sample_org, ctx_mismatch)
        self.assertEqual(res_mismatch.status, AttemptStatus.QUARANTINED.value)
        self.assertTrue(res_mismatch.quarantined)
        self.assertEqual(len(res_mismatch.accepted_claims), 0)
        self.assertEqual(len(res_mismatch.rejected_claims), 1)
        self.assertEqual(res_mismatch.rejected_claims[0]["rejection_reason"], "failed_name_or_municipality_reverse_proof")

        # Case B: Verified candidate matching tokens and municipality -> Success!
        ctx_match = ExecutionContext(
            run_id="run-1",
            budget=MagicMock(can_proceed=lambda: True),
            options={
                "mock_google_places": {
                    "displayName": {"text": "Aas Elektronikk"},
                    "formattedAddress": "Industriveien 12, 4841 Arendal, Norway",
                    "rating": 4.8,
                }
            },
        )
        res_match = route.execute(self.sample_org, ctx_match)
        self.assertEqual(res_match.status, AttemptStatus.SUCCESS.value)
        self.assertFalse(res_match.quarantined)
        self.assertEqual(len(res_match.accepted_claims), 1)

    def test_route_9_leader_bridge_execution(self) -> None:
        route = LeaderBridgeRoute()
        res = route.execute(self.sample_org, self.context)
        self.assertEqual(res.status, AttemptStatus.SUCCESS.value)
        self.assertEqual(len(res.accepted_claims), 1)
        leaders = res.accepted_claims[0]["value"]
        self.assertTrue(any(l["name"] == "Knut Aas" for l in leaders))

    def test_route_10_browser_fallback_safe_abstention(self) -> None:
        route = BrowserFallbackRoute()
        # Without JS shell indication -> abstains
        can_run, reason = route.can_execute(self.sample_org, self.context)
        self.assertFalse(can_run)
        self.assertEqual(reason, "static_crawl_sufficient_or_not_js_shell")

        # With mock rendered html -> executes
        ctx_mock = ExecutionContext(
            run_id="run-1",
            budget=MagicMock(can_proceed=lambda: True),
            options={"force_browser_fallback": True, "mock_rendered_html": "<html><body>Rendered SPA Content</body></html>"},
        )
        res = route.execute(self.sample_org, ctx_mock)
        self.assertEqual(res.status, AttemptStatus.SUCCESS.value)
        self.assertEqual(len(res.accepted_claims), 1)

    def test_route_11_pdf_fallback_execution(self) -> None:
        route = PdfFallbackRoute()
        can_run, _ = route.can_execute(self.sample_org, self.context)
        self.assertTrue(can_run)

        # Mock PDF bytes containing employee text
        ctx_mock = ExecutionContext(
            run_id="run-1",
            budget=MagicMock(can_proceed=lambda: True),
            options={"mock_pdf_bytes": b"%PDF-1.4 workforce note: Selskapet sysselsatte 8 ansatte i 2024."},
        )
        res = route.execute(self.sample_org, ctx_mock)
        self.assertEqual(res.status, AttemptStatus.SUCCESS.value)
        self.assertEqual(len(res.accepted_claims), 1)
        self.assertEqual(res.accepted_claims[0]["value"]["extracted_workforce"], 8)

    def test_route_budget_blocking(self) -> None:
        exhausted_budget = MagicMock(can_proceed=MagicMock(return_value=False))
        ctx_blocked = ExecutionContext(run_id="run-blocked", budget=exhausted_budget)
        route = RegistrySiteRoute()
        res = route.execute(self.sample_org, ctx_blocked)
        self.assertEqual(res.status, AttemptStatus.BLOCKED.value)
        self.assertEqual(res.error, "budget_exhausted")

    def test_route_exception_isolation(self) -> None:
        class CrashingRoute(StrategyRoute):
            name = "crashing_route"
            def can_execute(self, org, ctx): return True, None
            def run(self, org, ctx): raise ValueError("Simulated network crash")

        route = CrashingRoute()
        res = route.execute(self.sample_org, self.context)
        self.assertEqual(res.status, AttemptStatus.FAILED.value)
        self.assertIn("ValueError: Simulated network crash", res.error)


class AttemptStorageTests(unittest.TestCase):
    """Tests for AttemptStorage with dual-locking and Gate 3 grounding validation."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.storage = AttemptStorage(base_dir=Path(self.temp_dir.name), partition_by_company=True)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_valid_attempt_record_write_and_load(self) -> None:
        record = StrategyAttemptRecord(
            attempt_id="att-test-001",
            run_id="run-test",
            organisation_number="888567232",
            company_name="AAS ELEKTRONIKK AS",
            route_name="static_homepage",
            version="1.0.0",
            timestamp="2026-10-02T15:00:00Z",
            status="success",
            requested_urls=["https://www.aelektronikk.no"],
            redirect_chain=["https://www.aelektronikk.no"],
            snapshot_hash="a" * 64,
            candidate_domains=["aelektronikk.no"],
            accepted_claims=[{
                "claim_id": "c1",
                "field": "company_text",
                "value": "Text",
                "evidence_span": "Verbatim text span",
                "evidence_ids": ["ev1"],
                "source_url": "https://www.aelektronikk.no",
            }],
            rejected_claims=[],
            exact_identity_evidence={"is_exact": True},
            runtime_seconds=0.42,
            request_count=1,
            cost_usd=0.0,
            quarantine_flag=False,
        )

        self.storage.write_attempt(record)

        # Global log exists and has 1 record
        records = self.storage.load_attempts()
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].attempt_id, "att-test-001")
        self.assertEqual(records[0].organisation_number, "888567232")

        # Partitioned per-company file exists
        company_records = self.storage.load_attempts(organisation_number="888567232")
        self.assertEqual(len(company_records), 1)

    def test_validation_rejects_missing_grounding(self) -> None:
        # Accepted claim missing evidence_span -> ValueError
        invalid_record = StrategyAttemptRecord(
            attempt_id="att-bad-001",
            run_id="run-test",
            organisation_number="888567232",
            company_name="AAS ELEKTRONIKK AS",
            route_name="static_homepage",
            version="1.0.0",
            timestamp="2026-10-02T15:00:00Z",
            status="success",
            requested_urls=["https://www.aelektronikk.no"],
            redirect_chain=[],
            snapshot_hash="b" * 64,
            candidate_domains=[],
            accepted_claims=[{
                "claim_id": "c1",
                "field": "company_text",
                "value": "Text",
                "evidence_span": "",  # Empty evidence span -> invalid!
                "evidence_ids": ["ev1"],
                "source_url": "https://www.aelektronikk.no",
            }],
            rejected_claims=[],
            exact_identity_evidence={},
            runtime_seconds=0.1,
            request_count=1,
            cost_usd=0.0,
            quarantine_flag=False,
        )

        errors = validate_attempt_record(invalid_record)
        self.assertTrue(any("evidence_span" in e for e in errors))

        with self.assertRaises(ValueError):
            self.storage.write_attempt(invalid_record)

    def test_validation_rejects_missing_rejection_reason(self) -> None:
        invalid_rejected = StrategyAttemptRecord(
            attempt_id="att-bad-002",
            run_id="run-test",
            organisation_number="888567232",
            company_name="AAS ELEKTRONIKK AS",
            route_name="google_places",
            version="1.0.0",
            timestamp="2026-10-02T15:00:00Z",
            status="quarantined",
            requested_urls=[],
            redirect_chain=[],
            snapshot_hash="c" * 64,
            candidate_domains=[],
            accepted_claims=[],
            rejected_claims=[{
                "field": "place",
                "candidate_value": {},
                "rejection_reason": "",  # Empty rejection reason -> invalid!
            }],
            exact_identity_evidence={},
            runtime_seconds=0.1,
            request_count=1,
            cost_usd=0.0,
            quarantine_flag=True,
        )

        errors = validate_attempt_record(invalid_rejected)
        self.assertTrue(any("rejection_reason" in e for e in errors))

    def test_storage_summarize_attempts(self) -> None:
        rec = StrategyAttemptRecord(
            attempt_id="att-sum-001",
            run_id="batch-1",
            organisation_number="888567232",
            company_name="AAS ELEKTRONIKK AS",
            route_name="static_homepage",
            version="1.0.0",
            timestamp="2026-10-02T15:00:00Z",
            status="success",
            requested_urls=["https://www.aelektronikk.no"],
            redirect_chain=[],
            snapshot_hash="d" * 64,
            candidate_domains=[],
            accepted_claims=[{
                "field": "f",
                "value": "v",
                "evidence_span": "Span here",
                "evidence_ids": ["ev1"],
                "source_url": "https://url.com",
            }],
            rejected_claims=[],
            exact_identity_evidence={},
            runtime_seconds=0.5,
            request_count=2,
            cost_usd=0.01,
            quarantine_flag=False,
        )
        self.storage.write_attempt(rec)

        summary = self.storage.summarize_attempts(run_id="batch-1")
        self.assertEqual(summary["total_attempts"], 1)
        self.assertEqual(summary["successful_attempts"], 1)
        self.assertEqual(summary["total_requests"], 2)
        self.assertEqual(summary["total_cost_usd"], 0.01)
        self.assertEqual(summary["grounded_evidence_rate"], 1.0)


class FreezePolicyTests(unittest.TestCase):
    """Tests for Strategy Freeze Manifest, CLI and verification."""

    def test_manifest_creation_and_verification(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            manifest_path = Path(td) / "strategy_manifest.json"
            manifest = freeze_strategies(manifest_path=manifest_path)

            self.assertTrue(manifest_path.exists())
            self.assertEqual(manifest.routes_count, 11)
            self.assertEqual(len(manifest.overall_manifest_hash), 64)

            # Verification passes immediately after freeze
            ver = verify_strategies(manifest_path=manifest_path)
            self.assertTrue(ver.is_valid)
            self.assertIn("VERIFIED", ver.message)

    def test_tamper_detection(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            manifest_path = Path(td) / "strategy_manifest.json"
            manifest = freeze_strategies(manifest_path=manifest_path)

            # Intentionally alter an expected hash in the manifest
            m_data = json.loads(manifest_path.read_text(encoding="utf-8"))
            first_fw = list(m_data["framework_files"].keys())[0]
            m_data["framework_files"][first_fw] = "0" * 64
            manifest_path.write_text(json.dumps(m_data), encoding="utf-8")

            ver = verify_strategies(manifest_path=manifest_path)
            self.assertFalse(ver.is_valid)
            self.assertGreaterEqual(len(ver.mismatches), 1)

    def test_crlf_normalization(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            p_lf = Path(td) / "file_lf.py"
            p_crlf = Path(td) / "file_crlf.py"
            p_lf.write_bytes(b"line 1\nline 2\n")
            p_crlf.write_bytes(b"line 1\r\nline 2\r\n")

            hash_lf = compute_file_sha256(p_lf)
            hash_crlf = compute_file_sha256(p_crlf)
            self.assertEqual(hash_lf, hash_crlf)

    def test_version_table_generation(self) -> None:
        table_str = generate_version_table()
        self.assertIn("registry_site", table_str)
        self.assertIn("browser_fallback", table_str)
        self.assertIn("pdf_fallback", table_str)
        self.assertIn("1.0.0", table_str)


class PromotionGateTests(unittest.TestCase):
    """Tests for Builderr's Hierarchical 6-Gate Promotion Engine."""

    def setUp(self) -> None:
        self.baseline = {
            "wrong_company_count": 0,
            "claim_precision": 0.98,
            "claim_recall": 0.80,
            "company_recall": 0.90,
            "coverage": 0.87,
            "grounded_evidence_rate": 1.0,
            "ungrounded_claims_count": 0,
            "total_requests": 150,
            "total_cost_usd": 0.50,
            "total_runtime_seconds": 60.0,
        }

    def test_gate_1_fatal_wrong_company_rejection(self) -> None:
        challenger = dict(self.baseline)
        challenger["wrong_company_count"] = 1  # Introduced 1 wrong company!
        challenger["claim_recall"] = 0.95      # Even with massive recall gain!

        verdict = evaluate_promotion(baseline_metrics=self.baseline, challenger_metrics=challenger)
        self.assertFalse(verdict.promoted)
        self.assertEqual(verdict.verdict_code, PromotionVerdictCode.REJECTED_FATAL_WRONG_COMPANY)
        self.assertEqual(verdict.gates_passed, 0)
        self.assertIn("gate_1_zero_wrong_company", verdict.failed_gate_names)
        # Downstream gates were skipped
        self.assertEqual(verdict.gate_results["gate_2_claim_precision_maintained"].status, GateStatus.SKIPPED)

    def test_gate_2_precision_drop_rejection(self) -> None:
        challenger = dict(self.baseline)
        # Baseline is 0.980, challenger is 0.955 (drop of 2.5% > max 2.0%)
        challenger["claim_precision"] = 0.955
        challenger["claim_recall"] = 0.85

        verdict = evaluate_promotion(baseline_metrics=self.baseline, challenger_metrics=challenger)
        self.assertFalse(verdict.promoted)
        self.assertEqual(verdict.verdict_code, PromotionVerdictCode.REJECTED_PRECISION_DROP)
        self.assertIn("gate_2_claim_precision_maintained", verdict.failed_gate_names)

    def test_gate_2_precision_drop_allowed_within_threshold(self) -> None:
        challenger = dict(self.baseline)
        # Baseline is 0.980, challenger is 0.965 (drop of 1.5% <= max 2.0%)
        challenger["claim_precision"] = 0.965
        challenger["claim_recall"] = 0.85
        challenger["coverage"] = 0.90

        verdict = evaluate_promotion(baseline_metrics=self.baseline, challenger_metrics=challenger)
        self.assertTrue(verdict.gate_results["gate_2_claim_precision_maintained"].passed)

    def test_gate_3_ungrounded_evidence_rejection(self) -> None:
        challenger = dict(self.baseline)
        challenger["grounded_evidence_rate"] = 0.98  # Less than 100%
        challenger["ungrounded_claims_count"] = 2
        challenger["claim_recall"] = 0.85

        verdict = evaluate_promotion(baseline_metrics=self.baseline, challenger_metrics=challenger)
        self.assertFalse(verdict.promoted)
        self.assertEqual(verdict.verdict_code, PromotionVerdictCode.REJECTED_UNGROUNDED_EVIDENCE)
        self.assertIn("gate_3_evidence_grounding_validity", verdict.failed_gate_names)

    def test_gate_4_lack_of_recall_improvement_rejection(self) -> None:
        challenger = dict(self.baseline)
        # Gain is only +0.005 (+0.5% < minimum +1.0%)
        challenger["claim_recall"] = 0.805
        challenger["coverage"] = 0.873

        verdict = evaluate_promotion(baseline_metrics=self.baseline, challenger_metrics=challenger)
        self.assertFalse(verdict.promoted)
        self.assertEqual(verdict.verdict_code, PromotionVerdictCode.REJECTED_NO_RECALL_IMPROVEMENT)
        self.assertIn("gate_4_recall_and_coverage_improvement", verdict.failed_gate_names)

    def test_gate_5_refresh_false_positive_rejection(self) -> None:
        challenger = dict(self.baseline)
        challenger["claim_recall"] = 0.85
        challenger["coverage"] = 0.90
        refresh_results = {"false_positive": 1, "recall": 1.0, "idempotent": True}

        verdict = evaluate_promotion(
            baseline_metrics=self.baseline,
            challenger_metrics=challenger,
            refresh_results=refresh_results,
        )
        self.assertFalse(verdict.promoted)
        self.assertEqual(verdict.verdict_code, PromotionVerdictCode.REJECTED_REFRESH_INCORRECT)
        self.assertIn("gate_5_refresh_correctness", verdict.failed_gate_names)

    def test_gate_6_budget_envelope_breach_rejection(self) -> None:
        challenger = dict(self.baseline)
        challenger["claim_recall"] = 0.85
        challenger["coverage"] = 0.90
        challenger["total_requests"] = 2500  # Exceeds 2000

        verdict = evaluate_promotion(baseline_metrics=self.baseline, challenger_metrics=challenger)
        self.assertFalse(verdict.promoted)
        self.assertEqual(verdict.verdict_code, PromotionVerdictCode.REJECTED_BUDGET_EXCEEDED)
        self.assertIn("gate_6_budget_envelope_compliance", verdict.failed_gate_names)

    def test_successful_promotion_when_all_gates_pass(self) -> None:
        challenger = dict(self.baseline)
        challenger["claim_precision"] = 0.985  # Precision gain!
        challenger["claim_recall"] = 0.85      # +5.0% gain!
        challenger["coverage"] = 0.90          # +3.0% gain!
        challenger["grounded_evidence_rate"] = 1.0
        challenger["ungrounded_claims_count"] = 0
        challenger["total_requests"] = 180     # Within budget
        challenger["total_cost_usd"] = 0.60
        challenger["total_runtime_seconds"] = 70.0

        verdict = evaluate_promotion(baseline_metrics=self.baseline, challenger_metrics=challenger)
        self.assertTrue(verdict.promoted)
        self.assertEqual(verdict.verdict_code, PromotionVerdictCode.PROMOTED)
        self.assertEqual(verdict.gates_passed, 6)
        self.assertEqual(len(verdict.failed_gate_names), 0)


class BenchmarkRunnerCLITests(unittest.TestCase):
    """Tests for CLI benchmark runner execution."""

    def test_cli_execution_on_smoke_companies(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            out_file = Path(td) / "eval-report.json"
            cmd = [
                sys.executable,
                "-m",
                "eval.run",
                "--corpus",
                str(ROOT / "eval" / "gold_companies.jsonl"),
                "--profiles",
                str(ROOT / "smoke-companies.jsonl"),
                "--output",
                str(out_file),
                "--quiet",
            ]
            res = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True)
            self.assertEqual(res.returncode, 0)
            self.assertTrue(out_file.exists())

            report = json.loads(out_file.read_text(encoding="utf-8"))
            self.assertEqual(report["total_gold_companies"], 10)
            self.assertEqual(report["matched_companies"], 10)
            self.assertEqual(report["wrong_company_count"], 0)
            self.assertEqual(report["grounded_evidence_rate"], 1.0)


if __name__ == "__main__":
    unittest.main()
