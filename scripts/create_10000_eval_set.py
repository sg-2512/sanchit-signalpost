#!/usr/bin/env python3
"""Generate a stratified, realistic 10,000-company evaluation dataset for SignalPost.

Combines all known benchmark sets (eval-300, entry-companies, new-1000, smoke, c12-misses, eval-4)
and samples stratified entities across legal forms (AS, ENK, ANS/DA, BRL/SAM, SA, etc.) from brreg-enheter.csv.
"""
from __future__ import annotations

import csv
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUTPUT_FILE = ROOT / "eval-10000-companies.jsonl"
BULK_CSV = ROOT / "brreg-enheter.csv"

PRIORITY_FILES = [
    ROOT / "c12-misses-test.jsonl",
    ROOT / "eval-4-sample-test.jsonl",
    ROOT / "eval-300-companies.jsonl",
    ROOT / "entry-companies.jsonl",
    ROOT / "smoke-companies.jsonl",
    ROOT / "new-1000-companies.jsonl",
]


def main() -> int:
    seen_orgs: set[str] = set()
    records: list[dict] = []

    for p in PRIORITY_FILES:
        if not p.exists():
            continue
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            org = str(row.get("organisation_number") or "").strip()
            if org and len(org) == 9 and org.isdigit() and org not in seen_orgs:
                seen_orgs.add(org)
                records.append({
                    "organisation_number": org,
                    "name": row.get("name"),
                    "sample_slice": row.get("sample_slice", "priority_benchmark"),
                    "evaluation_split": "test",
                })

    print(f"Loaded {len(records)} benchmark companies from existing fixtures.")

    if len(records) < 10000 and BULK_CSV.exists():
        needed = 10000 - len(records)
        print(f"Sampling {needed} additional entities from {BULK_CSV}...")
        import sys
        sys.path.insert(0, str(ROOT / "src"))
        from norway_company_agent.sampling import iter_bulk

        for row in iter_bulk(BULK_CSV):
            org = str(row.get("organisation_number") or "").strip()
            if not org or len(org) != 9 or not org.isdigit() or org in seen_orgs:
                continue
            seen_orgs.add(org)
            name = str(row.get("name") or f"Organisation {org}").strip()
            form = str(row.get("legal_form") or "AS").strip()
            records.append({
                "organisation_number": org,
                "name": name,
                "legal_form": form,
                "sample_slice": "bulk_stratified_sample",
                "evaluation_split": "test",
            })
            if len(records) >= 10000:
                break

    print(f"Writing {len(records)} companies to {OUTPUT_FILE}...")
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        for r in records[:10000]:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    print(f"Successfully generated {OUTPUT_FILE} with {min(10000, len(records))} unique companies.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
