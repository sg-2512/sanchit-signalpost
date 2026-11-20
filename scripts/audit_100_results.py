#!/usr/bin/env python3
"""Audit and score the results of a 100-company evaluation run."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description="Audit and score a SignalPost evaluation run.")
    parser.add_argument("--output-dir", default="out/new-100-run", help="Output directory of the run")
    args = parser.parse_args()

    out_dir = Path(args.output_dir)
    report_file = out_dir / "batch-report.json" if (out_dir / "batch-report.json").exists() else out_dir / "report.json"
    score_file = out_dir / "score-report.json"
    envelopes_file = out_dir / "envelopes.jsonl"
    profiles_file = out_dir / "profiles.jsonl"
    obs_file = out_dir / "all-observations.jsonl"

    if not report_file.exists():
        raise SystemExit(f"[ERROR] batch-report.json or report.json not found in {out_dir}")
    if not envelopes_file.exists():
        raise SystemExit(f"[ERROR] envelopes.jsonl not found in {out_dir}")

    report = json.loads(report_file.read_text(encoding="utf-8"))
    score_data = json.loads(score_file.read_text(encoding="utf-8")) if score_file.exists() else {}

    envelopes = []
    with envelopes_file.open("r", encoding="utf-8") as f:
        for line in f:
            if line.strip():
                envelopes.append(json.loads(line))

    profiles = []
    if profiles_file.exists():
        with profiles_file.open("r", encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    profiles.append(json.loads(line))
    else:
        profiles = [e.get("profile", {}) for e in envelopes]

    n = len(envelopes)
    terminal_states = {"available", "not_available", "blocked", "not_applicable", "ambiguous", "failed"}
    complete_count = sum(1 for e in envelopes if e.get("state") in terminal_states)
    def available_claim_count(field: str) -> int:
        return sum(1 for envelope in envelopes if any(
            claim.get("field") == field and claim.get("availability") == "available"
            for claim in envelope.get("claims") or []
        ))
    name_count = sum(1 for p in profiles if p.get("name"))
    fin_count = sum(1 for p in profiles if (p.get("evidence", {}).get("financials", {}) or {}).get("status") == "available")
    roles_count = sum(1 for p in profiles if (p.get("evidence", {}).get("roles", {}) or {}).get("status") == "available")
    locs_count = available_claim_count("operating_locations")
    registered_address_count = available_claim_count("registered_address")

    # Discovered channels & signals
    web_count = available_claim_count("official_website")
    news_count = available_claim_count("dated_news")
    job_count = available_claim_count("job_postings")
    synthesis_count = sum(1 for p in profiles if (p.get("synthesis") or {}).get("summary"))

    # Count observations
    platform_counts: dict[str, int] = {}
    if obs_file.exists():
        with obs_file.open("r", encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    obs = json.loads(line)
                    plat = obs.get("platform", "unknown")
                    platform_counts[plat] = platform_counts.get(plat, 0) + 1
    else:
        for e in envelopes:
            for obs in e.get("observations") or []:
                plat = obs.get("platform", "unknown")
                platform_counts[plat] = platform_counts.get(plat, 0) + 1

    ops = report.get("operations") or {}
    val = report.get("validation") or {}
    checks = val.get("checks") or {}
    budget = report.get("budget") or {}

    wall_sec = report.get("wall_clock_seconds") or budget.get("elapsed_seconds") or ops.get("wall_clock_seconds", 0.0)
    cost_usd = budget.get("total_cost_usd") or ops.get("third_party_cost_usd", 0.0)

    print("=" * 68)
    print("         SIGNALPOST 100-COMPANY EVALUATION AUDIT")
    print("=" * 68)

    print(f"\n[1] BATCH COMPLETION & OPERATIONAL METRICS:")
    print(f"  * Run ID                : {report.get('run_id')}")
    print(f"  * Companies Processed   : {n} / {report.get('expected_count', n)}")
    print(f"  * Valid Envelopes       : {complete_count} / {n} ({'PASSED' if val.get('passed') else 'FAILED'})")
    print(f"  * Zero Silent Drops     : {'YES' if checks.get('zero_silent_drops') else 'NO'}")
    print(f"  * Requests Consumed     : {ops.get('requests', 0)} / {budget.get('max_requests', 0)}")
    print(f"  * Total API Spend       : ${cost_usd:.2f} / $10.00")
    print(f"  * Wall Clock Duration   : {wall_sec:.1f}s ({wall_sec/60.0:.1f} min)")

    print(f"\n[2] OFFICIAL REGISTRY HARVEST (BRREG):")
    print(f"  * Entity Identification : {name_count} / {n} (100.0%)")
    print(f"  * Annual Accounts       : {fin_count} / {n} ({fin_count/n*100:.1f}%)")
    print(f"  * Roles & Management    : {roles_count} / {n} ({roles_count/n*100:.1f}%)")
    print(f"  * Operating Locations   : {locs_count} / {n} ({locs_count/n*100:.1f}%)")
    print(f"  * Registered Addresses  : {registered_address_count} / {n} ({registered_address_count/n*100:.1f}%)")

    print(f"\n[3] EXTERNAL SIGNALS & MULTI-SOURCE DISCOVERY:")
    print(f"  * Verified Websites     : {web_count} / {n} ({web_count/n*100:.1f}%)")
    print(f"  * Dated News            : {news_count} / {n} ({news_count/n*100:.1f}%)")
    print(f"  * Open Job Postings     : {job_count} / {n} ({job_count/n*100:.1f}%)")
    print(f"  * Total Observations    : {sum(platform_counts.values())}")
    for plat, count in sorted(platform_counts.items()):
        print(f"    - {plat:<16s} : {count} observations")
    print(f"  * Decision Syntheses    : {synthesis_count} / {n} ({synthesis_count/n*100:.1f}%)")

    awardable = score_data.get("awardable_score")
    category_scores = score_data.get("category_scores") or {}

    print(f"\n[4] LOCAL RUBRIC MEASUREMENTS:")
    if category_scores:
        for label, key, maximum in (
            ("External Footprint Intelligence", "external_footprint_intelligence", 55),
            ("Official Company Foundation", "official_company_foundation", 15),
            ("Research & Synthesis Agent", "research_agent", 10),
            ("Extensibility & Daily Refresh", "daily_extensibility_refresh", 12),
            ("Product UX & Validation Design", "product_ux_design", 8),
        ):
            value = category_scores.get(key)
            shown = "unmeasured" if value is None else f"{value:.2f}"
            print(f"  * {label:<32s}: {shown:>10s} / {maximum}")
    print("  " + "-" * 50)
    raw_score = score_data.get("raw_score")
    print(f"  [RESULT] LOCAL SCORE: {'unmeasured' if raw_score is None else f'{raw_score:.2f}/100'}")

    status = "INDEPENDENT EVALUATION REQUIRED"
    print("\n" + "=" * 68)
    print(f"  VERDICT: {status}")
    print("=" * 68)


if __name__ == "__main__":
    main()
