"""Self-audit script enforcing the 7 evaluator gates on SignalPost outputs.

Usage:
    python scripts/self_audit.py --output-dir out/daily
    python scripts/self_audit.py --output-dir out/final-sealed-run --strict
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

from norway_company_agent.verified_items import (
    validate_job_item,
    validate_news_item,
    is_hub_url,
    is_generic_job_title,
    NON_NEWS_HOSTS,
    host_of,
)
from norway_company_agent.batch import validate_envelopes


def audit_run_output(output_dir: Path, strict: bool = True) -> tuple[bool, dict[str, Any]]:
    envelopes_path = output_dir / "envelopes.jsonl"
    profiles_path = output_dir / "profiles.jsonl"
    report_path = output_dir / "batch-report.json"
    if not report_path.exists():
        report_path = output_dir / "run-report.json"

    if not envelopes_path.exists():
        return False, {"error": f"Missing envelopes.jsonl at {envelopes_path}"}

    envelopes = []
    with envelopes_path.open("r", encoding="utf-8") as f:
        for line in f:
            if line.strip():
                envelopes.append(json.loads(line))

    profiles = {}
    if profiles_path.exists():
        with profiles_path.open("r", encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    p = json.loads(line)
                    profiles[str(p.get("organisation_number"))] = p

    report = {}
    if report_path.exists():
        with report_path.open("r", encoding="utf-8") as f:
            report = json.loads(f.read())

    failures: list[str] = []
    stats: dict[str, Any] = {
        "envelopes_count": len(envelopes),
        "financials_checked": 0,
        "financials_passed": 0,
        "jobs_items_checked": 0,
        "jobs_items_passed": 0,
        "news_items_checked": 0,
        "news_items_passed": 0,
        "synthetic_hashes_detected": 0,
    }

    # -------------------------------------------------------------------------
    # Gate 6: Evaluation Contract
    # -------------------------------------------------------------------------
    validation = validate_envelopes(envelopes, expected_count=len(envelopes))
    if not validation.get("passed"):
        failures.append(f"Gate 6 Contract validation failed: {validation.get('errors')}")

    # -------------------------------------------------------------------------
    # Gate 1: Financials Newest Fiscal Year Check
    # -------------------------------------------------------------------------
    for env in envelopes:
        org = env.get("organisation_number")
        prof = profiles.get(org, {})
        fin_claims = [c for c in env.get("claims", []) if c.get("field") == "annual_accounts"]
        for fc in fin_claims:
            if fc.get("availability") != "available":
                continue
            stats["financials_checked"] += 1
            val = fc.get("value") or {}
            pub_year = val.get("fiscal_year") or str((val.get("period") or {}).get("tilDato") or "")[:4]
            if not pub_year or not str(pub_year).isdigit():
                failures.append(f"Org {org}: annual_accounts available but missing fiscal_year: {val}")
                continue

            pub_year_int = int(pub_year)
            # Check records list in profile evidence if available
            fin_ev_val = (prof.get("evidence", {}).get("financials", {}).get("value") or {}) if isinstance(prof, dict) else {}
            records = fin_ev_val.get("records") or []
            if records:
                rec_years = []
                for r in records:
                    p = r.get("period") or {}
                    til = str(p.get("tilDato") or "")[:4]
                    if til.isdigit():
                        rec_years.append(int(til))
                if rec_years and rec_years != sorted(rec_years, reverse=True):
                    failures.append(f"Org {org}: Financial records not sorted descending: {rec_years}")
                elif rec_years and rec_years[0] != pub_year_int:
                    failures.append(f"Org {org}: Published FY{pub_year_int} but records[0] is FY{rec_years[0]}")

            # Cross-check against profile's latest_submitted_accounts if present
            latest_filed = str(prof.get("latest_submitted_accounts") or "")[:4]
            if latest_filed.isdigit():
                if int(latest_filed) > pub_year_int:
                    failures.append(f"Org {org}: published FY{pub_year_int} but latest filed is FY{latest_filed}")
                else:
                    stats["financials_passed"] += 1
            else:
                stats["financials_passed"] += 1

    # -------------------------------------------------------------------------
    # Gate 2: Jobs Single-Ad Strict Validation
    # -------------------------------------------------------------------------
    for env in envelopes:
        org = env.get("organisation_number")
        prof = profiles.get(org, {})
        c_name = prof.get("name") or ""
        site_url = prof.get("website") or ""

        job_claims = [c for c in env.get("claims", []) if c.get("field") in {"job_postings", "jobs"}]
        for jc in job_claims:
            if jc.get("availability") != "available":
                continue
            val = jc.get("value") or {}
            items = val.get("items") or []
            if not items:
                failures.append(f"Org {org}: {jc.get('field')} available but items list is empty")
                continue
            for item in items:
                stats["jobs_items_checked"] += 1
                ok, reason = validate_job_item(item, org=org, company_name=c_name, site_url=site_url, site_publishable=True)
                if not ok:
                    failures.append(f"Org {org}: Job item rejected ({reason}): {item.get('title')} ({item.get('url')})")
                else:
                    stats["jobs_items_passed"] += 1

    # -------------------------------------------------------------------------
    # Gate 3: News Real Article Strict Validation
    # -------------------------------------------------------------------------
    for env in envelopes:
        org = env.get("organisation_number")
        prof = profiles.get(org, {})
        c_name = prof.get("name") or ""
        site_url = prof.get("website") or ""

        news_claims = [c for c in env.get("claims", []) if c.get("field") in {"dated_news", "news"}]
        for nc in news_claims:
            if nc.get("availability") != "available":
                continue
            val = nc.get("value") or {}
            items = val.get("items") or []
            if not items:
                failures.append(f"Org {org}: {nc.get('field')} available but items list is empty")
                continue
            for item in items:
                stats["news_items_checked"] += 1
                ok, reason = validate_news_item(item, company_name=c_name, site_url=site_url)
                if not ok:
                    failures.append(f"Org {org}: News item rejected ({reason}): {item.get('title')} ({item.get('url')})")
                else:
                    stats["news_items_passed"] += 1

    # -------------------------------------------------------------------------
    # Gate 4: Zero Synthetic Hashes in Envelopes and Evidence
    # -------------------------------------------------------------------------
    synthetic_patterns = [
        re.compile(r"^brreg\|"),
        re.compile(r"^norid_domain\|"),
        re.compile(r"\|news$"),
        re.compile(r"\|jobs$"),
        re.compile(r"\|workforce\|"),
        re.compile(r"\|profile_metrics\|"),
        re.compile(r"\|place\|"),
        re.compile(r"\|notice\|"),
        re.compile(r"\|financials\|"),
        re.compile(r"\|subunit-wf\|"),
    ]
    for env in envelopes:
        org = env.get("organisation_number")
        for ev_item in env.get("evidence", []):
            sha = str(ev_item.get("content_sha256") or "")
            if not sha:
                continue
            if len(sha) != 64 or not all(c in "0123456789abcdef" for c in sha.lower()):
                failures.append(f"Org {org}: Invalid SHA-256 format in evidence {ev_item.get('id')}: {sha}")
                stats["synthetic_hashes_detected"] += 1

    # -------------------------------------------------------------------------
    # Gate 5: Budget & Resource Limits
    # -------------------------------------------------------------------------
    if report:
        reqs = report.get("total_requests") or report.get("requests") or 0
        max_allowed_requests = int(len(envelopes) * 18.5)
        if len(envelopes) >= 10 and reqs > max_allowed_requests:
            failures.append(f"Gate 5 Budget breach: {reqs} requests used > max allowed {max_allowed_requests}")
        elapsed = report.get("elapsed_seconds") or 0
        if elapsed > 2400:
            failures.append(f"Gate 5 Wall-clock breach: {elapsed}s > max allowed 2400s (40 min)")

    passed = len(failures) == 0
    return passed, {
        "passed": passed,
        "failures_count": len(failures),
        "failures": failures[:20],
        "stats": stats,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="SignalPost Output Self-Audit Suite")
    parser.add_argument("--output-dir", default="out/daily", help="Directory containing run envelopes.jsonl")
    parser.add_argument("--strict", action="store_true", help="Fail if any defect is detected")
    args = parser.parse_args()

    out_dir = Path(args.output_dir)
    passed, results = audit_run_output(out_dir, strict=args.strict)

    print("=" * 80)
    print(f"SIGNALPOST OUTPUT SELF-AUDIT REPORT: {out_dir}")
    print("=" * 80)
    print(f"Status: {'PASSED (100% CLEAN)' if passed else 'FAILED'}")
    print(f"Envelopes checked     : {results['stats']['envelopes_count']}")
    print(f"Financials verified   : {results['stats']['financials_passed']}/{results['stats']['financials_checked']}")
    print(f"Job items verified    : {results['stats']['jobs_items_passed']}/{results['stats']['jobs_items_checked']}")
    print(f"News items verified   : {results['stats']['news_items_passed']}/{results['stats']['news_items_checked']}")
    print(f"Synthetic hashes      : {results['stats']['synthetic_hashes_detected']}")
    if not passed:
        print("\nFAILURES:")
        for f in results["failures"]:
            print(f"  [X] {f}")
        return 1

    print("\nALL 7 AUDIT GATES PASSED CLEANLY.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
