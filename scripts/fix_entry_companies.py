#!/usr/bin/env python3
"""Ensure all 1000 organisations in entry-companies.jsonl exist in brreg-enheter.csv."""

import json
from pathlib import Path

replacements = [
    {"organisation_number": "810034882", "name": "SANDNES ELEKTRISKE AS", "legal_form": "AS", "employees": 11, "bankrupt": False, "liquidating": False, "municipality": "SANDNES", "municipality_number": "1108", "industry_code": "43.210", "industry_label": "Elektrisk installasjonsarbeid", "website": "", "latest_submitted_accounts": "2025"},
    {"organisation_number": "810059672", "name": "AASEN & FARSTAD AS", "legal_form": "AS", "employees": None, "bankrupt": False, "liquidating": False, "municipality": "MOLDE", "municipality_number": "1506", "industry_code": "68.200", "industry_label": "Utleie av egen eller leid fast eiendom", "website": "", "latest_submitted_accounts": "2025"},
]

missing_to_remove = {"928987728", "912695875"}
manifest_file = Path("entry-companies.jsonl")
manifest_lines = [line for line in manifest_file.read_text(encoding="utf-8").splitlines() if line.strip()]

new_rows = []
for line in manifest_lines:
    row = json.loads(line)
    if row["organisation_number"] not in missing_to_remove:
        new_rows.append(row)

new_rows.extend(replacements)
print(f"Total rows in new manifest: {len(new_rows)}")
manifest_file.write_text("".join(json.dumps(r, separators=(",", ":")) + "\n" for r in new_rows), encoding="utf-8")
print("Successfully updated entry-companies.jsonl")
