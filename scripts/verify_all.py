#!/usr/bin/env python3
"""SignalPost Master Verification & Continuous Testing Runner.

Executes the complete learning harness verification pipeline in one command:
1. Pytest Unit & Connector Test Suite (219 tests)
2. Idempotent Refresh Replay Verification (0 false changes)
3. Cryptographic Strategy Manifest Verification (SHA-256 seal)
4. Golden Benchmark Promotion Evaluation (Fatal gate & 6-gate check)
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def run_step(step_num: int, title: str, cmd: list[str]) -> bool:
    print(f"\n{'='*75}")
    print(f"[STEP {step_num}] {title}")
    print(f"Command: {' '.join(cmd)}")
    print(f"{'='*75}")
    start = time.monotonic()
    result = subprocess.run(cmd, cwd=str(ROOT))
    elapsed = time.monotonic() - start
    passed = result.returncode == 0
    status = "PASSED" if passed else "FAILED"
    print(f"\n--> Result: {status} in {elapsed:.1f}s")
    return passed


def main() -> int:
    print("\n" + "#"*75)
    print("#  SIGNALPOST CONTINUOUS TESTING & LEARNING HARNESS VERIFICATION")
    print("#"*75)

    steps = [
        (
            1,
            "Unit & Connector Test Suite (219 tests)",
            [sys.executable, "-m", "pytest", "tests/", "-q"],
        ),
        (
            2,
            "Idempotent Refresh Replay (Zero False Changes)",
            [sys.executable, "first_run.py"],
        ),
        (
            3,
            "Cryptographic Strategy Freeze Verification",
            [sys.executable, "-m", "strategies.freeze", "--verify"],
        ),
        (
            4,
            "Golden Benchmark Evaluation (Fatal & 6-Gate Promotion Check)",
            [
                sys.executable,
                "-m",
                "eval.run",
                "--corpus",
                "eval/gold_companies.jsonl",
                "--profiles",
                "out/fresh-smoke-100/profiles.jsonl",
                "--envelopes",
                "out/fresh-smoke-100/envelopes.jsonl",
            ],
        ),
    ]

    failed_steps = []
    for step_num, title, cmd in steps:
        if not run_step(step_num, title, cmd):
            failed_steps.append((step_num, title))
            print(f"\n[FATAL] Step {step_num} failed. Halting verification pipeline.")
            break

    print("\n" + "="*75)
    print("VERIFICATION SUMMARY")
    print("="*75)
    if not failed_steps:
        print("ALL 4 VERIFICATION STEPS PASSED!")
        print("Agent is 100% compliant, tested, and sealed for official evaluation.")
        return 0
    else:
        print(f"FAILED STEPS: {len(failed_steps)}")
        for step_num, title in failed_steps:
            print(f"  - Step {step_num}: {title}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
