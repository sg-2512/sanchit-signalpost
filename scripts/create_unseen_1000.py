import csv
import gzip
import json
from pathlib import Path

# 1. Collect all previously seen organisation numbers
seen_orgs = set()
for p in Path(".").glob("*.jsonl"):
    if p.name.startswith("unseen-1000"):
        continue
    try:
        for line in p.read_text(encoding="utf-8").splitlines():
            if line.strip():
                try:
                    row = json.loads(line)
                    org = str(row.get("organisation_number") or "")
                    if org:
                        seen_orgs.add(org)
                except Exception:
                    pass
    except Exception:
        pass

for p in Path("out").glob("**/*.jsonl"):
    try:
        for line in p.read_text(encoding="utf-8").splitlines():
            if line.strip():
                try:
                    row = json.loads(line)
                    org = str(row.get("organisation_number") or "")
                    if org:
                        seen_orgs.add(org)
                except Exception:
                    pass
    except Exception:
        pass

print(f"Total previously evaluated/seen orgs: {len(seen_orgs)}")

# 2. Stream brreg-enheter.csv (gzip compressed, comma-delimited) to find 1000 active, never-evaluated companies
unseen = []
with gzip.open("brreg-enheter.csv", mode="rt", encoding="utf-8", errors="replace") as f:
    reader = csv.DictReader(f, delimiter=",")
    for row in reader:
        org = str(row.get("organisasjonsnummer") or "").strip()
        name = str(row.get("navn") or "").strip()
        form = str(row.get("organisasjonsform.kode") or "").strip()
        konkurs = str(row.get("konkurs") or "false").strip().lower()
        avvikling = str(row.get("underAvvikling") or "false").strip().lower()
        tvang = str(row.get("underTvangsavviklingEllerTvangsopplosning") or "false").strip().lower()

        if not org or org in seen_orgs:
            continue
        if konkurs == "true" or avvikling == "true" or tvang == "true":
            continue
        if form not in {"AS", "ASA", "ENK", "BA", "SA"}:
            continue
        if not name or len(name) < 3:
            continue

        emp_raw = row.get("antallAnsatte") or "0"
        try:
            emp = int(emp_raw)
        except ValueError:
            emp = 0

        muni = str(row.get("forretningsadresse.kommune") or row.get("postadresse.kommune") or "").strip()

        unseen.append({
            "organisation_number": org,
            "name": name,
            "legal_form": form,
            "employees": emp,
            "municipality": muni,
        })
        if len(unseen) >= 1000:
            break

assert len(unseen) == 1000, f"Expected 1000 unseen companies, found {len(unseen)}"
assert all(c["organisation_number"] not in seen_orgs for c in unseen), "Collision detected!"

out_path = Path("unseen-1000-companies.jsonl")
out_path.write_text("".join(json.dumps(c, ensure_ascii=False) + "\n" for c in unseen), encoding="utf-8")
print(f"Successfully generated {out_path} with exactly 1,000 NEVER-EVALUATED-BEFORE companies!")
