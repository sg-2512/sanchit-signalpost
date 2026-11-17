#!/usr/bin/env python3
"""Compare deterministic coverage/precision regressions in two envelope batches.

This is a consistency audit, not a live source fact check or Builderr score.
"""
from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path
from typing import Any


def rows(path: Path) -> dict[str, dict[str, Any]]:
    records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    result = {}
    for record in records:
        org = str(record.get("organisation_number") or "")
        if not org or org in result:
            raise ValueError(f"Missing or duplicated organisation number: {org}")
        result[org] = record
    return result


def score(envelopes: dict[str, dict[str, Any]]) -> dict[str, Any]:
    totals: Counter[str] = Counter()
    by_company: list[dict[str, Any]] = []
    for org, env in sorted(envelopes.items()):
        profile = env.get("profile") or {}
        evid = profile.get("evidence") or {}
        claims = {c.get("field"): c for c in env.get("claims") or []}
        def available(field: str) -> bool:
            return (claims.get(field) or {}).get("availability") == "available"
        account = evid.get("accounting_obligation") or {}
        registered = evid.get("registered_address") or {}
        if (account.get("status") == "available"
                and (account.get("value") or {}).get("classification")
                and not available("accounting_obligation")):
            totals["accounting_claim_contradictions"] += 1
        # The previous agent did not create registered_address evidence when
        # it found subunits, so inspect the authoritative registry row as well.
        live_row = (evid.get("registry_live") or {}).get("value") or {}
        bulk_row = (evid.get("registry") or {}).get("value") or {}
        raw_address = (
            live_row.get("forretningsadresse") or live_row.get("business_address")
            or bulk_row.get("forretningsadresse")
            or any(bulk_row.get(f"forretningsadresse.{k}") for k in
                   ("adresse", "postnummer", "poststed", "kommune"))
        )
        if ((registered.get("status") == "available" and
             (registered.get("value") or {}).get("address")) or raw_address) and not available("registered_business_address"):
            totals["registered_address_omissions"] += 1
        if available("official_website"):
            totals["verified_official_websites"] += 1
        if available("registry_declared_website"):
            totals["registry_site_candidates_not_necessarily_verified"] += 1
        if available("dated_news"):
            totals["companies_with_dated_news"] += 1
        if available("job_postings"):
            totals["companies_with_verified_jobs"] += 1
        if (claims.get("official_website") or {}).get("availability") == "blocked":
            totals["blocked_website_status"] += 1
        stats = (env.get("operations") or {})
        if isinstance(stats.get("runtime_ms"), (int, float)) and stats["runtime_ms"] > 0:
            totals["companies_with_measured_runtime"] += 1
        if stats.get("third_party_cost_usd") is None:
            totals["per_company_cost_unattributed"] += 1
        synth = env.get("synthesis") or {}
        if synth.get("citation_density_pct") == 100.0:
            totals["claimed_100_percent_citation_density"] += 1
        for claim in env.get("claims") or []:
            if claim.get("availability") == "available":
                totals["available_claims"] += 1
                evidence_map = {e.get("id"): e for e in env.get("evidence") or []}
                if not any((evidence_map.get(eid) or {}).get("claim_span")
                           for eid in claim.get("evidence_ids") or []):
                    totals["available_claims_without_source_excerpt"] += 1
        totals["companies"] += 1
        by_company.append({
            "organisation_number": org,
            "company_name": (env.get("legal_identity") or {}).get("name"),
            "accounting_claim_status": (claims.get("accounting_obligation") or {}).get("availability"),
            "registered_address_status": (claims.get("registered_business_address") or {}).get("availability"),
            "verified_website_status": (claims.get("official_website") or {}).get("availability"),
            "registry_website_candidate_status": (claims.get("registry_declared_website") or {}).get("availability"),
            "citation_density_pct": synth.get("citation_density_pct"),
        })
    return {"totals": dict(totals), "companies": by_company}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--before", type=Path, required=True, help="Older envelopes.jsonl")
    parser.add_argument("--after", type=Path, required=True, help="New envelopes.jsonl")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    older = rows(args.before)
    newer = rows(args.after)
    matching = set(older) == set(newer)
    before = score(older)
    after = score(newer)
    keys = sorted(set(before["totals"]) | set(after["totals"]))
    differences = {
        key: {
            "before": before["totals"].get(key, 0),
            "after": after["totals"].get(key, 0),
            "change": after["totals"].get(key, 0) - before["totals"].get(key, 0),
        }
        for key in keys
    }
    report = {
        "report_kind": "internal_regression_comparison_not_external_validation",
        "same_organisation_numbers": matching,
        "before_companies": len(older),
        "after_companies": len(newer),
        "differences": differences,
        "after_companies_detail": after["companies"],
        "limitations": [
            "No independent Bronnoysund lookup or website authentication is performed.",
            "Source hashes and evidence IDs alone do not establish excerpt support.",
            "Company statistics can change as source filing periods change.",
            "An unverified registry website is not a publishable official website.",
        ],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({k: v for k, v in report.items() if k != "after_companies_detail"},
                     ensure_ascii=False, indent=2))
    return 0 if matching else 1


if __name__ == "__main__":
    raise SystemExit(main())
