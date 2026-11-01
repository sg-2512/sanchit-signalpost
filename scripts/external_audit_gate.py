#!/usr/bin/env python3
"""External audit gate wrapper for Signalpost live agent.

Calls evaluate_external_footprint.py as a subprocess and returns the qualification result.
"""

import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from norway_company_agent.external_footprint import publishable_observation  # noqa: F401


def call_external_audit(profiles_path: Path, observations_path: Path, labels_path: Path, output_path: Path, min_audit: int = 100, source_policy: str = "official_safe") -> dict:
    """Run evaluate_external_footprint.py as a subprocess and return its report.

    The previous implementation accidentally read a stale output file instead of
    executing the audit script.  This version launches the script, waits for it
    to write its report, then reads the freshly written output.
    """
    script_path = ROOT / "scripts" / "evaluate_external_footprint.py"
    if not script_path.exists():
        raise FileNotFoundError(f"External audit script not found: {script_path}")

    args = [
        sys.executable, str(script_path),
        f"--profiles={profiles_path}",
        f"--observations={observations_path}",
        f"--labels={labels_path}",
        f"--output={output_path}",
        f"--minimum-audit={min_audit}",
        f"--source-policy={source_policy}",
    ]
    result = subprocess.run(args, capture_output=True, text=True, timeout=600)
    if result.returncode != 0:
        raise RuntimeError(
            f"External audit script failed (exit {result.returncode}):\n"
            f"stdout: {result.stdout[-2000:]}\n"
            f"stderr: {result.stderr[-2000:]}"
        )

    # Read the freshly written output file
    audit_text = output_path.read_text(encoding="utf-8")
    audit_report = json.loads(audit_text)
    return audit_report


def main():
    parser = argparse.ArgumentParser(description="Signalpost external audit gate")
    parser.add_argument("--profiles", required=True, help="Profiles JSONL file")
    parser.add_argument("--observations", required=True, help="Observations JSONL file")
    parser.add_argument("--labels", required=True, help="Reviewer labels JSONL file")
    parser.add_argument("--output", required=True, help="Output audit report JSON file")
    parser.add_argument("--minimum-audit", type=int, default=100, help="Minimum observations for audit")
    parser.add_argument("--source-policy", default="official_safe", help="Source policy for audit")
    args = parser.parse_args()

    profiles_path = Path(args.profiles)
    observations_path = Path(args.observations)
    labels_path = Path(args.labels)
    output_path = Path(args.output)

    try:
        audit_report = call_external_audit(profiles_path, observations_path, labels_path, output_path, args.minimum_audit, args.source_policy)
        qualification = audit_report.get("qualification_passed", False)
        print(f"External audit completed: {len(audit_report.get('published_audited', []))} audited, qualification_passed={qualification}")
        if qualification:
            print("✅ External audit gate PASSED - external category will be scored")
        else:
            print("❌ External audit gate FAILED - external category remains 0")
            sys.exit(1)
    except Exception as e:
        print(f"External audit failed: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
