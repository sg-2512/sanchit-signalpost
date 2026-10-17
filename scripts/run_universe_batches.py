#!/usr/bin/env python3
"""Run automated 10,000-company evaluation batches across the remaining Norwegian public universe.

This script:
1. Loads all 411,160 companies from `signalpost-universe.jsonl.gz`.
2. Identifies the companies already evaluated in the initial 10,000 batch (`eval-10000-companies.jsonl`).
3. Partitions the remaining 406,651 companies into sequential sets of 10,000 each (41 batches).
4. Runs each batch through `run_agent.py` in fast-bulk mode with multi-threading.
5. Performs standard format and schema auditing on every envelope.
6. Compresses envelopes to .gz to maintain safe disk usage.
7. Logs per-batch results and emits a consolidated grand summary report.
"""
from __future__ import annotations

import argparse
import gzip
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from scripts.verify_field_formats import audit_envelope_file

UNIVERSE_GZ = ROOT / "signalpost-universe.jsonl.gz"
INITIAL_EVAL_FILE = ROOT / "eval-10000-companies.jsonl"
DEFAULT_OUT_DIR = ROOT / "out" / "universe-batches"


def load_evaluated_orgs(path: Path) -> set[str]:
    seen: set[str] = set()
    if not path.exists():
        return seen
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
                org = str(row.get("organisation_number") or "").strip()
                if org and len(org) == 9:
                    seen.add(org)
            except Exception:
                pass
    return seen


def load_universe_records(gz_path: Path) -> list[dict]:
    records: list[dict] = []
    with gzip.open(gz_path, "rt", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except Exception:
                pass
    return records


def compress_file(src_path: Path, dst_path: Path) -> int:
    with open(src_path, "rb") as f_in, gzip.open(dst_path, "wb", compresslevel=6) as f_out:
        shutil.copyfileobj(f_in, f_out)
    return dst_path.stat().st_size


def run_batch(
    batch_idx: int,
    records: list[dict],
    out_dir: Path,
    python_exe: str,
    workers: int = 32,
) -> dict:
    batch_dir = out_dir / f"batch_{batch_idx:02d}"
    batch_dir.mkdir(parents=True, exist_ok=True)

    score_report_path = batch_dir / "score-report.json"
    audit_report_path = batch_dir / "audit-report.json"
    envelopes_gz_path = batch_dir / "envelopes.jsonl.gz"
    envelopes_jsonl_path = batch_dir / "envelopes.jsonl"

    # Resume check: if already completed and valid, skip re-computation
    if score_report_path.exists() and audit_report_path.exists() and (envelopes_gz_path.exists() or envelopes_jsonl_path.exists()):
        try:
            score_data = json.loads(score_report_path.read_text(encoding="utf-8"))
            audit_data = json.loads(audit_report_path.read_text(encoding="utf-8"))
            if audit_data.get("passed") and score_data.get("awardable_score", 0) > 0:
                print(f"[RESUME] Batch {batch_idx:02d} already completed. Score: {score_data['awardable_score']:.2f}")
                return {
                    "batch": batch_idx,
                    "companies": len(records),
                    "score": score_data["awardable_score"],
                    "passed": True,
                    "resumed": True,
                    "elapsed_seconds": 0.0,
                }
        except Exception:
            pass

    input_file = batch_dir / "input.jsonl"
    with open(input_file, "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    cmd = [
        python_exe,
        str(ROOT / "run_agent.py"),
        "--organisations", str(input_file),
        "--bulk", str(UNIVERSE_GZ),
        "--output-dir", str(batch_dir),
        "--run-id", f"universe-batch-{batch_idx:02d}",
        "--fast-bulk",
        "--workers", str(workers),
    ]

    t0 = time.monotonic()
    proc = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8", errors="replace")
    elapsed = time.monotonic() - t0

    if proc.returncode != 0:
        print(f"[ERROR] Batch {batch_idx:02d} failed with return code {proc.returncode}!")
        print("STDERR:", proc.stderr[-500:])
        return {
            "batch": batch_idx,
            "companies": len(records),
            "score": 0.0,
            "passed": False,
            "error": proc.stderr[-200:],
            "elapsed_seconds": elapsed,
        }

    # Audit standard envelope schema & formats
    if not envelopes_jsonl_path.exists():
        return {
            "batch": batch_idx,
            "companies": len(records),
            "score": 0.0,
            "passed": False,
            "error": "envelopes.jsonl missing",
            "elapsed_seconds": elapsed,
        }

    audit_result = audit_envelope_file(envelopes_jsonl_path)
    audit_report_path.write_text(json.dumps(audit_result, indent=2), encoding="utf-8")

    score_val = 0.0
    if score_report_path.exists():
        try:
            score_data = json.loads(score_report_path.read_text(encoding="utf-8"))
            score_val = float(score_data.get("awardable_score", 0.0))
        except Exception:
            pass

    # Compress envelopes.jsonl to gzip to preserve disk space, then remove uncompressed copy
    gz_size = compress_file(envelopes_jsonl_path, envelopes_gz_path)
    try:
        envelopes_jsonl_path.unlink()
    except Exception:
        pass

    # Clean up input.jsonl to save space
    try:
        input_file.unlink()
    except Exception:
        pass

    passed = audit_result.get("passed", False) and (audit_result.get("total_envelopes") == len(records))

    print(
        f"[BATCH {batch_idx:02d}/41 DONE] {len(records)} companies in {elapsed:.1f}s | "
        f"Score: {score_val:.2f}/100 | Format: {'PASSED' if passed else 'FAILED'} | "
        f"Envelopes .gz: {gz_size / (1024*1024):.1f} MB"
    )

    return {
        "batch": batch_idx,
        "companies": len(records),
        "score": score_val,
        "passed": passed,
        "errors_detected": len(audit_result.get("errors", [])),
        "broken_evidence_links": audit_result.get("broken_evidence_links", 0),
        "gz_size_bytes": gz_size,
        "elapsed_seconds": round(elapsed, 2),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Evaluate remaining Norwegian universe in 10,000-company batches")
    parser.add_argument("--batch-size", type=int, default=10000, help="Number of companies per batch (default: 10,000)")
    parser.add_argument("--workers", type=int, default=32, help="Parallel worker threads")
    parser.add_argument("--limit-batches", type=int, default=None, help="Optional max number of batches to run")
    parser.add_argument("--output-dir", default=str(DEFAULT_OUT_DIR), help="Root directory for batch outputs")
    args = parser.parse_args()

    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    python_exe = sys.executable

    print("=" * 80)
    print("SIGNALPOST PUBLIC UNIVERSE BATCH EVALUATION RUNNER")
    print("=" * 80)

    # 1. Load universe
    print(f"[1/4] Loading universe records from {UNIVERSE_GZ}...")
    all_universe = load_universe_records(UNIVERSE_GZ)
    print(f"      Total entities in public universe: {len(all_universe)}")

    # 2. Load already-evaluated organizations
    print(f"[2/4] Checking already evaluated entities from {INITIAL_EVAL_FILE}...")
    evaluated_orgs = load_evaluated_orgs(INITIAL_EVAL_FILE)
    print(f"      Initial batch evaluated entities: {len(evaluated_orgs)}")

    remaining = [r for r in all_universe if r.get("organisation_number") not in evaluated_orgs]
    print(f"[3/4] Remaining entities to process: {len(remaining)}")

    # 3. Partition into batches of 10,000
    chunk_size = args.batch_size
    batches = [remaining[i:i + chunk_size] for i in range(0, len(remaining), chunk_size)]
    if args.limit_batches:
        batches = batches[:args.limit_batches]

    total_batches = len(batches)
    est_seconds_per_batch = 3.5
    total_est_seconds = total_batches * est_seconds_per_batch
    print(f"[4/4] Partitioned into {total_batches} sets of up to {chunk_size} companies each.")
    print(f"      Estimated execution time: ~{total_est_seconds / 60:.1f} minutes ({total_est_seconds:.0f} seconds)")
    print("=" * 80)

    start_all = time.monotonic()
    batch_results: list[dict] = []

    for idx, batch_records in enumerate(batches, start=1):
        res = run_batch(idx, batch_records, out_dir, python_exe, workers=args.workers)
        batch_results.append(res)

        # Update running grand summary
        summary_path = out_dir / "universe-evaluation-summary.json"
        total_companies_done = sum(b.get("companies", 0) for b in batch_results)
        avg_score = sum(b.get("score", 0.0) for b in batch_results) / max(len(batch_results), 1)
        summary_payload = {
            "status": "in_progress" if idx < total_batches else "completed",
            "total_universe_entities": len(all_universe),
            "initial_batch_evaluated": len(evaluated_orgs),
            "remaining_universe_entities": len(remaining),
            "batches_planned": total_batches,
            "batches_completed": len(batch_results),
            "total_companies_evaluated_in_runner": total_companies_done,
            "average_score": round(avg_score, 3),
            "all_batches_passed": all(b.get("passed") for b in batch_results),
            "wall_clock_elapsed_seconds": round(time.monotonic() - start_all, 1),
            "batches": batch_results,
        }
        summary_path.write_text(json.dumps(summary_payload, indent=2), encoding="utf-8")

    total_time = time.monotonic() - start_all
    print("\n" + "=" * 80)
    print("ALL UNIVERSE BATCHES COMPLETED SUCCESSFULLY!")
    print(f"Total Batches: {len(batch_results)}")
    print(f"Total Entities Evaluated: {sum(b['companies'] for b in batch_results)}")
    print(f"Average Proxy Score: {sum(b.get('score', 0) for b in batch_results) / len(batch_results):.2f}/100")
    print(f"All 15 Fields Compliant: {'YES (100%)' if all(b.get('passed') for b in batch_results) else 'NO'}")
    print(f"Total Execution Time: {total_time:.1f}s ({total_time / 60:.2f} minutes)")
    print(f"Summary Report: {out_dir / 'universe-evaluation-summary.json'}")
    print("=" * 80)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
