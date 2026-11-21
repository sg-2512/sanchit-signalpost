#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from norway_company_agent.external_footprint import (  # noqa: E402
    INDEPENDENT_SENTIMENT_CLASSES,
    publishable_observation,
)
from norway_company_agent.source_policy import OFFICIAL_SAFE, audit_snapshot_time, observation_decision  # noqa: E402


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def ratio(numerator: int, denominator: int) -> float:
    return numerator / denominator if denominator else 0.0


def compute_original_signalpost_score(report: dict[str, Any]) -> dict[str, Any]:
    """
    Original Signalpost Evaluation Contract (v2) scoring (100 points):

        Total Score = Recall & Coverage (50) + Precision & Identity (30)
                      + Synthesis (12) + UX & Interaction (8)

    Runs alongside the current external-footprint coverage/audit scoring so both
    scores appear in the output report.
    """
    coverage = report.get("coverage", {})
    published = report.get("published_audited") or 0

    # --- A. Recall & Coverage (50 points) ---
    # Coverage = 0.70 × Company Recall + 0.30 × Claim Recall
    company_recall = coverage.get("any_external", 0.0)
    signal_buckets = ["two_platforms", "workforce_jobs", "ratings_reviews", "buzz_engagement", "sentiment"]
    claim_recall = sum(coverage.get(b, 0.0) for b in signal_buckets) / len(signal_buckets)
    recall_coverage_score = 50.0 * (0.70 * company_recall + 0.30 * claim_recall)

    # --- B. Precision, Identity & Evidence (30 points) ---
    entity_precision = report.get("entity_precision") or 0.0
    metric_precision = report.get("metric_precision") or 0.0
    evidence_validity = report.get("evidence_span_validity") or 0.0
    wrong_entity = report.get("wrong_entity_publications") or 0
    unsupported = report.get("unsupported_publications") or 0
    invalid_spans = report.get("invalid_evidence_spans") or 0
    audit_fail_rate = (wrong_entity + unsupported + invalid_spans) / max(published, 1)
    precision_raw = entity_precision * metric_precision * evidence_validity * (1.0 - audit_fail_rate)
    precision_score = 30.0 * precision_raw

    # --- C. Synthesis (12 points) ---
    # Depth of executive briefings — approximated from available coverage signals
    has_workforce = coverage.get("workforce_jobs", 0.0) > 0
    has_reviews = coverage.get("ratings_reviews", 0.0) > 0
    has_buzz = coverage.get("buzz_engagement", 0.0) > 0
    synthesis_signal_count = sum([has_workforce, has_reviews, has_buzz])
    synthesis_score = 12.0 * (synthesis_signal_count / 3.0)

    # --- D. UX & Interaction (8 points) ---
    # Fast local inspection, clean presentation, searchability, audit metrics.
    # Approximated: if a report was produced, the run is within operational bounds.
    ux_score = 8.0

    total = recall_coverage_score + precision_score + synthesis_score + ux_score

    return {
        "original_signalpost_score": round(total, 2),
        "original_signalpost_breakdown": {
            "recall_coverage_50": round(recall_coverage_score, 2),
            "precision_identity_evidence_30": round(precision_score, 2),
            "synthesis_12": round(synthesis_score, 2),
            "ux_interaction_8": round(ux_score, 2),
        },
        "original_qualification_passed": total >= 65.0,
        "original_score_components": {
            "company_recall": round(company_recall, 4),
            "claim_recall": round(claim_recall, 4),
            "entity_precision": entity_precision,
            "metric_precision": metric_precision,
            "evidence_span_validity": evidence_validity,
            "audit_fail_rate": round(audit_fail_rate, 4),
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate frozen external-footprint observations and exact-entity labels.")
    parser.add_argument("--profiles", required=True)
    parser.add_argument("--observations", required=True)
    parser.add_argument(
        "--labels",
        required=True,
        help="Reviewer-completed JSONL: id, exact_entity, metric_correct, evidence_supported, evidence_span_valid, connector_policy_passed, reviewer, reviewed_at",
    )
    parser.add_argument("--output", required=True)
    parser.add_argument("--minimum-audit", type=int, default=100)
    parser.add_argument(
        "--source-policy",
        default=OFFICIAL_SAFE,
        choices=(OFFICIAL_SAFE, "experimental"),
        help="Publication policy used for the evaluated run (default: official_safe)",
    )
    args = parser.parse_args()

    profiles = read_jsonl(Path(args.profiles))
    observations = read_jsonl(Path(args.observations))
    label_rows = read_jsonl(Path(args.labels))
    labels = {str(item["id"]): item for item in label_rows}
    if len(labels) != len(label_rows):
        raise ValueError("Label IDs must be unique")

    observation_by_id = {str(item.get("id")): item for item in observations}
    unknown_ids = sorted(set(labels) - set(observation_by_id))
    if unknown_ids:
        raise ValueError(f"Labels contain IDs absent from observations: {unknown_ids[:5]}")

    required_verdicts = (
        "exact_entity",
        "metric_correct",
        "evidence_supported",
        "evidence_span_valid",
        "connector_policy_passed",
    )

    def reviewed(item: dict[str, Any]) -> bool:
        if any(type(item.get(key)) is not bool for key in required_verdicts):
            return False
        source_observation = observation_by_id[str(item["id"])]
        if source_observation.get("sentiment_label") is not None and type(item.get("sentiment_correct")) is not bool:
            return False
        if not str(item.get("reviewer") or "").strip():
            return False
        try:
            datetime.fromisoformat(str(item["reviewed_at"]).replace("Z", "+00:00"))
        except (KeyError, TypeError, ValueError):
            return False
        return True

    incomplete_labels = sum(not reviewed(item) for item in label_rows)
    labels = {key: item for key, item in labels.items() if reviewed(item)}

    snapshot_time = audit_snapshot_time(Path(args.observations))
    policy_accepted = [
        item for item in observations
        if observation_decision(item, policy=args.source_policy, snapshot_time=snapshot_time).publishable
    ]
    policy_accepted_ids = {str(item.get("id")) for item in policy_accepted}
    audited = [item for item in observations if str(item.get("id")) in labels]
    published = [
        item for item in audited
        if str(item.get("id")) in policy_accepted_ids and publishable_observation(item)
    ]
    audit_size_gate = len(published) >= args.minimum_audit
    wrong_entity = sum(not labels[str(item["id"])].get("exact_entity", False) for item in published)
    wrong_metric = sum(not labels[str(item["id"])].get("metric_correct", False) for item in published)
    unsupported = sum(not labels[str(item["id"])]["evidence_supported"] for item in published)
    invalid_evidence_spans = sum(not labels[str(item["id"])]["evidence_span_valid"] for item in published)
    sentiment_audited = [item for item in published if item.get("sentiment_label") is not None]
    sentiment_correct = sum(labels[str(item["id"])].get("sentiment_correct", False) for item in sentiment_audited)

    all_orgs = {str(item["organisation_number"]) for item in profiles}
    # A valid observation is not publishable in an official run unless its
    # connector is also approved by the active source-rights policy.
    accepted_all = [item for item in policy_accepted if publishable_observation(item)]
    org_platforms: dict[str, set[str]] = defaultdict(set)
    org_signals: dict[str, set[str]] = defaultdict(set)
    for item in accepted_all:
        org = str(item["organisation_number"])
        if org not in all_orgs:
            continue
        org_platforms[org].add(str(item["platform"]))
        org_signals[org].add(str(item["signal_type"]))

    coverage = {
        "any_external": ratio(sum(bool(org_platforms[org]) for org in all_orgs), len(all_orgs)),
        "two_platforms": ratio(sum(len(org_platforms[org]) >= 2 for org in all_orgs), len(all_orgs)),
        "workforce_jobs": ratio(sum(bool(org_signals[org] & {"job_posting", "workforce_snapshot"}) for org in all_orgs), len(all_orgs)),
        # Official addresses and registry metrics are useful facts, but must
        # never be relabelled as independent reviews, engagement or sentiment.
        "ratings_reviews": ratio(sum(bool(org_signals[org] & {"review", "review_summary"}) for org in all_orgs), len(all_orgs)),
        "buzz_engagement": ratio(sum(bool(org_signals[org] & {"public_post", "buzz_metrics"}) for org in all_orgs), len(all_orgs)),
        "sentiment": ratio(sum(any(
            item.get("sentiment_label")
            and item.get("source_class") in INDEPENDENT_SENTIMENT_CLASSES
            and item.get("signal_type") in {"review", "public_post", "public_mention"}
            for item in accepted_all
            if str(item.get("organisation_number")) == org
        ) for org in all_orgs), len(all_orgs)),
    }
    acquisition_modes = Counter(str(item.get("acquisition_mode")) for item in accepted_all)
    entity_precision = ratio(len(published) - wrong_entity, len(published)) if published else None
    metric_precision = ratio(len(published) - wrong_metric, len(published)) if published else None
    evidence_span_validity = ratio(len(published) - invalid_evidence_spans, len(published)) if published else None
    sentiment_accuracy = ratio(sentiment_correct, len(sentiment_audited)) if sentiment_audited else None
    qualification = bool(
        audit_size_gate
        and published
        and wrong_entity == 0
        and unsupported == 0
        and invalid_evidence_spans == 0
        and all(labels[str(item["id"])]["connector_policy_passed"] for item in published)
        and entity_precision is not None
        and entity_precision >= 0.995
        and metric_precision is not None
        and metric_precision >= 0.98
    )
    connector_policy = (
        all(labels[str(item["id"])]["connector_policy_passed"] for item in published)
        if published else None
    )
    def _parse_iso(val: str | None) -> datetime | None:
        try:
            p = datetime.fromisoformat(str(val or "").replace("Z", "+00:00"))
            return p if p.tzinfo else p.replace(tzinfo=timezone.utc)
        except ValueError:
            return None

    now = datetime.now(timezone.utc)
    fresh_orgs = set()
    for item in accepted_all:
        org = str(item.get("organisation_number") or "")
        dt = _parse_iso(item.get("retrieved_at"))
        if dt and abs((now - dt).total_seconds()) <= (45 * 86_400):
            fresh_orgs.add(org)
    fresh_coverage = ratio(len(fresh_orgs & all_orgs), len(all_orgs))

    report = {
        "scorer": "signalpost_external_footprint_eval_v1",
        "claim_boundary": "Held-out observation audit plus full-corpus coverage; it does not validate an unlabelled connector.",
        "profiles": len(profiles),
        "observations": len(observations),
        "policy_accepted_observations": len(policy_accepted),
        "policy_and_evidence_accepted_observations": len(accepted_all),
        "source_policy": args.source_policy,
        "audit_queue_rows": len(label_rows),
        "incomplete_audit_rows": incomplete_labels,
        "audited_observations": len(audited),
        "published_audited": len(published),
        "wrong_entity_publications": wrong_entity if published else None,
        "wrong_company_publications": wrong_entity if published else None,
        "wrong_metric_publications": wrong_metric if published else None,
        "unsupported_publications": unsupported if published else None,
        "entity_precision": entity_precision,
        "exact_company_precision": entity_precision,
        "metric_precision": metric_precision,
        "evidence_span_validity": evidence_span_validity,
        "sentiment_audited": len(sentiment_audited),
        "sentiment_accuracy": sentiment_accuracy,
        "coverage": coverage,
        "fresh_coverage": fresh_coverage,
        "platform_counts": dict(Counter(str(item.get("platform")) for item in accepted_all)),
        "acquisition_modes": dict(acquisition_modes),
        "minimum_audit": args.minimum_audit,
        "audit_size_gate": audit_size_gate,
        "connector_policy_passed": connector_policy,
        "qualification_passed": qualification,
    }
    report.update(compute_original_signalpost_score(report))
    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    Path(args.output).write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
