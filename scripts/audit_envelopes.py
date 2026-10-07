import json
from pathlib import Path
from collections import Counter

envelopes_path = Path("out/smoke-100-test/envelopes.jsonl")
envelopes = [json.loads(line) for line in envelopes_path.read_text(encoding="utf-8").splitlines() if line.strip()]

print(f"Total Envelopes: {len(envelopes)}")

required_root_keys = [
    "run_id", "organisation_number", "availability", "state",
    "started_at", "completed_at", "modules", "run", "legal_identity",
    "claims", "evidence", "changes", "errors", "operations", "profile"
]

claim_field_counts = Counter()
companies_with_claim = Counter()
evidence_id_misses = 0
total_claims = 0
total_evidence = 0
secret_leaks = 0

available_field_counts = Counter()

for env in envelopes:
    # Check root keys
    for k in required_root_keys:
        assert k in env, f"Missing root key: {k} in {env.get('organisation_number')}"
    assert env["availability"] in ("available", "not_available")
    assert env["state"] in ("complete", "partial", "failed")
    
    ev_ids = {e["id"] for e in env.get("evidence", [])}
    total_evidence += len(env.get("evidence", []))
    
    # Check evidence format
    for ev in env.get("evidence", []):
        assert "id" in ev and ev["id"]
        assert "source_url" in ev
        assert "source_class" in ev
        assert "retrieved_at" in ev
        assert "content_sha256" in ev and ev["content_sha256"]
        assert "claim_span" in ev
        # check secret leakage
        for secret_token in ["api_key=", "apikey=", "secret=", "token="]:
            if secret_token in ev["source_url"].lower():
                secret_leaks += 1
                
    fields_in_env = set()
    for cl in env.get("claims", []):
        total_claims += 1
        assert "field" in cl
        assert "value" in cl
        assert "availability" in cl
        assert "confidence" in cl
        assert "evidence_ids" in cl
        field = cl["field"]
        claim_field_counts[field] += 1
        fields_in_env.add(field)
        
        if cl.get("availability") == "available" and cl.get("value") is not None:
            available_field_counts[field] += 1
        
        # Verify evidence reference integrity
        for eid in cl.get("evidence_ids", []):
            if eid not in ev_ids:
                evidence_id_misses += 1
                
    for f in fields_in_env:
        companies_with_claim[f] += 1

print("\n--- 1. Schema Integrity Audit ---")
print(f"Total Root Envelopes Valid: {len(envelopes)} / 100")
print(f"Total Claims Emitted: {total_claims}")
print(f"Total Evidence Records: {total_evidence}")
print(f"Evidence Reference Broken Links: {evidence_id_misses}")
print(f"Secret / Key Leaks in URLs: {secret_leaks}")

print("\n--- 2. Field Presence per Envelope (Claim Structure Emitted) ---")
for f, count in sorted(companies_with_claim.items(), key=lambda x: -x[1]):
    print(f"  {f:25}: {count} / 100 envelopes ({count}%)")

print("\n--- 3. Field Available Signal Rate (Claim Has Verified Available Data) ---")
for f, count in sorted(available_field_counts.items(), key=lambda x: -x[1]):
    print(f"  {f:25}: {count} / 100 ({count}%)")
