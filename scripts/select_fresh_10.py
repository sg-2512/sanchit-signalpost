import csv
import gzip
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

seen_orgs = set()
for path in [
    ROOT / "smoke-companies.jsonl",
    ROOT / "dev-100-companies.jsonl",
    ROOT / "eval" / "gold_companies.jsonl",
    ROOT / "entry-companies.jsonl"
]:
    if path.exists():
        with path.open(encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    try:
                        seen_orgs.add(str(json.loads(line).get("organisasjonsnummer", "")).strip())
                    except Exception:
                        pass

print(f"Total seen orgs in existing manifests: {len(seen_orgs)}")

csv_path = ROOT / "brreg-enheter.csv"
new_orgs = []
with gzip.open(csv_path, mode="rt", encoding="utf-8", errors="replace") as f_in:
    reader = csv.DictReader(f_in, delimiter=",")
    for row in reader:
        org = row.get("organisasjonsnummer", "").strip()
        name = row.get("navn", "").strip()
        form = row.get("organisasjonsform.kode", "").strip()
        konkurs = str(row.get("konkurs", "false")).lower()
        ansatte = row.get("antallAnsatte", "").strip()
        
        # Select active AS or ASA companies with registered employees not seen before
        if org and org not in seen_orgs and form in ("AS", "ASA") and konkurs == "false":
            try:
                emp_count = int(ansatte) if ansatte else 0
            except ValueError:
                emp_count = 0
            if emp_count >= 15:
                new_orgs.append({
                    "organisasjonsnummer": org,
                    "navn": name,
                    "form": form,
                    "employees": emp_count,
                    "municipality": row.get("forretningsadresse.kommune", "")
                })
                if len(new_orgs) == 10:
                    break

out_path = ROOT / "out" / "test-10-new.jsonl"
out_path.parent.mkdir(parents=True, exist_ok=True)
with out_path.open("w", encoding="utf-8") as f:
    for o in new_orgs:
        print(f" * {o['organisasjonsnummer']} - {o['navn']} ({o['form']}, {o['employees']} employees, {o['municipality']})")
        f.write(json.dumps({"organisasjonsnummer": o["organisasjonsnummer"], "navn": o["navn"]}) + "\n")

print(f"\nWrote 10 completely unseen companies to {out_path}")
