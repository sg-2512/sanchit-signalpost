#!/usr/bin/env python3
"""Produce a held-out connector promotion report from reviewed JSONL rows."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from norway_company_agent.promotion import promotion_report, write_promotion_report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--strategy", required=True)
    parser.add_argument("--labels", required=True, help="Independently reviewed held-out JSONL rows")
    parser.add_argument("--run-report", required=True, help="Run report JSON with requests/runtime/cost")
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    rows = [json.loads(line) for line in Path(args.labels).read_text(encoding="utf-8").splitlines() if line.strip()]
    run = json.loads(Path(args.run_report).read_text(encoding="utf-8"))
    operations = run.get("operations") or run.get("budget") or run
    report = promotion_report(
        args.strategy,
        rows,
        {
            "requests": operations.get("requests") or operations.get("total_requests"),
            "runtime_seconds": run.get("wall_clock_seconds") or operations.get("elapsed_seconds"),
            "third_party_cost_usd": operations.get("third_party_cost_usd") or operations.get("total_cost_usd"),
        },
    )
    write_promotion_report(args.output, report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["promoted"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
