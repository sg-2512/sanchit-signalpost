"""Held-out connector promotion gates.

Promotion is deliberately based on reviewer labels, not connector assertions.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import json
from typing import Any

from .source_policy import source_registry


@dataclass(frozen=True)
class PromotionReport:
    strategy: str
    reviewed: int
    accepted: int
    wrong_company: int
    unsupported: int
    evidence_incomplete: int
    coverage_gain: int
    promoted: bool
    reasons: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "strategy": self.strategy,
            "reviewed": self.reviewed,
            "accepted": self.accepted,
            "wrong_company": self.wrong_company,
            "unsupported": self.unsupported,
            "evidence_incomplete": self.evidence_incomplete,
            "coverage_gain": self.coverage_gain,
            "promoted": self.promoted,
            "reasons": list(self.reasons),
        }


def evaluate_promotion(strategy: str, rows: list[dict[str, Any]], *, minimum_gain: int = 1) -> PromotionReport:
    """Require reviewed held-out rows and reject any material safety regression."""
    scoped = [row for row in rows if row.get("strategy") == strategy]
    reviewed = [row for row in scoped if row.get("reviewed") is True]
    wrong_company = sum(row.get("exact_entity") is False for row in reviewed)
    unsupported = sum(row.get("evidence_supported") is False for row in reviewed)
    evidence_incomplete = sum(row.get("evidence_complete") is False for row in reviewed)
    accepted = sum(
        row.get("exact_entity") is True
        and row.get("evidence_supported") is True
        and row.get("evidence_complete") is True
        for row in reviewed
    )
    coverage_gain = len({str(row.get("organisation_number") or "") for row in reviewed if row.get("new_supported_claim") is True})
    reasons: list[str] = []
    if not reviewed:
        reasons.append("no independently reviewed held-out observations")
    if wrong_company:
        reasons.append("wrong-company publication detected")
    if unsupported:
        reasons.append("unsupported claim detected")
    if evidence_incomplete:
        reasons.append("required evidence missing")
    if coverage_gain < minimum_gain:
        reasons.append("insufficient measured coverage gain")
    return PromotionReport(
        strategy=strategy,
        reviewed=len(reviewed),
        accepted=accepted,
        wrong_company=wrong_company,
        unsupported=unsupported,
        evidence_incomplete=evidence_incomplete,
        coverage_gain=coverage_gain,
        promoted=not reasons,
        reasons=tuple(reasons),
    )


def promotion_report(
    strategy: str,
    rows: list[dict[str, Any]],
    run_metrics: dict[str, Any],
    *,
    max_requests: int = 2_000,
    max_runtime_seconds: float = 2_700,
    max_cost_usd: float = 10.0,
    minimum_gain: int = 1,
) -> dict[str, Any]:
    """Answer the five required promotion questions with auditable fields."""
    decision = evaluate_promotion(strategy, rows, minimum_gain=minimum_gain)
    declaration = source_registry().get(strategy)
    requests = int(run_metrics.get("requests") or 0)
    runtime_seconds = float(run_metrics.get("runtime_seconds") or 0)
    cost_usd = float(run_metrics.get("third_party_cost_usd") or 0)
    budget_ok = requests <= max_requests and runtime_seconds <= max_runtime_seconds and cost_usd <= max_cost_usd
    access_ok = bool(declaration and declaration.get("publication") == "official_safe")
    evidence_ok = decision.evidence_incomplete == 0 and decision.unsupported == 0
    wrong_company_ok = decision.wrong_company == 0
    coverage_ok = decision.coverage_gain >= minimum_gain
    promoted = decision.promoted and budget_ok and access_ok
    reasons = list(decision.reasons)
    if not declaration:
        reasons.append("connector has no source-rights declaration")
    elif not access_ok:
        reasons.append(f"connector publication state is {declaration.get('publication')}, not official_safe")
    if not budget_ok:
        reasons.append("request, runtime, or cost budget exceeded")
    return {
        "report_kind": "connector_promotion_held_out_v1",
        "strategy": strategy,
        "source_rights": declaration,
        "questions": {
            "adds_facts_for_new_companies": {"passed": coverage_ok, "new_companies": decision.coverage_gain},
            "zero_wrong_company_claims": {"passed": wrong_company_ok, "wrong_company_claims": decision.wrong_company},
            "every_fact_has_source_date_hash_span": {"passed": evidence_ok, "unsupported": decision.unsupported, "evidence_incomplete": decision.evidence_incomplete},
            "within_request_runtime_cost_budget": {
                "passed": budget_ok,
                "actual": {"requests": requests, "runtime_seconds": runtime_seconds, "third_party_cost_usd": cost_usd},
                "limits": {"requests": max_requests, "runtime_seconds": max_runtime_seconds, "third_party_cost_usd": max_cost_usd},
            },
            "access_permitted_and_reproducible": {"passed": access_ok, "declaration": declaration},
        },
        "review": decision.to_dict(),
        "promoted": promoted,
        "reasons": reasons,
    }


def write_promotion_report(path: str | Path, report: dict[str, Any]) -> None:
    Path(path).write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
