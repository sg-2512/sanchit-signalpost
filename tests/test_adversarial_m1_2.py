"""Adversarial Stress Test Suite for Milestone 1 Promotion Gate & Freeze Verification.

Executed by Challenger M1-2:
1. Gate 1: Adversarial fatal rejection (REJECTED_FATAL_WRONG_COMPANY) on wrong-company publications,
   verifying immediate halt and downstream gate skipping.
2. Gate 2: Adversarial rejection on claim precision drop > 2.0%.
3. Gate 3: Adversarial rejection on ungrounded claims (short span, missing SHA-256/URL).
4. Gate 6: Adversarial rejection on budget envelope breach (>2000 req, >$10 spend, >2700s runtime).
5. Freeze Verification: 1-character file modification tamper detection, CLI exit code 1,
   StrategyIntegrityViolationError raising, and clean restoration.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from eval.promotion_gate import (
    GateResult,
    GateStatus,
    GateVerdict,
    PromotionVerdictCode,
    evaluate_promotion,
)
from eval.run import evaluate_benchmark
from strategies.freeze import (
    StrategyIntegrityViolationError,
    enforce_frozen_state,
    verify_strategies,
)


class AdversarialPromotionGateTests(unittest.TestCase):
    """Stress tests for Builderr's Hierarchical 6-Gate Promotion Engine."""

    def setUp(self) -> None:
        self.perfect_baseline = {
            "wrong_company_count": 0,
            "claim_precision": 1.0,
            "claim_recall": 0.80,
            "company_recall": 0.90,
            "coverage": 0.87,
            "grounded_evidence_rate": 1.0,
            "ungrounded_claims_count": 0,
            "total_requests": 200,
            "total_cost_usd": 0.50,
            "total_runtime_seconds": 60.0,
        }

    # =========================================================================
    # Gate 1 Adversarial Challenges
    # =========================================================================
    def test_gate_1_fatal_rejection_single_wrong_company(self) -> None:
        """Gate 1 MUST trigger immediate fatal rejection on even 1 wrong publication and skip downstream gates."""
        challenger = dict(self.perfect_baseline)
        challenger["wrong_company_count"] = 1
        # Give challenger unbelievable improvements in every other metric
        challenger["claim_precision"] = 1.0
        challenger["claim_recall"] = 1.0
        challenger["coverage"] = 1.0
        challenger["total_requests"] = 10
        challenger["total_cost_usd"] = 0.01

        verdict = evaluate_promotion(
            baseline_metrics=self.perfect_baseline,
            challenger_metrics=challenger,
        )

        self.assertFalse(verdict.promoted, "Challenger with wrong company must NOT be promoted")
        self.assertEqual(verdict.verdict_code, PromotionVerdictCode.REJECTED_FATAL_WRONG_COMPANY)
        self.assertEqual(verdict.gates_passed, 0, "No gates should be counted as passed on fatal failure")
        self.assertEqual(verdict.total_gates, 6)
        self.assertIn("gate_1_zero_wrong_company", verdict.failed_gate_names)

        # Verify Gate 1 status
        g1 = verdict.gate_results["gate_1_zero_wrong_company"]
        self.assertEqual(g1.status, GateStatus.FATAL_FAILURE)
        self.assertFalse(g1.passed)
        self.assertTrue(g1.fatal)

        # Verify downstream gates were ALL aborted/skipped
        for gate_key in [
            "gate_2_claim_precision_maintained",
            "gate_3_evidence_grounding_validity",
            "gate_4_recall_and_coverage_improvement",
            "gate_5_refresh_correctness",
            "gate_6_budget_envelope_compliance",
        ]:
            self.assertIn(gate_key, verdict.gate_results)
            self.assertEqual(
                verdict.gate_results[gate_key].status,
                GateStatus.SKIPPED,
                f"Downstream gate {gate_key} must be marked SKIPPED",
            )
            self.assertFalse(verdict.gate_results[gate_key].passed)

    def test_gate_1_end_to_end_confuser_detection_via_benchmark(self) -> None:
        """Verify that introducing a forbidden confuser or aggregator into a profile triggers Gate 1 fatal rejection."""
        smoke_profiles_path = ROOT / "smoke-companies.jsonl"
        self.assertTrue(smoke_profiles_path.exists())

        raw_lines = smoke_profiles_path.read_text(encoding="utf-8").strip().splitlines()
        profiles = [json.loads(line) for line in raw_lines]

        # Inject an adversarial confuser into company 985589003
        # In gold_companies.jsonl: forbidden_confusers has org 912345111 ('VIKØREN BYGG AS')
        adversarial_profiles = []
        for p in profiles:
            p_copy = dict(p)
            if str(p_copy.get("organisation_number")) == "985589003":
                # Inject confuser org number in profile text
                p_copy["description"] = "Subsidiary of 912345111 VIKØREN BYGG AS"
            adversarial_profiles.append(p_copy)

        with tempfile.TemporaryDirectory() as td:
            adv_path = Path(td) / "adv_wrong_company_profiles.jsonl"
            with open(adv_path, "w", encoding="utf-8") as f:
                for p in adversarial_profiles:
                    f.write(json.dumps(p) + "\n")

            baseline_metrics = {
                "wrong_company_count": 0,
                "claim_precision": 1.0,
                "claim_recall": 0.36,
                "coverage": 0.18,
                "grounded_evidence_rate": 1.0,
                "ungrounded_claims_count": 0,
                "total_requests": 0,
                "total_cost_usd": 0.0,
                "total_runtime_seconds": 0.0,
            }

            metrics = evaluate_benchmark(
                corpus_path=ROOT / "eval" / "gold_companies.jsonl",
                profiles_path=adv_path,
                baseline_metrics=baseline_metrics,
            )

            self.assertGreaterEqual(metrics["wrong_company_count"], 1)
            self.assertIn("gate_verdict", metrics)
            v = metrics["gate_verdict"]
            self.assertFalse(v["promoted"])
            self.assertEqual(v["verdict_code"], "REJECTED_FATAL_WRONG_COMPANY")
            self.assertEqual(v["gates_passed"], 0)
            self.assertEqual(v["gate_results"]["gate_2_claim_precision_maintained"]["status"], "SKIPPED")

    def test_gate_1_forbidden_aggregator_domain_website_publication(self) -> None:
        """Publishing a forbidden directory (e.g. proff.no) as company website triggers fatal wrong-company."""
        smoke_profiles_path = ROOT / "smoke-companies.jsonl"
        raw_lines = smoke_profiles_path.read_text(encoding="utf-8").strip().splitlines()
        profiles = [json.loads(line) for line in raw_lines]

        adversarial_profiles = []
        for p in profiles:
            p_copy = dict(p)
            if str(p_copy.get("organisation_number")) == "985589003":
                # Inject forbidden domain proff.no as website
                p_copy["website"] = "https://www.proff.no/selskap/arkitektfirma-jon-vikøren"
            adversarial_profiles.append(p_copy)

        with tempfile.TemporaryDirectory() as td:
            adv_path = Path(td) / "adv_forbidden_domain_profiles.jsonl"
            with open(adv_path, "w", encoding="utf-8") as f:
                for p in adversarial_profiles:
                    f.write(json.dumps(p) + "\n")

            baseline_metrics = {"wrong_company_count": 0, "claim_precision": 1.0}
            metrics = evaluate_benchmark(
                corpus_path=ROOT / "eval" / "gold_companies.jsonl",
                profiles_path=adv_path,
                baseline_metrics=baseline_metrics,
            )

            self.assertGreaterEqual(metrics["wrong_company_count"], 1)
            self.assertEqual(metrics["gate_verdict"]["verdict_code"], "REJECTED_FATAL_WRONG_COMPANY")

    # =========================================================================
    # Gate 2 Adversarial Challenges
    # =========================================================================
    def test_gate_2_precision_drop_boundary_and_rejection(self) -> None:
        """Gate 2 must reject when precision drop exceeds 2.0% and accept within 2.0%."""
        # Baseline = 0.980
        # Case A: drop of 2.01% (0.980 - 0.0201 = 0.9599) -> REJECT
        challenger_fail = dict(self.perfect_baseline)
        challenger_fail["claim_precision"] = 0.9599
        challenger_fail["claim_recall"] = 0.85
        challenger_fail["coverage"] = 0.90

        verdict_fail = evaluate_promotion(
            baseline_metrics={"claim_precision": 0.980, "wrong_company_count": 0},
            challenger_metrics=challenger_fail,
        )
        self.assertFalse(verdict_fail.promoted)
        self.assertEqual(verdict_fail.verdict_code, PromotionVerdictCode.REJECTED_PRECISION_DROP)
        self.assertFalse(verdict_fail.gate_results["gate_2_claim_precision_maintained"].passed)

        # Case B: drop of 1.80% (0.980 - 0.0180 = 0.9620 <= max 2.0%) -> PASS Gate 2
        challenger_pass = dict(self.perfect_baseline)
        challenger_pass["claim_precision"] = 0.9620
        challenger_pass["claim_recall"] = 0.85
        challenger_pass["coverage"] = 0.90

        verdict_pass = evaluate_promotion(
            baseline_metrics={"claim_precision": 0.980, "wrong_company_count": 0},
            challenger_metrics=challenger_pass,
        )
        self.assertTrue(verdict_pass.gate_results["gate_2_claim_precision_maintained"].passed)

        # Case C: drop of 2.5% (0.980 - 0.025 = 0.9550 > 2.0%) -> REJECT
        challenger_drop_25 = dict(self.perfect_baseline)
        challenger_drop_25["claim_precision"] = 0.9550
        challenger_drop_25["claim_recall"] = 0.85
        verdict_drop_25 = evaluate_promotion(
            baseline_metrics={"claim_precision": 0.980, "wrong_company_count": 0},
            challenger_metrics=challenger_drop_25,
        )
        self.assertFalse(verdict_drop_25.promoted)
        self.assertEqual(verdict_drop_25.verdict_code, PromotionVerdictCode.REJECTED_PRECISION_DROP)

        # Case D: drop of 5.0% -> REJECT
        challenger_big_drop = dict(self.perfect_baseline)
        challenger_big_drop["claim_precision"] = 0.930
        challenger_big_drop["claim_recall"] = 0.95
        verdict_big_drop = evaluate_promotion(
            baseline_metrics={"claim_precision": 0.980, "wrong_company_count": 0},
            challenger_metrics=challenger_big_drop,
        )
        self.assertFalse(verdict_big_drop.promoted)
        self.assertEqual(verdict_big_drop.verdict_code, PromotionVerdictCode.REJECTED_PRECISION_DROP)

    # =========================================================================
    # Gate 3 Adversarial Challenges
    # =========================================================================
    def test_gate_3_ungrounded_claims_rejection(self) -> None:
        """Gate 3 must reject any challenger with grounded_rate < 1.0 or ungrounded_claims_count > 0."""
        # Case A: 99% grounding, 1 ungrounded claim
        chal_a = dict(self.perfect_baseline)
        chal_a["grounded_evidence_rate"] = 0.99
        chal_a["ungrounded_claims_count"] = 1
        chal_a["claim_recall"] = 0.85
        verdict_a = evaluate_promotion(
            baseline_metrics=self.perfect_baseline,
            challenger_metrics=chal_a,
        )
        self.assertFalse(verdict_a.promoted)
        self.assertEqual(verdict_a.verdict_code, PromotionVerdictCode.REJECTED_UNGROUNDED_EVIDENCE)
        self.assertFalse(verdict_a.gate_results["gate_3_evidence_grounding_validity"].passed)

        # Case B: Adversarial inconsistency - grounded_rate reported as 1.0 but ungrounded_count = 1
        chal_b = dict(self.perfect_baseline)
        chal_b["grounded_evidence_rate"] = 1.0
        chal_b["ungrounded_claims_count"] = 1
        chal_b["claim_recall"] = 0.85
        verdict_b = evaluate_promotion(
            baseline_metrics=self.perfect_baseline,
            challenger_metrics=chal_b,
        )
        self.assertFalse(verdict_b.promoted)
        self.assertEqual(verdict_b.verdict_code, PromotionVerdictCode.REJECTED_UNGROUNDED_EVIDENCE)
        self.assertFalse(verdict_b.gate_results["gate_3_evidence_grounding_validity"].passed)

    def test_gate_3_end_to_end_grounding_failure_via_benchmark(self) -> None:
        """Verify that an evidence claim missing SHA-256 or short span triggers Gate 3 rejection in benchmark."""
        smoke_profiles_path = ROOT / "smoke-companies.jsonl"
        raw_lines = smoke_profiles_path.read_text(encoding="utf-8").strip().splitlines()
        profiles = [json.loads(line) for line in raw_lines]

        # Inject an ungrounded evidence item into company 888567232
        adversarial_profiles = []
        for p in profiles:
            p_copy = dict(p)
            if str(p_copy.get("organisation_number")) == "888567232":
                p_copy["evidence"] = {
                    "unverified_fact": {
                        "status": "available",
                        "claim_span": "abc",  # Too short (< 5 chars)
                        "content_sha256": "",  # Missing sha256
                        "source_url": "",      # Missing url
                    }
                }
            adversarial_profiles.append(p_copy)

        with tempfile.TemporaryDirectory() as td:
            adv_path = Path(td) / "adv_ungrounded_profiles.jsonl"
            with open(adv_path, "w", encoding="utf-8") as f:
                for p in adversarial_profiles:
                    f.write(json.dumps(p) + "\n")

            baseline_metrics = {
                "wrong_company_count": 0,
                "claim_precision": 1.0,
                "claim_recall": 0.20,
                "coverage": 0.10,
                "grounded_evidence_rate": 1.0,
                "ungrounded_claims_count": 0,
            }

            metrics = evaluate_benchmark(
                corpus_path=ROOT / "eval" / "gold_companies.jsonl",
                profiles_path=adv_path,
                baseline_metrics=baseline_metrics,
            )

            self.assertGreater(metrics["ungrounded_claims_count"], 0)
            self.assertLess(metrics["grounded_evidence_rate"], 1.0)
            self.assertFalse(metrics["gate_verdict"]["promoted"])
            self.assertEqual(metrics["gate_verdict"]["verdict_code"], "REJECTED_UNGROUNDED_EVIDENCE")

    # =========================================================================
    # Gate 6 Adversarial Challenges
    # =========================================================================
    def test_gate_6_budget_envelope_breach(self) -> None:
        """Gate 6 must reject any challenger exceeding requests (>2000), cost (>$10.00), or runtime (>2700s)."""
        # Case A: Requests breach (2500 requests > 2000)
        chal_req = dict(self.perfect_baseline)
        chal_req["claim_recall"] = 0.85
        chal_req["coverage"] = 0.90
        chal_req["total_requests"] = 2500
        verdict_req = evaluate_promotion(
            baseline_metrics=self.perfect_baseline,
            challenger_metrics=chal_req,
        )
        self.assertFalse(verdict_req.promoted)
        self.assertEqual(verdict_req.verdict_code, PromotionVerdictCode.REJECTED_BUDGET_EXCEEDED)
        self.assertFalse(verdict_req.gate_results["gate_6_budget_envelope_compliance"].passed)
        self.assertIn("requests", verdict_req.rationale)

        # Case B: Cost breach ($15.00 > $10.00)
        chal_cost = dict(self.perfect_baseline)
        chal_cost["claim_recall"] = 0.85
        chal_cost["coverage"] = 0.90
        chal_cost["total_cost_usd"] = 15.00
        verdict_cost = evaluate_promotion(
            baseline_metrics=self.perfect_baseline,
            challenger_metrics=chal_cost,
        )
        self.assertFalse(verdict_cost.promoted)
        self.assertEqual(verdict_cost.verdict_code, PromotionVerdictCode.REJECTED_BUDGET_EXCEEDED)
        self.assertFalse(verdict_cost.gate_results["gate_6_budget_envelope_compliance"].passed)
        self.assertIn("cost", verdict_cost.rationale)

        # Case C: Runtime breach (2800s > 2700s)
        chal_time = dict(self.perfect_baseline)
        chal_time["claim_recall"] = 0.85
        chal_time["coverage"] = 0.90
        chal_time["total_runtime_seconds"] = 2800.0
        verdict_time = evaluate_promotion(
            baseline_metrics=self.perfect_baseline,
            challenger_metrics=chal_time,
        )
        self.assertFalse(verdict_time.promoted)
        self.assertEqual(verdict_time.verdict_code, PromotionVerdictCode.REJECTED_BUDGET_EXCEEDED)
        self.assertFalse(verdict_time.gate_results["gate_6_budget_envelope_compliance"].passed)
        self.assertIn("runtime", verdict_time.rationale)

        # Boundary checks: exact envelope vs envelope + 1
        # Requests: 2000 passes, 2001 fails
        chal_2000 = dict(self.perfect_baseline, claim_recall=0.85, coverage=0.90, total_requests=2000)
        v_2000 = evaluate_promotion(self.perfect_baseline, chal_2000)
        self.assertTrue(v_2000.gate_results["gate_6_budget_envelope_compliance"].passed)

        chal_2001 = dict(self.perfect_baseline, claim_recall=0.85, coverage=0.90, total_requests=2001)
        v_2001 = evaluate_promotion(self.perfect_baseline, chal_2001)
        self.assertFalse(v_2001.gate_results["gate_6_budget_envelope_compliance"].passed)

        # Cost: 10.00 passes, 10.01 fails
        chal_1000 = dict(self.perfect_baseline, claim_recall=0.85, coverage=0.90, total_cost_usd=10.00)
        v_1000 = evaluate_promotion(self.perfect_baseline, chal_1000)
        self.assertTrue(v_1000.gate_results["gate_6_budget_envelope_compliance"].passed)

        chal_1001 = dict(self.perfect_baseline, claim_recall=0.85, coverage=0.90, total_cost_usd=10.01)
        v_1001 = evaluate_promotion(self.perfect_baseline, chal_1001)
        self.assertFalse(v_1001.gate_results["gate_6_budget_envelope_compliance"].passed)

    def test_cli_promote_exits_1_on_adversarial_rejection(self) -> None:
        """Verify python -m eval.run --promote exits with code 1 when promotion is rejected."""
        with tempfile.TemporaryDirectory() as td:
            base_file = Path(td) / "baseline.json"
            base_file.write_text(json.dumps(self.perfect_baseline), encoding="utf-8")

            # Adversarial profile with wrong entity
            smoke_profiles_path = ROOT / "smoke-companies.jsonl"
            raw_lines = smoke_profiles_path.read_text(encoding="utf-8").strip().splitlines()
            adv_profiles = []
            for line in raw_lines:
                p = json.loads(line)
                if str(p.get("organisation_number")) == "985589003":
                    p["description"] = "Forbidden confuser 912345111"
                adv_profiles.append(p)

            prof_file = Path(td) / "adv_profiles.jsonl"
            with open(prof_file, "w", encoding="utf-8") as f:
                for p in adv_profiles:
                    f.write(json.dumps(p) + "\n")

            out_file = Path(td) / "report.json"
            cmd = [
                sys.executable,
                "-m",
                "eval.run",
                "--corpus",
                str(ROOT / "eval" / "gold_companies.jsonl"),
                "--profiles",
                str(prof_file),
                "--baseline-metrics",
                str(base_file),
                "--output",
                str(out_file),
                "--promote",
                "--quiet",
            ]
            proc = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True)
            self.assertEqual(proc.returncode, 1, "eval.run --promote must exit with code 1 when rejected")
            if out_file.exists():
                report = json.loads(out_file.read_text(encoding="utf-8"))
                self.assertEqual(report["gate_verdict"]["verdict_code"], "REJECTED_FATAL_WRONG_COMPANY")

    def test_empirical_finding_circular_reference_in_eval_benchmark(self) -> None:
        """EMPIRICAL FINDING: evaluate_benchmark returns a circular reference when baseline_metrics is passed.
        
        eval_metrics is passed to evaluate_promotion as challenger_metrics.
        GateVerdict stores challenger_summary (eval_metrics).
        Then eval_metrics['gate_verdict'] = gate_verdict.to_dict() links eval_metrics back into itself!
        This causes json.dumps(metrics) to raise ValueError('Circular reference detected').
        """
        smoke_profiles_path = ROOT / "smoke-companies.jsonl"
        baseline = {"wrong_company_count": 0, "claim_precision": 1.0}
        metrics = evaluate_benchmark(
            corpus_path=ROOT / "eval" / "gold_companies.jsonl",
            profiles_path=smoke_profiles_path,
            baseline_metrics=baseline,
        )
        self.assertIn("gate_verdict", metrics)
        with self.assertRaises(ValueError) as ctx:
            json.dumps(metrics)
        self.assertIn("Circular reference detected", str(ctx.exception))

    def test_empirical_finding_gate_2_floating_point_exact_boundary(self) -> None:
        """EMPIRICAL FINDING: IEEE-754 representation causes exact 2.0% drop to fail Gate 2 without rounding.
        
        0.960 - 0.980 == -0.020000000000000018 < -0.020.
        """
        chal = dict(self.perfect_baseline)
        chal["claim_precision"] = 0.9600  # Exactly 2.0% drop from 0.980
        chal["claim_recall"] = 0.85
        chal["coverage"] = 0.90

        verdict = evaluate_promotion(
            baseline_metrics={"claim_precision": 0.980, "wrong_company_count": 0},
            challenger_metrics=chal,
        )
        # Because eval/promotion_gate.py does not round prec_delta, it fails the exact boundary
        self.assertFalse(verdict.gate_results["gate_2_claim_precision_maintained"].passed)
        self.assertLess(verdict.metric_deltas["precision_delta"], -0.020)


class AdversarialFreezeVerificationTests(unittest.TestCase):
    """Stress tests for Strategy Freeze Tamper Detection and Integrity Enforcement."""

    def test_single_character_tamper_detection_and_enforcement(self) -> None:
        """Simulate a 1-character tamper in a strategy file:
        
        1. Verify python -m strategies.freeze --verify returns exit code 1.
        2. Verify enforce_frozen_state() raises StrategyIntegrityViolationError.
        3. Revert modification and verify integrity restored to exit code 0.
        """
        target_file = ROOT / "strategies" / "base.py"
        self.assertTrue(target_file.exists(), f"Target file must exist: {target_file}")

        original_bytes = target_file.read_bytes()

        try:
            # 1. Baseline verification must pass
            res_clean = verify_strategies()
            self.assertTrue(res_clean.is_valid, "Baseline repository must be clean and verified")

            # 2. Simulate 1-character modification (append single space)
            tampered_bytes = original_bytes + b" "
            target_file.write_bytes(tampered_bytes)

            # 3. Test verify_strategies() detects modification
            res_tampered = verify_strategies()
            self.assertFalse(res_tampered.is_valid, "Tampered file must fail verification")
            self.assertGreaterEqual(len(res_tampered.mismatches), 1)
            modified_rel_paths = [m["file"] for m in res_tampered.mismatches]
            self.assertIn("strategies/base.py", modified_rel_paths)

            # 4. Test enforce_frozen_state() raises StrategyIntegrityViolationError
            with self.assertRaises(StrategyIntegrityViolationError):
                enforce_frozen_state()

            # 5. Test CLI invocation exits with code 1
            cmd = [sys.executable, "-m", "strategies.freeze", "--verify"]
            proc = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True)
            self.assertEqual(proc.returncode, 1, "strategies.freeze --verify must exit with code 1 when tampered")
            self.assertIn("Integrity violations detected", proc.stdout)
            self.assertIn("strategies/base.py", proc.stdout)

        finally:
            # 6. Revert immediately
            target_file.write_bytes(original_bytes)

        # 7. Post-revert verification must pass
        res_restored = verify_strategies()
        self.assertTrue(res_restored.is_valid, "Restored repository must be valid")
        self.assertEqual(len(res_restored.mismatches), 0)

        # 8. Post-revert CLI invocation must exit with code 0
        cmd = [sys.executable, "-m", "strategies.freeze", "--verify"]
        proc_clean = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True)
        self.assertEqual(proc_clean.returncode, 0, "strategies.freeze --verify must exit with code 0 when clean")
        self.assertIn("VERIFIED", proc_clean.stdout)

    def test_missing_strategy_file_detection(self) -> None:
        """Verify that a missing framework or connector file triggers verification failure."""
        with tempfile.TemporaryDirectory() as td:
            manifest_path = Path(td) / "strategy_manifest.json"
            # Create manifest pointing to a non-existent file
            manifest_data = {
                "manifest_version": "1.0.0",
                "framework_files": {
                    "strategies/non_existent_file.py": "0" * 64,
                },
                "connector_files": {},
            }
            manifest_path.write_text(json.dumps(manifest_data), encoding="utf-8")

            res = verify_strategies(manifest_path=manifest_path)
            self.assertFalse(res.is_valid)
            self.assertIn("strategies/non_existent_file.py", res.missing_files)

            with self.assertRaises(StrategyIntegrityViolationError):
                verify_strategies(manifest_path=manifest_path, raise_on_error=True)


if __name__ == "__main__":
    unittest.main()
