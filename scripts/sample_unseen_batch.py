#!/usr/bin/env python3
"""Sample a fresh, unseen batch of Norwegian companies for agent evaluation.

Ensures no overlap with previous test runs (e.g. dev-100-companies.jsonl).
Filters for active Norwegian companies (AS) from the official Brønnøysund bulk snapshot.
"""
from __future__ import annotations

import argparse
import json
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from norway_company_agent.sampling import iter_bulk


def main() -> None:
    parser = argparse.ArgumentParser(description="Sample unseen Norwegian companies from bulk registry.")
    parser.add_argument("--bulk", default="brreg-enheter.csv", help="Bulk registry CSV or JSONL")
    parser.add_argument("--exclude", default="dev-100-companies.jsonl", help="Previous JSONL to exclude")
    parser.add_argument("--count", type=int, default=100, help="Number of companies to sample (default 100)")
    parser.add_argument("--output", default="new-100-companies.jsonl", help="Output JSONL manifest")
    parser.add_argument("--seed", type=int, default=20261001, help="Random seed for reproducibility")
    args = parser.parse_args()

    # Load excluded organisation numbers
    excluded_orgs: set[str] = set()
    exclude_path = Path(args.exclude)
    if exclude_path.exists():
        with exclude_path.open("r", encoding="utf-8-sig") as f:
            for line in f:
                if line.strip():
                    try:
                        row = json.loads(line)
                        org = str(row.get("organisation_number") or "").strip()
                        if org:
                            excluded_orgs.add(org)
                    except Exception:
                        pass
        print(f"[INFO] Excluding {len(excluded_orgs)} organisation numbers from {args.exclude}")

    # Reservoir sampling of active AS entities
    rng = random.Random(args.seed)
    candidates: list[dict] = []
    pool_limit = max(args.count * 10, 2000)

    print(f"[INFO] Scanning bulk file '{args.bulk}' for active companies...")
    for row in iter_bulk(args.bulk):
        org = str(row.get("organisation_number") or "").strip()
        if not org or len(org) != 9 or org in excluded_orgs:
            continue

        legal_form = row.get("legal_form") or ""
        bankrupt = bool(row.get("bankrupt"))
        liquidating = bool(row.get("liquidating"))

        # Select active AS companies
        if legal_form == "AS" and not bankrupt and not liquidating:
            entry = {
                "organisation_number": org,
                "name": row.get("name") or "",
                "legal_form": legal_form,
                "employees": row.get("employees"),
                "bankrupt": False,
                "liquidating": False,
                "municipality": row.get("municipality") or "",
                "municipality_number": row.get("municipality_number") or "",
                "industry_code": row.get("industry_code") or "",
                "industry_label": row.get("industry_label") or "",
                "website": row.get("website") or "",
                "latest_submitted_accounts": row.get("latest_submitted_accounts") or "2025",
            }
            candidates.append(entry)
            if len(candidates) >= pool_limit:
                break

    if len(candidates) < args.count:
        raise SystemExit(f"[ERROR] Only found {len(candidates)} candidate companies; needed {args.count}")

    # Randomly select exact count with seed
    selected = rng.sample(candidates, args.count)

    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as f:
        for item in selected:
            f.write(json.dumps(item, ensure_ascii=False) + "\n")

    print(f"✅ Successfully wrote {len(selected)} unseen companies to '{args.output}' (seed={args.seed})")


if __name__ == "__main__":
    main()
