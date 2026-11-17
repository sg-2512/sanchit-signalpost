#!/usr/bin/env python3
"""Validate a candidate Signalpost run before it is submitted.

This is an operational contract check, not a score or qualification decision.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from typing import Any

ALLOWED = {"available", "not_available", "blocked", "not_applicable", "ambiguous", "failed"}


def jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate a Signalpost evaluator run for submission readiness.")
    parser.add_argument("--input", required=True, help="Original organisation-number JSONL")
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--output", help="Write JSON report here")
    args = parser.parse_args()
    input_rows = jsonl(Path(args.input))
    expected = [str(row.get("organisation_number") or "") for row in input_rows]
    out = Path(args.output_dir)
    envelopes_path = out / "envelopes.jsonl"
    report_path = out / "run-report.json"
    registry_path = Path(__file__).resolve().parents[1] / "config" / "source-rights.json"
    errors: list[str] = []
    if not envelopes_path.exists():
        errors.append("missing envelopes.jsonl")
        envelopes: list[dict[str, Any]] = []
    else:
        envelopes = jsonl(envelopes_path)
    emitted = [str(row.get("organisation_number") or "") for row in envelopes]
    if len(expected) != len(set(expected)):
        errors.append("input has duplicate organisation numbers")
    if len(emitted) != len(expected):
        errors.append(f"expected {len(expected)} envelopes, found {len(emitted)}")
    if set(emitted) != set(expected):
        errors.append("output organisation numbers differ from input")
    if len(emitted) != len(set(emitted)):
        errors.append("output contains duplicate organisation numbers")
    available_claims = 0
    linked_claims = 0
    for env in envelopes:
        if env.get("availability") not in ALLOWED:
            errors.append(f"invalid envelope availability for {env.get('organisation_number')}")
        for state in (env.get("modules") or {}).values():
            if state.get("state") not in ALLOWED:
                errors.append(f"invalid module state for {env.get('organisation_number')}")
        evidence = {str(item.get("id") or item.get("evidence_id") or ""): item for item in env.get("evidence") or []}
        for claim in env.get("claims") or []:
            if claim.get("availability") not in ALLOWED:
                errors.append(f"invalid claim availability for {env.get('organisation_number')}:{claim.get('field')}")
            if claim.get("availability") != "available":
                continue
            available_claims += 1
            ids = [str(value) for value in claim.get("evidence_ids") or []]
            if not ids or not all(value in evidence for value in ids):
                errors.append(f"available claim lacks linked evidence: {env.get('organisation_number')}:{claim.get('field')}")
                continue
            linked_claims += 1
            for item_id in ids:
                item = evidence[item_id]
                digest = str(item.get("content_sha256") or "")
                if not item.get("source_url") or not item.get("retrieved_at") or not digest:
                    errors.append(f"incomplete evidence metadata: {env.get('organisation_number')}:{claim.get('field')}")
                elif not re.fullmatch(r"[0-9a-fA-F]{64}", digest) or digest.lower() == hashlib.sha256(b"").hexdigest():
                    errors.append(f"invalid or empty-response evidence hash: {env.get('organisation_number')}:{claim.get('field')}")
    if not report_path.exists():
        errors.append("missing run-report.json")
    if not registry_path.exists():
        errors.append("missing source-rights registry")
    run = json.loads(report_path.read_text(encoding="utf-8")) if report_path.exists() else {}
    report = {
        "report_kind": "submission_readiness_v1",
        "input_companies": len(expected),
        "emitted_envelopes": len(emitted),
        "available_claims": available_claims,
        "available_claims_with_linked_evidence": linked_claims,
        "source_policy": run.get("source_policy"),
        "run_report_present": report_path.exists(),
        "source_rights_registry_present": registry_path.exists(),
        "passed": not errors,
        "errors": sorted(set(errors)),
        "note": "Passing this check verifies the local output contract only. Builderr independently determines score and qualification.",
    }
    text = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        Path(args.output).write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
