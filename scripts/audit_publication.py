#!/usr/bin/env python3
"""Local after-run integrity/coverage audit, not independent fact verification.

Usage:
    python scripts/audit_publication.py --envelopes out/pr3-smoke/envelopes.jsonl \
        --batch-report out/pr3-smoke/batch-report.json \
        --output out/pr3-smoke/publication-audit.json
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections import Counter
from pathlib import Path
from typing import Any

INVALID_HASH = hashlib.sha256(b"").hexdigest()


def _valid_source(item: dict[str, Any]) -> bool:
    digest = str(item.get("content_sha256") or "")
    return (
        str(item.get("source_url") or "").startswith(("https://", "http://"))
        and re.fullmatch(r"[0-9a-fA-F]{64}", digest) is not None
        and digest.lower() != INVALID_HASH
    )


def audit(
    envelopes: list[dict[str, Any]], batch_report: dict[str, Any]
) -> dict[str, Any]:
    counts: Counter[str] = Counter()
    web_states: Counter[str] = Counter()
    sample_errors: list[dict[str, str]] = []
    organisation_numbers: set[str] = set()
    company_requests = 0
    for env in envelopes:
        org = str(env.get("organisation_number") or "")
        if org in organisation_numbers or not org:
            sample_errors.append({"organisation_number": org, "reason": "duplicate_or_missing_organisation_number"})
        organisation_numbers.add(org)
        evidence = {e.get("id"): e for e in env.get("evidence") or [] if e.get("id")}
        claims = {c.get("field"): c for c in env.get("claims") or []}
        site = claims.get("official_website") or {}
        web_states[site.get("availability") or "missing"] += 1
        for field in ("dated_news", "job_postings", "registered_business_address", "accounting_obligation"):
            if (claims.get(field) or {}).get("availability") == "available":
                counts["available_" + field] += 1
        for claim in env.get("claims") or []:
            if claim.get("availability") != "available":
                continue
            counts["available_claims"] += 1
            refs = claim.get("evidence_ids") or []
            if not refs or not any(_valid_source(evidence.get(ref) or {}) for ref in refs):
                counts["available_claims_without_valid_source"] += 1
                sample_errors.append({"organisation_number": org, "reason": "available_claim_has_no_valid_source",
                                      "field": str(claim.get("field") or "")})
            if any((evidence.get(ref) or {}).get("claim_span_verification") == "source_excerpt"
                   for ref in refs):
                counts["available_claims_with_source_excerpt"] += 1
        synth = env.get("synthesis") or {}
        for sentence in synth.get("sentence_citations") or []:
            for ref in sentence.get("evidence_ids") or []:
                if ref not in evidence:
                    counts["dangling_synthesis_references"] += 1
                    sample_errors.append({"organisation_number": org, "reason": "dangling_synthesis_reference",
                                          "evidence_id": str(ref)})
        counts["source_excerpt_evidence_items"] += sum(
            1 for row in evidence.values() if row.get("claim_span_verification") == "source_excerpt"
        )
        operations = env.get("operations") or {}
        company_requests += int(operations.get("requests") or 0)
        if (operations.get("request_attribution") or "") == "measured_thread_context":
            counts["companies_with_request_attribution"] += 1
        counts["companies"] += 1
    budget = batch_report.get("budget") or {}
    total = budget.get("total_requests")
    unattributed = budget.get("requests_unattributed")
    reconciliation = (
        isinstance(total, int) and isinstance(unattributed, int)
        and company_requests + unattributed == total
    )
    # Stable zero-valued metrics are useful for scripts, comparisons and CI.
    for metric in (
        "available_claims", "available_claims_without_valid_source",
        "available_claims_with_source_excerpt", "dangling_synthesis_references",
        "source_excerpt_evidence_items", "companies_with_request_attribution",
        "available_dated_news", "available_job_postings",
        "available_registered_business_address", "available_accounting_obligation",
    ):
        counts.setdefault(metric, 0)
    result = {
        "report_kind": "internal_publication_audit_not_external_fact_check",
        "batch_company_count_matches": len(envelopes) == batch_report.get("expected_count"),
        "unique_organisation_numbers": len(organisation_numbers) == len(envelopes),
        "counts": dict(counts),
        "official_website_availability": dict(web_states),
        "requests": {
            "company_attributed_sum": company_requests,
            "shared_or_unattributed": unattributed,
            "batch_total": total,
            "reconciles": reconciliation,
        },
        "examples": sample_errors[:25],
        "limitations": [
            "A source hash, excerpt and identity reference do not independently verify the underlying claim.",
            "Unverified website candidates, uncrawled press, and missing job adverts are not automatically counted as unavailable in the real world.",
            "Publication counts are not the official Signalpost leaderboard score.",
        ],
    }
    result["passed"] = bool(
        result["batch_company_count_matches"]
        and result["unique_organisation_numbers"]
        and counts["dangling_synthesis_references"] == 0
        and counts["available_claims_without_valid_source"] == 0
        and reconciliation
    )
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--envelopes", required=True, type=Path)
    parser.add_argument("--batch-report", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    envelopes = [
        json.loads(line) for line in args.envelopes.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    report = json.loads(args.batch_report.read_text(encoding="utf-8"))
    result = audit(envelopes, report)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
