#!/usr/bin/env python3
"""Measure one official specialist connector before any official-safe promotion.

This tool intentionally writes a candidate-observation file and a coverage
report. It does not modify profiles or make a connector publishable.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from norway_company_agent.connectors.doffin import fetch_doffin_procurement
from norway_company_agent.connectors.forskningsradet import fetch_forskningsradet_grants
from norway_company_agent.connectors.innovasjon_norge import fetch_innovasjon_norge_grants
from norway_company_agent.connectors.patentstyret import fetch_patentstyret_data
from norway_company_agent.connectors.mattilsynet import fetch_mattilsynet_inspections
from norway_company_agent.connectors.miljodirektoratet import fetch_miljodirektoratet_permits
from norway_company_agent.connectors.losoreregisteret import fetch_losoreregisteret_liens
from norway_company_agent.source_policy import OFFICIAL_SAFE, observation_decision

CONNECTORS = {
    "doffin": fetch_doffin_procurement,
    "forskningsradet": fetch_forskningsradet_grants,
    "innovasjon_norge": fetch_innovasjon_norge_grants,
    "patentstyret": fetch_patentstyret_data,
    "mattilsynet": fetch_mattilsynet_inspections,
    "miljodirektoratet": fetch_miljodirektoratet_permits,
    "losoreregisteret": fetch_losoreregisteret_liens,
}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profiles", required=True)
    parser.add_argument("--connector", required=True, choices=sorted(CONNECTORS))
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--limit", type=int, default=100)
    args = parser.parse_args()

    profiles = [json.loads(line) for line in Path(args.profiles).read_text(encoding="utf-8").splitlines() if line.strip()]
    selected = [p for p in profiles if str(p.get("organisation_number") or "").isdigit()][:args.limit]
    observations: list[dict] = []
    failures = 0
    fetch = CONNECTORS[args.connector]
    for profile in selected:
        try:
            observations.extend(fetch(str(profile["organisation_number"]), str(profile.get("name") or "")))
        except Exception:
            failures += 1

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    observation_path = output_dir / f"{args.connector}-candidates.jsonl"
    observation_path.write_text("".join(json.dumps(item, ensure_ascii=False) + "\n" for item in observations), encoding="utf-8")
    companies_with_findings = {str(item.get("organisation_number")) for item in observations}
    policy = Counter(observation_decision(item, policy=OFFICIAL_SAFE).reason for item in observations)
    report = {
        "report_kind": "specialist_connector_trial_v1",
        "connector": args.connector,
        "companies_checked": len(selected),
        "companies_with_findings": len(companies_with_findings),
        "company_coverage": len(companies_with_findings) / len(selected) if selected else 0.0,
        "observations": len(observations),
        "worker_failures": failures,
        "official_safe_decisions": dict(policy),
        "candidate_observations": str(observation_path),
        "promotion": "not_promoted; run held-out evidence/right-to-use review before changing source-rights.json",
    }
    (output_dir / f"{args.connector}-trial-report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
