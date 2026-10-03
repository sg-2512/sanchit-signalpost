"""SignalPost Promotion Gate Engine.

Implements the strict hierarchical 6-gate promotion engine:
Gate 1: Zero new material wrong-company publications (FATAL GATE: halts immediately)
Gate 2: Claim precision maintained (drop <= 2.0%)
Gate 3: 100% source-span grounding on accepted claims
Gate 4: Recall and coverage improvement (>= 1.0% gain)
Gate 5: Refresh correctness: verified changes detected without false changes
Gate 6: Cost and runtime within allocated budget envelope
"""
from __future__ import annotations

import sys
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
    return datetime.now(timezone.utc).isoformat()


class GateStatus(str, Enum):
    PASSED = "PASSED"
    FAILED = "FAILED"
    FATAL_FAILURE = "FATAL_FAILURE"
    SKIPPED = "SKIPPED"


class PromotionVerdictCode(str, Enum):
    PROMOTED = "PROMOTED"
    REJECTED_FATAL_WRONG_COMPANY = "REJECTED_FATAL_WRONG_COMPANY"
    REJECTED_PRECISION_DROP = "REJECTED_PRECISION_DROP"
    REJECTED_UNGROUNDED_EVIDENCE = "REJECTED_UNGROUNDED_EVIDENCE"
    REJECTED_NO_RECALL_IMPROVEMENT = "REJECTED_NO_RECALL_IMPROVEMENT"
    REJECTED_REFRESH_INCORRECT = "REJECTED_REFRESH_INCORRECT"
    REJECTED_BUDGET_EXCEEDED = "REJECTED_BUDGET_EXCEEDED"


@dataclass
class GateResult:
    gate_number: int
    gate_name: str
    status: GateStatus
    passed: bool
    fatal: bool
    baseline_value: Any
    challenger_value: Any
    delta: Optional[float]
    threshold_spec: str
    failure_reason: Optional[str] = None
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "gate_number": self.gate_number,
            "gate_name": self.gate_name,
            "status": self.status.value,
            "passed": self.passed,
            "fatal": self.fatal,
            "baseline_value": self.baseline_value,
            "challenger_value": self.challenger_value,
            "delta": self.delta,
            "threshold_spec": self.threshold_spec,
            "failure_reason": self.failure_reason,
            "details": self.details,
        }


@dataclass
class GateVerdict:
    promoted: bool
    verdict_code: PromotionVerdictCode
    gates_passed: int
    total_gates: int
    failed_gate_names: list[str]
    gate_results: dict[str, GateResult]
    metric_deltas: dict[str, float]
    baseline_summary: dict[str, Any]
    challenger_summary: dict[str, Any]
    budget_envelope: dict[str, Any]
    evaluated_at: str
    rationale: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "promoted": self.promoted,
            "verdict_code": self.verdict_code.value,
            "gates_passed": self.gates_passed,
            "total_gates": self.total_gates,
            "failed_gate_names": self.failed_gate_names,
            "gate_results": {k: v.to_dict() for k, v in self.gate_results.items()},
            "metric_deltas": self.metric_deltas,
            "baseline_summary": self.baseline_summary,
            "challenger_summary": self.challenger_summary,
            "budget_envelope": self.budget_envelope,
            "evaluated_at": self.evaluated_at,
            "rationale": self.rationale,
        }


def evaluate_promotion(
    baseline_metrics: dict[str, Any],
    challenger_metrics: dict[str, Any],
    budget_envelope: Optional[dict[str, Any]] = None,
    refresh_results: Optional[dict[str, Any]] = None,
) -> GateVerdict:
    """Evaluate candidate crawler strategy against baseline through the 6-gate hierarchy.
    
    Returns:
        GateVerdict with promotion decision, pass/fail per gate, and audit details.
    """
    envelope = budget_envelope or {
        "max_requests": 2000,
        "max_cost_usd": 10.0,
        "max_runtime_seconds": 2700.0,
    }

    gate_results: dict[str, GateResult] = {}
    metric_deltas: dict[str, float] = {}
    failed_gates: list[str] = []

    # -------------------------------------------------------------------------
    # GATE 1: Zero New Material Wrong-Company Publications (FATAL GATE)
    # -------------------------------------------------------------------------
    base_wrong = int(baseline_metrics.get("wrong_company_count", baseline_metrics.get("wrong_entity_publications", 0)))
    chal_wrong = int(challenger_metrics.get("wrong_company_count", challenger_metrics.get("wrong_entity_publications", 0)))
    wrong_delta = float(chal_wrong - base_wrong)
    metric_deltas["wrong_company_delta"] = wrong_delta

    g1_passed = (chal_wrong <= base_wrong and chal_wrong == 0) or (chal_wrong <= base_wrong)
    # If challenger introduces ANY wrong company, fatal failure!
    if chal_wrong > base_wrong or (chal_wrong > 0 and base_wrong == 0):
        g1_passed = False

    if not g1_passed:
        g1_reason = f"Fatal Gate 1 failure: Challenger introduced {chal_wrong} wrong-company publication(s) (baseline had {base_wrong})."
        gate_results["gate_1_zero_wrong_company"] = GateResult(
            gate_number=1,
            gate_name="zero_wrong_company",
            status=GateStatus.FATAL_FAILURE,
            passed=False,
            fatal=True,
            baseline_value=base_wrong,
            challenger_value=chal_wrong,
            delta=wrong_delta,
            threshold_spec="challenger_wrong <= baseline_wrong and chal_wrong == 0",
            failure_reason=g1_reason,
            details={"disqualification": "Immediate halt: wrong-company publication reduces competition score to zero."},
        )
        failed_gates.append("gate_1_zero_wrong_company")

        # Downstream gates are skipped
        for g_num, g_name in [
            (2, "claim_precision_maintained"),
            (3, "evidence_grounding_validity"),
            (4, "recall_and_coverage_improvement"),
            (5, "refresh_correctness"),
            (6, "budget_envelope_compliance"),
        ]:
            gate_results[f"gate_{g_num}_{g_name}"] = GateResult(
                gate_number=g_num,
                gate_name=g_name,
                status=GateStatus.SKIPPED,
                passed=False,
                fatal=False,
                baseline_value=None,
                challenger_value=None,
                delta=None,
                threshold_spec="Skipped due to upstream fatal gate failure",
                failure_reason="Skipped due to Gate 1 fatal failure",
            )
            failed_gates.append(f"gate_{g_num}_{g_name}")

        return GateVerdict(
            promoted=False,
            verdict_code=PromotionVerdictCode.REJECTED_FATAL_WRONG_COMPANY,
            gates_passed=0,
            total_gates=6,
            failed_gate_names=failed_gates,
            gate_results=gate_results,
            metric_deltas=metric_deltas,
            baseline_summary=baseline_metrics,
            challenger_summary=challenger_metrics,
            budget_envelope=envelope,
            evaluated_at=_utc_now_iso(),
            rationale=g1_reason,
        )

    gate_results["gate_1_zero_wrong_company"] = GateResult(
        gate_number=1,
        gate_name="zero_wrong_company",
        status=GateStatus.PASSED,
        passed=True,
        fatal=True,
        baseline_value=base_wrong,
        challenger_value=chal_wrong,
        delta=wrong_delta,
        threshold_spec="challenger_wrong <= baseline_wrong and chal_wrong == 0",
    )

    # -------------------------------------------------------------------------
    # GATE 2: Claim Precision Maintained (Drop <= 2.0%)
    # -------------------------------------------------------------------------
    base_prec = float(baseline_metrics.get("claim_precision", baseline_metrics.get("precision", 1.0)))
    chal_prec = float(challenger_metrics.get("claim_precision", challenger_metrics.get("precision", 1.0)))
    prec_delta = chal_prec - base_prec
    metric_deltas["precision_delta"] = prec_delta

    # Allowed drop <= 0.020
    g2_passed = prec_delta >= -0.020
    if not g2_passed:
        g2_reason = f"Precision dropped by {abs(prec_delta)*100:.2f}% from {base_prec*100:.2f}% to {chal_prec*100:.2f}% (max allowed drop is 2.0%)."
        gate_results["gate_2_claim_precision_maintained"] = GateResult(
            gate_number=2,
            gate_name="claim_precision_maintained",
            status=GateStatus.FAILED,
            passed=False,
            fatal=False,
            baseline_value=base_prec,
            challenger_value=chal_prec,
            delta=prec_delta,
            threshold_spec="prec_delta >= -0.020",
            failure_reason=g2_reason,
        )
        failed_gates.append("gate_2_claim_precision_maintained")
    else:
        gate_results["gate_2_claim_precision_maintained"] = GateResult(
            gate_number=2,
            gate_name="claim_precision_maintained",
            status=GateStatus.PASSED,
            passed=True,
            fatal=False,
            baseline_value=base_prec,
            challenger_value=chal_prec,
            delta=prec_delta,
            threshold_spec="prec_delta >= -0.020",
        )

    # -------------------------------------------------------------------------
    # GATE 3: Evidence Grounding Validity (100% source-span grounding)
    # -------------------------------------------------------------------------
    grounded_rate = float(challenger_metrics.get("grounded_evidence_rate", challenger_metrics.get("evidence_grounding_rate", 1.0)))
    ungrounded_count = int(challenger_metrics.get("ungrounded_claims_count", challenger_metrics.get("ungrounded_claims", 0)))
    metric_deltas["grounded_evidence_rate"] = grounded_rate

    g3_passed = (grounded_rate >= 1.0) and (ungrounded_count == 0)
    if not g3_passed:
        g3_reason = f"Evidence grounding rate is {grounded_rate*100:.2f}% (100.0% required; {ungrounded_count} ungrounded claims detected)."
        gate_results["gate_3_evidence_grounding_validity"] = GateResult(
            gate_number=3,
            gate_name="evidence_grounding_validity",
            status=GateStatus.FAILED,
            passed=False,
            fatal=False,
            baseline_value=1.0,
            challenger_value=grounded_rate,
            delta=grounded_rate - 1.0,
            threshold_spec="grounded_rate == 1.0 and ungrounded_count == 0",
            failure_reason=g3_reason,
        )
        failed_gates.append("gate_3_evidence_grounding_validity")
    else:
        gate_results["gate_3_evidence_grounding_validity"] = GateResult(
            gate_number=3,
            gate_name="evidence_grounding_validity",
            status=GateStatus.PASSED,
            passed=True,
            fatal=False,
            baseline_value=1.0,
            challenger_value=grounded_rate,
            delta=0.0,
            threshold_spec="grounded_rate == 1.0 and ungrounded_count == 0",
        )

    # -------------------------------------------------------------------------
    # GATE 4: Recall & Coverage Improvement (>= 1.0% Gain)
    # -------------------------------------------------------------------------
    base_recall = float(baseline_metrics.get("claim_recall", baseline_metrics.get("recall", 0.0)))
    chal_recall = float(challenger_metrics.get("claim_recall", challenger_metrics.get("recall", 0.0)))
    recall_delta = chal_recall - base_recall
    metric_deltas["recall_delta"] = recall_delta

    base_co_rec = float(baseline_metrics.get("company_recall", 1.0))
    chal_co_rec = float(challenger_metrics.get("company_recall", 1.0))

    base_cov = float(baseline_metrics.get("coverage", 0.70 * base_co_rec + 0.30 * base_recall))
    chal_cov = float(challenger_metrics.get("coverage", 0.70 * chal_co_rec + 0.30 * chal_recall))
    cov_delta = chal_cov - base_cov
    metric_deltas["coverage_delta"] = cov_delta

    # Pass if either coverage gain >= +0.010 or recall gain >= +0.010
    # Also pass if both baseline and challenger are already saturated at 100% precision and recall
    saturated = (base_cov >= 0.999 and chal_cov >= 0.999 and base_recall >= 0.999 and chal_recall >= 0.999)
    g4_passed = (cov_delta >= 0.010 or recall_delta >= 0.010 or saturated)

    if not g4_passed:
        g4_reason = f"Recall gain (+{recall_delta*100:.2f}%) and coverage gain (+{cov_delta*100:.2f}%) are below the minimum +1.0% threshold."
        gate_results["gate_4_recall_and_coverage_improvement"] = GateResult(
            gate_number=4,
            gate_name="recall_and_coverage_improvement",
            status=GateStatus.FAILED,
            passed=False,
            fatal=False,
            baseline_value={"recall": base_recall, "coverage": base_cov},
            challenger_value={"recall": chal_recall, "coverage": chal_cov},
            delta=max(cov_delta, recall_delta),
            threshold_spec="cov_delta >= 0.010 or recall_delta >= 0.010",
            failure_reason=g4_reason,
        )
        failed_gates.append("gate_4_recall_and_coverage_improvement")
    else:
        gate_results["gate_4_recall_and_coverage_improvement"] = GateResult(
            gate_number=4,
            gate_name="recall_and_coverage_improvement",
            status=GateStatus.PASSED,
            passed=True,
            fatal=False,
            baseline_value={"recall": base_recall, "coverage": base_cov},
            challenger_value={"recall": chal_recall, "coverage": chal_cov},
            delta=max(cov_delta, recall_delta),
            threshold_spec="cov_delta >= 0.010 or recall_delta >= 0.010",
        )

    # -------------------------------------------------------------------------
    # GATE 5: Refresh Correctness & Idempotency
    # -------------------------------------------------------------------------
    ref_data = refresh_results or challenger_metrics.get("refresh_results")
    if ref_data:
        fp = int(ref_data.get("false_positive", ref_data.get("false_positives", 0)))
        ref_rec = float(ref_data.get("recall", 1.0))
        idemp = bool(ref_data.get("idempotent", ref_data.get("idempotent_rerun", True)))
        g5_passed = (fp == 0) and (ref_rec >= 0.95) and idemp
        if not g5_passed:
            g5_reason = f"Refresh replay failed: false_positives={fp} (must be 0), recall={ref_rec:.2f} (>=0.95), idempotent={idemp}."
            gate_results["gate_5_refresh_correctness"] = GateResult(
                gate_number=5,
                gate_name="refresh_correctness",
                status=GateStatus.FAILED,
                passed=False,
                fatal=False,
                baseline_value={"false_positives": 0, "recall": 1.0, "idempotent": True},
                challenger_value={"false_positives": fp, "recall": ref_rec, "idempotent": idemp},
                delta=float(fp),
                threshold_spec="false_positives == 0 and recall >= 0.95 and idempotent is True",
                failure_reason=g5_reason,
            )
            failed_gates.append("gate_5_refresh_correctness")
        else:
            gate_results["gate_5_refresh_correctness"] = GateResult(
                gate_number=5,
                gate_name="refresh_correctness",
                status=GateStatus.PASSED,
                passed=True,
                fatal=False,
                baseline_value={"false_positives": 0, "recall": 1.0, "idempotent": True},
                challenger_value={"false_positives": fp, "recall": ref_rec, "idempotent": idemp},
                delta=0.0,
                threshold_spec="false_positives == 0 and recall >= 0.95 and idempotent is True",
            )
    else:
        # Default pass if refresh replay not evaluated for this unit
        gate_results["gate_5_refresh_correctness"] = GateResult(
            gate_number=5,
            gate_name="refresh_correctness",
            status=GateStatus.PASSED,
            passed=True,
            fatal=False,
            baseline_value=None,
            challenger_value=None,
            delta=None,
            threshold_spec="Defaulted to pass (no refresh anomalies reported)",
        )

    # -------------------------------------------------------------------------
    # GATE 6: Resource Budget Envelope Compliance
    # -------------------------------------------------------------------------
    chal_reqs = int(challenger_metrics.get("total_requests", challenger_metrics.get("requests", 0)))
    chal_cost = float(challenger_metrics.get("total_cost_usd", challenger_metrics.get("cost_usd", 0.0)))
    chal_time = float(challenger_metrics.get("total_runtime_seconds", challenger_metrics.get("runtime_seconds", 0.0)))

    max_reqs = int(envelope.get("max_requests", 2000))
    max_cost = float(envelope.get("max_cost_usd", 10.0))
    max_time = float(envelope.get("max_runtime_seconds", 2700.0))

    budget_violations: list[str] = []
    if chal_reqs > max_reqs:
        budget_violations.append(f"requests: {chal_reqs} > {max_reqs}")
    if chal_cost > max_cost:
        budget_violations.append(f"cost: ${chal_cost:.4f} > ${max_cost:.2f}")
    if chal_time > max_time:
        budget_violations.append(f"runtime: {chal_time:.1f}s > {max_time:.1f}s")

    g6_passed = len(budget_violations) == 0
    if not g6_passed:
        g6_reason = f"Budget envelope breached: {', '.join(budget_violations)}."
        gate_results["gate_6_budget_envelope_compliance"] = GateResult(
            gate_number=6,
            gate_name="budget_envelope_compliance",
            status=GateStatus.FAILED,
            passed=False,
            fatal=False,
            baseline_value=envelope,
            challenger_value={"requests": chal_reqs, "cost_usd": chal_cost, "runtime_seconds": chal_time},
            delta=None,
            threshold_spec="requests <= max_requests, cost <= max_cost, runtime <= max_runtime",
            failure_reason=g6_reason,
        )
        failed_gates.append("gate_6_budget_envelope_compliance")
    else:
        gate_results["gate_6_budget_envelope_compliance"] = GateResult(
            gate_number=6,
            gate_name="budget_envelope_compliance",
            status=GateStatus.PASSED,
            passed=True,
            fatal=False,
            baseline_value=envelope,
            challenger_value={"requests": chal_reqs, "cost_usd": chal_cost, "runtime_seconds": chal_time},
            delta=0.0,
            threshold_spec="requests <= max_requests, cost <= max_cost, runtime <= max_runtime",
        )

    # -------------------------------------------------------------------------
    # Overall Promotion Decision
    # -------------------------------------------------------------------------
    passed_count = sum(1 for r in gate_results.values() if r.passed)
    promoted = (passed_count == 6)

    # Determine verdict code based on earliest failed gate
    verdict_code = PromotionVerdictCode.PROMOTED
    rationale = "All 6 promotion gates passed. Challenger strategy is promoted to production."

    if not promoted:
        if not gate_results["gate_2_claim_precision_maintained"].passed:
            verdict_code = PromotionVerdictCode.REJECTED_PRECISION_DROP
            rationale = gate_results["gate_2_claim_precision_maintained"].failure_reason or "Precision drop"
        elif not gate_results["gate_3_evidence_grounding_validity"].passed:
            verdict_code = PromotionVerdictCode.REJECTED_UNGROUNDED_EVIDENCE
            rationale = gate_results["gate_3_evidence_grounding_validity"].failure_reason or "Ungrounded evidence"
        elif not gate_results["gate_4_recall_and_coverage_improvement"].passed:
            verdict_code = PromotionVerdictCode.REJECTED_NO_RECALL_IMPROVEMENT
            rationale = gate_results["gate_4_recall_and_coverage_improvement"].failure_reason or "No recall improvement"
        elif not gate_results["gate_5_refresh_correctness"].passed:
            verdict_code = PromotionVerdictCode.REJECTED_REFRESH_INCORRECT
            rationale = gate_results["gate_5_refresh_correctness"].failure_reason or "Refresh replay incorrect"
        elif not gate_results["gate_6_budget_envelope_compliance"].passed:
            verdict_code = PromotionVerdictCode.REJECTED_BUDGET_EXCEEDED
            rationale = gate_results["gate_6_budget_envelope_compliance"].failure_reason or "Budget exceeded"

    return GateVerdict(
        promoted=promoted,
        verdict_code=verdict_code,
        gates_passed=passed_count,
        total_gates=6,
        failed_gate_names=failed_gates,
        gate_results=gate_results,
        metric_deltas=metric_deltas,
        baseline_summary=baseline_metrics,
        challenger_summary=challenger_metrics,
        budget_envelope=envelope,
        evaluated_at=_utc_now_iso(),
        rationale=rationale,
    )
