#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
import sys

sys.path.insert(0, str(ROOT / "src"))

from norway_company_agent.external_footprint import publishable_observation  # noqa: E402
from norway_company_agent.source_policy import OFFICIAL_SAFE, audit_snapshot_time, observation_decision  # noqa: E402


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description="Prepare an unreviewed external-evidence audit queue.")
    parser.add_argument("--profiles", required=True)
    parser.add_argument("--observations", required=True)
    parser.add_argument("--output", required=True, help="Output JSONL path for reviewers to complete")
    parser.add_argument("--sample-size", type=int, default=100, help="Minimum deterministic stratified sample size (default: 100), including all published jobs and news")
    parser.add_argument("--seed", default="signalpost-external-audit-v1", help="Stable seed used to reproduce the sample")
    parser.add_argument(
        "--source-policy",
        default=OFFICIAL_SAFE,
        choices=(OFFICIAL_SAFE, "experimental"),
        help="Publication policy used for the reviewed run (default: official_safe)",
    )
    args = parser.parse_args()
    if args.sample_size < 1:
        parser.error("--sample-size must be at least 1")

    profiles = read_jsonl(Path(args.profiles))
    observations_path = Path(args.observations)
    observations = read_jsonl(observations_path)
    snapshot_time = audit_snapshot_time(observations_path)
    profile_by_org = {str(profile.get("organisation_number")): profile for profile in profiles}
    queue: list[dict[str, Any]] = []
    seen: set[str] = set()
    for obs in observations:
        if (
            not observation_decision(obs, policy=args.source_policy, snapshot_time=snapshot_time).publishable
            or not publishable_observation(obs)
        ):
            continue
        observation_id = str(obs.get("id") or "")
        if not observation_id:
            raise ValueError("Every publishable observation must have an ID")
        if observation_id in seen:
            raise ValueError(f"Duplicate publishable observation ID: {observation_id}")
        seen.add(observation_id)
        profile = profile_by_org.get(str(obs.get("organisation_number")), {})
        identity = profile.get("legal_identity") or {}
        queue.append({
            "id": observation_id,
            "organisation_number": str(obs.get("organisation_number") or ""),
            "expected_company_name": identity.get("name") or profile.get("name") or profile.get("legal_name"),
            "platform": obs.get("platform"),
            "signal_type": obs.get("signal_type"),
            "claim": obs.get("metrics"),
            "claim_text": obs.get("text"),
            "title": obs.get("title"),
            "sentiment_label": obs.get("sentiment_label"),
            "source_url": obs.get("source_url"),
            "retrieved_at": obs.get("retrieved_at"),
            "content_sha256": obs.get("content_sha256"),
            "valid_through": obs.get("valid_through"),
            "application_due": obs.get("application_due"),
            "nav_feed": obs.get("nav_feed"),
            "evidence_span": obs.get("evidence_span"),
            "identity_proof": obs.get("identity_proof"),
            "acquisition_mode": obs.get("acquisition_mode"),
            "rights_status_claimed": obs.get("rights_status"),
            "exact_entity": None,
            "metric_correct": None,
            "evidence_supported": None,
            "evidence_span_valid": None,
            "connector_policy_passed": None,
            "sentiment_correct": None,
            "reviewer": None,
            "reviewed_at": None,
            "review_notes": None,
        })

    publishable_count = len(queue)
    strata: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in queue:
        key = (str(row.get("platform") or ""), str(row.get("signal_type") or ""))
        strata.setdefault(key, []).append(row)
    for key, entries in strata.items():
        entries.sort(key=lambda row: hashlib.sha256(f"{args.seed}:{row['id']}".encode()).hexdigest())

    ordered_strata = sorted(
        strata,
        key=lambda key: hashlib.sha256(f"{args.seed}:{key[0]}:{key[1]}".encode()).hexdigest(),
    )
    sample = [
        row for key in ordered_strata if key[1] in {"dated_news", "job_posting"}
        for row in strata[key]
    ]
    selected_ids = {row["id"] for row in sample}
    selected_orgs = {row["organisation_number"] for row in sample}
    # First spread across strata and companies; only repeat a company if needed
    # to reach the requested size.
    while len(sample) < min(args.sample_size, len(queue)):
        made_progress = False
        for key in ordered_strata:
            if len(sample) >= min(args.sample_size, len(queue)):
                break
            candidate = next(
                (row for row in strata[key]
                 if row["id"] not in selected_ids and row["organisation_number"] not in selected_orgs),
                None,
            )
            if candidate is None:
                continue
            sample.append(candidate)
            selected_ids.add(candidate["id"])
            selected_orgs.add(candidate["organisation_number"])
            made_progress = True
        if made_progress:
            continue
        for key in ordered_strata:
            if len(sample) >= min(args.sample_size, len(queue)):
                break
            candidate = next((row for row in strata[key] if row["id"] not in selected_ids), None)
            if candidate is None:
                continue
            sample.append(candidate)
            selected_ids.add(candidate["id"])
            made_progress = True
        if not made_progress:
            break

    write_jsonl(Path(args.output), sample)
    print(json.dumps({
        "publishable_observations": publishable_count,
        "queue_rows": len(sample),
        "reviewed_rows": 0,
        "sample_seed": args.seed,
        "source_policy": args.source_policy,
        "companies_in_sample": len({row["organisation_number"] for row in sample}),
        "strata_in_sample": len({(row["platform"], row["signal_type"]) for row in sample}),
        "output": str(Path(args.output)),
        "network_requests": 0,
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
