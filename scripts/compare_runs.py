#!/usr/bin/env python3
"""Compare two runs of the same Signalpost organisation-number set."""
from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path
from typing import Any


def rows(path: Path) -> list[dict[str, Any]]:
    items = []
    if not path.exists():
        return items
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line_str = line.strip()
            if line_str:
                try:
                    items.append(json.loads(line_str))
                except Exception:
                    pass
    return items


def summarize(directory: Path) -> dict[str, Any]:
    envelopes = rows(directory / "envelopes.jsonl")
    observations = rows(directory / "all-observations.jsonl") if (directory / "all-observations.jsonl").exists() else []
    quarantined = rows(directory / "quarantined-observations.jsonl") if (directory / "quarantined-observations.jsonl").exists() else []
    run = json.loads((directory / "run-report.json").read_text(encoding="utf-8")) if (directory / "run-report.json").exists() else {}
    fields = Counter()
    linked = 0
    for env in envelopes:
        evidence_ids = {str(item.get("id") or item.get("evidence_id") or "") for item in env.get("evidence") or []}
        for claim in env.get("claims") or []:
            if claim.get("availability") == "available":
                fields[str(claim.get("field") or "unknown")] += 1
                if claim.get("evidence_ids") and all(str(value) in evidence_ids for value in claim["evidence_ids"]):
                    linked += 1
    ops = run.get("operations") or run.get("budget") or {}
    return {
        "companies": len(envelopes),
        "organisations": sorted(str(row.get("organisation_number") or "") for row in envelopes),
        "available_claims_by_field": dict(sorted(fields.items())),
        "linked_available_claims": linked,
        "observations": len(observations),
        "quarantined_observations": len(quarantined),
        "requests": ops.get("requests") or ops.get("total_requests"),
        "runtime_seconds": run.get("wall_clock_seconds") or ops.get("elapsed_seconds"),
        "third_party_cost_usd": ops.get("third_party_cost_usd") or ops.get("total_cost_usd"),
        "source_policy": run.get("source_policy"),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline", required=True)
    parser.add_argument("--candidate", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    baseline, candidate = summarize(Path(args.baseline)), summarize(Path(args.candidate))
    if baseline["organisations"] != candidate["organisations"]:
        raise SystemExit("Runs do not contain the same organisation-number set")
    fields = sorted(set(baseline["available_claims_by_field"]) | set(candidate["available_claims_by_field"]))
    field_delta = {
        field: candidate["available_claims_by_field"].get(field, 0) - baseline["available_claims_by_field"].get(field, 0)
        for field in fields
    }
    report = {
        "report_kind": "same_input_run_comparison_v1",
        "baseline": {key: value for key, value in baseline.items() if key != "organisations"},
        "candidate": {key: value for key, value in candidate.items() if key != "organisations"},
        "delta": {
            "available_claims_by_field": field_delta,
            "linked_available_claims": candidate["linked_available_claims"] - baseline["linked_available_claims"],
            "observations": candidate["observations"] - baseline["observations"],
            "quarantined_observations": candidate["quarantined_observations"] - baseline["quarantined_observations"],
            "requests": (candidate["requests"] or 0) - (baseline["requests"] or 0),
            "runtime_seconds": (candidate["runtime_seconds"] or 0) - (baseline["runtime_seconds"] or 0),
            "third_party_cost_usd": (candidate["third_party_cost_usd"] or 0) - (baseline["third_party_cost_usd"] or 0),
        },
        "note": "This is a same-input coverage and operations comparison, not an independent accuracy score.",
    }
    Path(args.output).write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
