#!/usr/bin/env python3
"""Adversarial Empirical Stress Harness for Referential Integrity, Manifest, and Secret Leakage.

Written by Challenger 2 (Empirical Challenger).
"""
import json
import os
import re
import sys
import hashlib
from pathlib import Path
from collections import Counter

ROOT = Path(__file__).resolve().parents[1]

# Strict Regexes
SHA256_LOWERCASE_HEX_RE = re.compile(r"^[0-9a-f]{64}$")

# Secret scanning regexes
SECRET_PATTERNS = [
    ("Google API Key", re.compile(r"AIza[0-9A-Za-z\-_]{35}")),
    ("Bearer Token", re.compile(r"[bB]earer\s+[A-Za-z0-9_\-\.]{16,}")),
    ("Generic API Key Query Param", re.compile(r"(?:api[_-]?key|apikey|secret|token|auth)[=:\"'\s]+([A-Za-z0-9_\-]{16,})", re.IGNORECASE)),
    ("GitHub Token", re.compile(r"gh[pousr]_[A-Za-z0-9]{36,}")),
    ("OpenAI API Key", re.compile(r"sk-[A-Za-z0-9]{20,}")),
    ("AWS Key", re.compile(r"AKIA[0-9A-Z]{16}")),
]

# Sensitive query params in URLs specifically
URL_SECRET_KEYWORDS = ["api_key=", "apikey=", "secret=", "token=", "access_token=", "key=aiza"]

REQUIRED_ROOT_KEYS = [
    "run_id", "organisation_number", "availability", "state",
    "started_at", "completed_at", "modules", "run", "legal_identity",
    "claims", "evidence", "changes", "errors", "operations", "profile"
]

REQUIRED_CLAIM_FIELDS = [
    "legal_identity", "official_website", "website", "social_profiles",
    "social_profile", "annual_accounts", "roles", "operating_locations",
    "hiring_status", "hiring_signal", "accounting_obligation",
    "dated_news", "news", "job_postings", "jobs"
]

def stress_test_envelopes(file_path: Path, max_check: int = 15000) -> dict:
    print(f"\n{'='*80}\nSTRESS-TESTING ENVELOPES: {file_path}\n{'='*80}")
    if not file_path.exists():
        return {"error": f"File not found: {file_path}"}

    total_lines = 0
    orgs_seen = set()
    duplicate_orgs = 0
    missing_root_keys_count = 0
    total_claims = 0
    total_evidence = 0
    total_claim_evidence_links = 0
    broken_claim_evidence_links = 0
    non_sha256_hashes = 0
    uppercase_sha256_hashes = 0
    empty_hashes = 0
    dummy_all_zero_hashes = 0
    missing_evidence_ids = 0
    synth_broken_evidence_links = 0
    url_secret_leaks = 0
    claim_fields_missing_count = Counter()
    unresolved_eids_sample = []

    with open(file_path, "r", encoding="utf-8") as f:
        for line_num, line in enumerate(f, 1):
            if not line.strip():
                continue
            total_lines += 1
            if total_lines > max_check:
                break
            
            try:
                env = json.loads(line)
            except Exception as e:
                print(f"Line {line_num}: JSON decode failure: {e}")
                continue

            org = env.get("organisation_number")
            if org in orgs_seen:
                duplicate_orgs += 1
            orgs_seen.add(org)

            # Check root keys
            missing_roots = [k for k in REQUIRED_ROOT_KEYS if k not in env]
            if missing_roots:
                missing_root_keys_count += 1

            # Evidence index
            evidence_items = env.get("evidence", [])
            total_evidence += len(evidence_items)
            ev_map = {}
            for ev in evidence_items:
                eid = ev.get("id")
                if not eid:
                    missing_evidence_ids += 1
                    continue
                ev_map[eid] = ev

                sha = str(ev.get("content_sha256") or "")
                if not sha:
                    empty_hashes += 1
                elif sha == "0" * 64:
                    dummy_all_zero_hashes += 1
                elif not SHA256_LOWERCASE_HEX_RE.match(sha):
                    non_sha256_hashes += 1
                    if len(sha) == 64 and sha.lower() == sha:
                        pass
                    elif any(c.isupper() for c in sha):
                        uppercase_sha256_hashes += 1

                # URL secret scan
                src_url = str(ev.get("source_url") or "").lower()
                for kw in URL_SECRET_KEYWORDS:
                    if kw in src_url:
                        url_secret_leaks += 1

            # Claims verification
            claims = env.get("claims", [])
            total_claims += len(claims)
            emitted_fields = set()
            for claim in claims:
                f_name = claim.get("field")
                emitted_fields.add(f_name)
                eids = claim.get("evidence_ids", [])
                total_claim_evidence_links += len(eids)
                for eid in eids:
                    if eid not in ev_map:
                        broken_claim_evidence_links += 1
                        if len(unresolved_eids_sample) < 5:
                            unresolved_eids_sample.append((org, f_name, eid))

            for req_field in REQUIRED_CLAIM_FIELDS:
                if req_field not in emitted_fields:
                    claim_fields_missing_count[req_field] += 1

            # Synthesis evidence verification
            synth = env.get("synthesis")
            if isinstance(synth, dict):
                synth_eids = synth.get("evidence_ids", [])
                for se in synth_eids:
                    if se not in ev_map:
                        synth_broken_evidence_links += 1

    results = {
        "file": str(file_path),
        "total_envelopes": total_lines,
        "unique_orgs": len(orgs_seen),
        "duplicate_orgs": duplicate_orgs,
        "missing_root_keys_count": missing_root_keys_count,
        "total_evidence_items": total_evidence,
        "empty_hashes": empty_hashes,
        "dummy_all_zero_hashes": dummy_all_zero_hashes,
        "non_sha256_hashes": non_sha256_hashes,
        "uppercase_sha256_hashes": uppercase_sha256_hashes,
        "url_secret_leaks": url_secret_leaks,
        "total_claims": total_claims,
        "total_claim_evidence_links": total_claim_evidence_links,
        "broken_claim_evidence_links": broken_claim_evidence_links,
        "synth_broken_evidence_links": synth_broken_evidence_links,
        "unresolved_eids_sample": unresolved_eids_sample,
        "missing_required_fields_total": sum(claim_fields_missing_count.values()),
    }
    
    print(f"Results for {file_path.name}:")
    for k, v in results.items():
        print(f"  {k}: {v}")
    return results

def deep_secret_scan(paths: list[Path]) -> dict:
    print(f"\n{'='*80}\nDEEP SECRET & TOKEN LEAKAGE SCAN\n{'='*80}")
    matches_found = []
    scanned_files = 0
    scanned_bytes = 0

    for base_path in paths:
        if base_path.is_file():
            files_to_scan = [base_path]
        else:
            files_to_scan = [p for p in base_path.rglob("*") if p.is_file() and not any(part in {".git", ".venv", "__pycache__"} for part in p.parts)]

        for file_p in files_to_scan:
            # Skip binary or very large files if needed, or scan all text
            if file_p.suffix in {".pyc", ".png", ".jpg", ".gz", ".zip", ".tar", ".parquet"}:
                continue
            scanned_files += 1
            try:
                content = file_p.read_text(encoding="utf-8", errors="ignore")
                scanned_bytes += len(content)
                for pattern_name, regex in SECRET_PATTERNS:
                    for match in regex.finditer(content):
                        snippet = match.group(0)
                        # Filter out false positives (e.g. test dummies, env var names, schema defs)
                        if any(fp in snippet.lower() for fp in ["your_api_key", "mock", "dummy", "placeholder", "xxx", "00000000", "none"]):
                            continue
                        if "api_key" in file_p.name.lower() or "secret" in file_p.name.lower():
                            # might be code checking for secret
                            pass
                        line_no = content[:match.start()].count("\n") + 1
                        matches_found.append({
                            "file": str(file_p),
                            "line": line_no,
                            "type": pattern_name,
                            "match": snippet[:10] + "..." + snippet[-4:] if len(snippet) > 14 else snippet
                        })
            except Exception as e:
                print(f"Error reading {file_p}: {e}")

    print(f"Scanned {scanned_files} files ({scanned_bytes / (1024*1024):.2f} MB).")
    print(f"Leaks found: {len(matches_found)}")
    if matches_found:
        for m in matches_found[:10]:
            print(f"  ! LEAK DETECTED: {m}")
    return {
        "scanned_files": scanned_files,
        "scanned_mb": scanned_bytes / (1024*1024),
        "leaks_count": len(matches_found),
        "leaks": matches_found,
    }

def main():
    # 1. Stress test envelopes
    fresh_res = stress_test_envelopes(ROOT / "out" / "fresh-1000-live" / "envelopes.jsonl")
    eval_res = stress_test_envelopes(ROOT / "out" / "eval-10000-test" / "envelopes.jsonl")

    # 2. Deep secret scan
    scan_targets = [
        ROOT / "out" / "fresh-1000-live",
        ROOT / "out" / "eval-10000-test",
        ROOT / "snapshots",
    ]
    secret_res = deep_secret_scan(scan_targets)

    # 3. Final Summary Assertion
    print(f"\n{'='*80}\nFINAL VERIFICATION SUMMARY\n{'='*80}")
    checks = [
        ("Fresh 1,000 envelopes valid count", fresh_res.get("total_envelopes") == 1000),
        ("Fresh 1,000 unique orgs (0 duplicates)", fresh_res.get("duplicate_orgs") == 0),
        ("Fresh 1,000 root keys complete", fresh_res.get("missing_root_keys_count") == 0),
        ("Fresh 1,000 claims broken links == 0", fresh_res.get("broken_claim_evidence_links") == 0),
        ("Fresh 1,000 synth broken links == 0", fresh_res.get("synth_broken_evidence_links") == 0),
        ("Fresh 1,000 sha256 non-hex == 0", fresh_res.get("non_sha256_hashes") == 0),
        ("Fresh 1,000 empty hashes == 0", fresh_res.get("empty_hashes") == 0),
        ("Fresh 1,000 uppercase hashes == 0", fresh_res.get("uppercase_sha256_hashes") == 0),
        ("Fresh 1,000 URL secret leaks == 0", fresh_res.get("url_secret_leaks") == 0),

        ("Eval 10,000 envelopes valid count", eval_res.get("total_envelopes") == 10000),
        ("Eval 10,000 unique orgs (0 duplicates)", eval_res.get("duplicate_orgs") == 0),
        ("Eval 10,000 root keys complete", eval_res.get("missing_root_keys_count") == 0),
        ("Eval 10,000 claims broken links == 0", eval_res.get("broken_claim_evidence_links") == 0),
        ("Eval 10,000 synth broken links == 0", eval_res.get("synth_broken_evidence_links") == 0),
        ("Eval 10,000 sha256 non-hex == 0", eval_res.get("non_sha256_hashes") == 0),
        ("Eval 10,000 empty hashes == 0", eval_res.get("empty_hashes") == 0),
        ("Eval 10,000 uppercase hashes == 0", eval_res.get("uppercase_sha256_hashes") == 0),
        ("Eval 10,000 URL secret leaks == 0", eval_res.get("url_secret_leaks") == 0),

        ("Deep Secret Scan Leaks == 0", secret_res.get("leaks_count") == 0),
    ]

    all_passed = True
    for desc, passed in checks:
        status = "PASS" if passed else "FAIL"
        if not passed:
            all_passed = False
        print(f"[{status}] {desc}")

    if all_passed:
        print("\nOVERALL VERDICT: ALL CHECKS PASSED (VERIFIED)")
        return 0
    else:
        print("\nOVERALL VERDICT: FAILURE DETECTED")
        return 1

if __name__ == "__main__":
    sys.exit(main())
