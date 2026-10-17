#!/usr/bin/env python3
"""Sample 1,000 strictly fresh, never-before-tested companies from the Builderr public universe."""
from __future__ import annotations

import gzip
import json
import random
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
UNIVERSE_GZ = ROOT / "signalpost-universe.jsonl.gz"
OUTPUT_FILE = ROOT / "fresh-1000-companies.jsonl"


def main() -> int:
    seen_orgs: set[str] = set()

    # Collect all organisation numbers from every existing benchmark/test fixture
    for p in ROOT.glob("*.jsonl"):
        for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
                org = str(row.get("organisation_number") or "").strip()
                if org and len(org) == 9 and org.isdigit():
                    seen_orgs.add(org)
            except Exception:
                pass

    for p in (ROOT / "tests" / "fixtures").glob("*"):
        if p.is_file():
            for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
                line = line.strip()
                if len(line) == 9 and line.isdigit():
                    seen_orgs.add(line)

    print(f"Total previously seen/tested orgs excluded: {len(seen_orgs)}")

    # Load fresh candidates from universe
    fresh: list[dict] = []
    with gzip.open(UNIVERSE_GZ, "rt", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            org = str(row.get("organisation_number") or "").strip()
            if org and len(org) == 9 and org.isdigit() and org not in seen_orgs:
                fresh.append(row)

    print(f"Total candidate fresh companies available: {len(fresh)}")

    # Stratified sampling: ensure diversity across legal forms and website presence
    rng = random.Random(20261017)
    rng.shuffle(fresh)

    selected: list[dict] = []
    by_form: dict[str, list[dict]] = {}
    for r in fresh:
        form = str(r.get("legal_form") or "AS").upper()
        by_form.setdefault(form, []).append(r)

    # Priority allocation: AS (650), ENK (150), BRL/BBL (80), ANS/DA (50), SA (40), other (30)
    quotas = {
        "AS": 650,
        "ENK": 150,
        "BRL": 60,
        "BBL": 20,
        "ANS": 25,
        "DA": 25,
        "SA": 40,
    }

    used_orgs: set[str] = set()
    for form, quota in quotas.items():
        pool = by_form.get(form, [])
        for item in pool[:quota]:
            selected.append({
                "organisation_number": item["organisation_number"],
                "name": item.get("name"),
                "legal_form": item.get("legal_form"),
                "sample_slice": f"fresh_live_{form.lower()}",
                "evaluation_split": "test",
            })
            used_orgs.add(item["organisation_number"])

    # Fill remainder up to 1,000 from general fresh pool
    for item in fresh:
        if len(selected) >= 1000:
            break
        if item["organisation_number"] not in used_orgs:
            selected.append({
                "organisation_number": item["organisation_number"],
                "name": item.get("name"),
                "legal_form": item.get("legal_form"),
                "sample_slice": "fresh_live_unseen",
                "evaluation_split": "test",
            })
            used_orgs.add(item["organisation_number"])

    print(f"Writing {len(selected)} completely fresh companies to {OUTPUT_FILE}...")
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        for r in selected[:1000]:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    print(f"Successfully generated {OUTPUT_FILE} with exactly {len(selected[:1000])} unseen companies.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
