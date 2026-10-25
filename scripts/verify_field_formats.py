#!/usr/bin/env python3
"""Comprehensive Schema, Field Format, and Provenance Auditor for SignalPost Envelopes.

Validates that:
1. Every envelope conforms to the exact root schema.
2. All 15 required competition fields are emitted with standard value formats.
3. Values match their strict typing when available, and are None when not available.
4. Evidence items adhere to cryptographic provenance requirements (SHA-256, source class).
5. 100% referential integrity between claims and evidence with zero broken references.
6. Zero secret tokens or API keys are leaked into source URLs or claim spans.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]

REQUIRED_ROOT_KEYS = {
    "run_id", "organisation_number", "availability", "state",
    "started_at", "completed_at", "modules", "run", "legal_identity",
    "claims", "evidence", "changes", "errors", "operations", "profile"
}

ALLOWED_AVAILABILITY = {"available", "not_available", "blocked", "not_applicable", "ambiguous", "failed"}
ALLOWED_ENTITY_STATES = {"complete", "partial", "failed", "submission_error"}

REQUIRED_FIELDS = {
    "legal_identity",
    "official_website",
    "website",
    "social_profiles",
    "social_profile",
    "annual_accounts",
    "roles",
    "operating_locations",
    "hiring_status",
    "hiring_signal",
    "accounting_obligation",
    "dated_news",
    "news",
    "job_postings",
    "jobs",
}

HEX_64_PATTERN = re.compile(r"^[a-fA-F0-9]{64}$")
SECRET_PATTERNS = ["api_key=", "apikey=", "secret=", "token=", "bearer "]


def validate_iso_timestamp(ts: Any) -> bool:
    if not isinstance(ts, str):
        return False
    try:
        datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return True
    except Exception:
        return False


def validate_claim_value_format(field: str, val: Any) -> tuple[bool, str]:
    """Validate that when a claim is 'available', its value follows the standard format."""
    if val is None:
        return False, "Value cannot be None when availability is 'available'"

    if field == "legal_identity":
        if not isinstance(val, dict):
            return False, f"Expected dict, got {type(val).__name__}"
        if not val.get("organisation_number"):
            return False, "Missing organisation_number in legal_identity"
        if not val.get("name"):
            return False, "Missing name in legal_identity"

    elif field in {"official_website", "website"}:
        if not isinstance(val, str) or not (val.startswith("http://") or val.startswith("https://")):
            return False, f"Expected HTTP(S) URL string, got: {val}"

    elif field == "social_profiles":
        if not isinstance(val, list):
            return False, f"Expected list of social link dicts, got {type(val).__name__}"
        for item in val:
            if not isinstance(item, dict) or not item.get("url"):
                return False, f"Invalid social profile item: {item}"

    elif field == "social_profile":
        if not isinstance(val, str) or not (val.startswith("http://") or val.startswith("https://")):
            return False, f"Expected HTTP(S) social URL string, got: {val}"

    elif field == "annual_accounts":
        if not isinstance(val, dict):
            return False, f"Expected dict for annual_accounts, got {type(val).__name__}"

    elif field in {"roles", "operating_locations"}:
        if not isinstance(val, list):
            return False, f"Expected list for {field}, got {type(val).__name__}"

    elif field == "hiring_status":
        if not isinstance(val, dict):
            return False, f"Expected dict for hiring_status, got {type(val).__name__}"
        if "appears_to_be_hiring" not in val:
            return False, "Missing 'appears_to_be_hiring' in hiring_status"

    elif field == "hiring_signal":
        if not isinstance(val, str) or len(val.strip()) == 0:
            return False, f"Expected non-empty string for hiring_signal, got: {val}"

    elif field == "accounting_obligation":
        if not isinstance(val, str) or len(val.strip()) == 0:
            return False, f"Expected non-empty string for accounting_obligation, got: {val}"

    elif field in {"dated_news", "news"}:
        if not isinstance(val, dict):
            return False, f"Expected dict for {field}, got {type(val).__name__}"
        if not val.get("title"):
            return False, f"Missing title in {field}"

    elif field in {"job_postings", "jobs"}:
        if not isinstance(val, dict):
            return False, f"Expected dict for {field}, got {type(val).__name__}"
        if not val.get("title"):
            return False, f"Missing title in {field}"

    return True, ""


def audit_envelope_file(file_path: Path) -> dict[str, Any]:
    with file_path.open("r", encoding="utf-8") as f:
        lines = [l.strip() for l in f if l.strip()]
    total_envelopes = len(lines)
    
    errors: list[str] = []
    field_counts = Counter()
    field_available_counts = Counter()
    total_claims = 0
    total_evidence = 0
    broken_evidence_links = 0
    secret_leaks = 0

    for idx, line in enumerate(lines, 1):
        try:
            env = json.loads(line)
        except Exception as e:
            errors.append(f"Line {idx}: JSON parse failure: {e}")
            continue

        org = env.get("organisation_number", f"unknown_row_{idx}")

        # 1. Root Keys Check
        missing_root = REQUIRED_ROOT_KEYS - set(env.keys())
        if missing_root:
            errors.append(f"Org {org}: Missing root keys: {missing_root}")

        # 2. Root Value Constraints
        if env.get("availability") not in ALLOWED_AVAILABILITY:
            errors.append(f"Org {org}: Invalid root availability: {env.get('availability')}")
        if env.get("state") not in ALLOWED_ENTITY_STATES:
            errors.append(f"Org {org}: Invalid root state: {env.get('state')}")

        if not validate_iso_timestamp(env.get("started_at")):
            errors.append(f"Org {org}: Invalid started_at timestamp: {env.get('started_at')}")
        if not validate_iso_timestamp(env.get("completed_at")):
            errors.append(f"Org {org}: Invalid completed_at timestamp: {env.get('completed_at')}")

        # 3. Evidence Audit
        ev_items = env.get("evidence", [])
        total_evidence += len(ev_items)
        ev_map = {}
        for ev in ev_items:
            ev_id = ev.get("id")
            if not ev_id:
                errors.append(f"Org {org}: Evidence missing ID")
                continue
            ev_map[ev_id] = ev

            # Source URL & Span Secret Check
            s_url = str(ev.get("source_url") or "")
            c_span = str(ev.get("claim_span") or "")
            for sec in SECRET_PATTERNS:
                if sec in s_url.lower() or sec in c_span.lower():
                    secret_leaks += 1
                    errors.append(f"Org {org}: Leaked secret token '{sec}' in evidence {ev_id}")

            # Content SHA-256 Check
            sha = ev.get("content_sha256")
            if not sha or not HEX_64_PATTERN.match(str(sha)):
                errors.append(f"Org {org}: Invalid/missing SHA-256 '{sha}' in evidence {ev_id}")

            # Timestamp check
            if not validate_iso_timestamp(ev.get("retrieved_at")):
                errors.append(f"Org {org}: Invalid retrieved_at in evidence {ev_id}")

        # 4. Claims Audit
        claims = env.get("claims", [])
        total_claims += len(claims)
        found_fields = set()

        for cl in claims:
            field = cl.get("field")
            if not field:
                errors.append(f"Org {org}: Claim missing 'field' key")
                continue
            found_fields.add(field)
            field_counts[field] += 1

            avail = cl.get("availability")
            if avail not in ALLOWED_AVAILABILITY:
                errors.append(f"Org {org}: Field '{field}' has invalid availability '{avail}'")

            conf = cl.get("confidence")
            if not isinstance(conf, (int, float)) or not (0.0 <= conf <= 1.0):
                errors.append(f"Org {org}: Field '{field}' has invalid confidence '{conf}'")

            # Check value formatting
            val = cl.get("value")
            if avail == "available":
                field_available_counts[field] += 1
                valid_fmt, msg = validate_claim_value_format(field, val)
                if not valid_fmt:
                    errors.append(f"Org {org}: Field '{field}' invalid available format: {msg}")
            else:
                if val is not None:
                    errors.append(f"Org {org}: Field '{field}' is '{avail}' but value is not None: {val}")

            # Referential integrity check
            eids = cl.get("evidence_ids", [])
            if not isinstance(eids, list):
                errors.append(f"Org {org}: Field '{field}' evidence_ids is not a list")
            else:
                for eid in eids:
                    if eid not in ev_map:
                        broken_evidence_links += 1
                        errors.append(f"Org {org}: Field '{field}' references non-existent evidence ID '{eid}'")

                if avail == "available" and not eids:
                    errors.append(f"Org {org}: Field '{field}' is available but has 0 evidence_ids")

        # 5. Field Presence Check: Did envelope emit all 15 required fields?
        missing_fields = REQUIRED_FIELDS - found_fields
        if missing_fields:
            errors.append(f"Org {org}: Missing required claim fields: {missing_fields}")

        # 6. Synthesis & Summary Audit (12-point rubric verification)
        synth = env.get("synthesis")
        if not isinstance(synth, dict):
            errors.append(f"Org {org}: Missing or non-dict top-level 'synthesis'")
        else:
            for req_s_key in ("summary", "what_it_does", "what_changed", "what_remains_unknown", "unknowns", "changes", "sources", "evidence_ids"):
                if not synth.get(req_s_key):
                    errors.append(f"Org {org}: Synthesis missing or empty required field '{req_s_key}'")
            for s_eid in synth.get("evidence_ids") or []:
                if s_eid not in ev_map:
                    broken_evidence_links += 1
                    errors.append(f"Org {org}: Synthesis references non-existent evidence ID '{s_eid}'")
        if not isinstance(env.get("summary"), str) or not env.get("summary").strip():
            errors.append(f"Org {org}: Missing or empty top-level 'summary' string")

    passed = len(errors) == 0
    return {
        "file": str(file_path),
        "total_envelopes": total_envelopes,
        "passed": passed,
        "error_count": len(errors),
        "errors_sample": errors[:15],
        "total_claims": total_claims,
        "total_evidence": total_evidence,
        "broken_evidence_links": broken_evidence_links,
        "secret_leaks": secret_leaks,
        "field_presence": {f: field_counts[f] for f in sorted(REQUIRED_FIELDS)},
        "field_available": {f: field_available_counts[f] for f in sorted(REQUIRED_FIELDS)},
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Auditor for SignalPost Envelopes")
    parser.add_argument("envelopes", nargs="?", default="out/smoke-200-test/envelopes.jsonl")
    args = parser.parse_args()

    p = Path(args.envelopes)
    if not p.exists():
        print(f"[ERROR] Target file does not exist: {p}")
        return 1

    print(f"Auditing file: {p}...")
    report = audit_envelope_file(p)

    print("\n" + "=" * 70)
    print("SIGNALPOST ENVELOPE STANDARD FORMAT & SCHEMA AUDIT REPORT")
    print("=" * 70)
    print(f"Target Envelopes File: {report['file']}")
    print(f"Total Envelopes Audited: {report['total_envelopes']}")
    print(f"Schema Compliance Verdict: {'100% PASSED' if report['passed'] else 'FAILED'}")
    print(f"Total Claims Audited: {report['total_claims']}")
    print(f"Total Evidence Items: {report['total_evidence']}")
    print(f"Broken Evidence Links: {report['broken_evidence_links']}")
    print(f"Secret / Key Leaks Detected: {report['secret_leaks']}")
    print(f"Total Format Errors Detected: {report['error_count']}")
    
    print("\n[FIELD PRESENCE RATE (15 REQUIRED FIELDS)]")
    for f, count in report['field_presence'].items():
        pct = (count / max(report['total_envelopes'], 1)) * 100
        print(f"  {f:25}: {count}/{report['total_envelopes']} envelopes ({pct:.1f}%)")

    print("\n[FIELD VERIFIED 'AVAILABLE' SIGNAL RATE]")
    for f, count in report['field_available'].items():
        pct = (count / max(report['total_envelopes'], 1)) * 100
        print(f"  {f:25}: {count}/{report['total_envelopes']} envelopes ({pct:.1f}%)")

    if report["errors_sample"]:
        print("\n[SAMPLE ERRORS DETECTED]")
        for err in report["errors_sample"]:
            print(f"  ! {err}")

    print("=" * 70)
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
