#!/usr/bin/env python3
"""
Signalpost Prototype & Interactive Showcase CLI.
Builds the standalone single-page application matching builderr.ai/signalpost.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Optional

# Ensure src is on sys.path
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from norway_company_agent.showcase import (
    compact_company_profile,
    render_showcase_html,
    write_showcase_html,
)

# Backwards-compatibility alias for test_poc.py
compact = compact_company_profile


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def normalize_independent_score(independent_score: dict | None) -> dict:
    result = dict(independent_score or {})
    if not result:
        return result
    score = result.get("score") or {}
    submission = score.get("submission_1000") or {}
    identity = (result.get("qualification") or {}).get("identity_gate") or {}
    if submission:
        result["qualification_passed"] = bool((result.get("qualification") or {}).get("passed"))
        result["raw_score"] = submission.get("raw_score", score.get("raw_score", 0))
        result["category_scores"] = {
            key: value.get("score", 0) for key, value in (submission.get("categories") or {}).items()
        }
        result["identity_audit"] = {
            "published_domains_correct": identity.get("published_domains_exact", 0),
            "published_socials_correct": identity.get("published_socials_exact", 0),
        }
        result["proxy"] = result.get("proxy") or {
            "submission_1000": (result.get("proxy_results_not_adopted") or {}).get("submission_1000", 0),
            "extension_250": (result.get("proxy_results_not_adopted") or {}).get("extension_250", 0),
        }
    return result


def qualification_copy(score: dict | None, independent_score: dict | None = None) -> tuple[str, str]:
    score = score or {}
    independent_score = normalize_independent_score(independent_score)
    if independent_score.get("qualification_passed"):
        independent = independent_score.get("raw_score", 0)
        proxy = (independent_score.get("proxy") or {}).get("submission_1000", score.get("raw_score", 0))
        return (
            f"Independent judge {independent}/100 · qualification passed · proxy {proxy}/100",
            "The independent score reached the 80-point target on both frozen corpora. Exact-entity publication, filing history and fresh PDF checks passed; calendar-time operation, open-ended research and broad context coverage remain unproven.",
        )
    if score.get("scorer") == "signalpost_all_source_completeness_v1":
        combined = score.get("combined") or {}
        return (
            f"Evidence completeness {combined.get('experimental_completeness_mean', 0):.2f}/100 experimental · {combined.get('strict_completeness_mean', 0):.2f}/100 strict",
            "LinkedIn discovery now enriches exact company records, but automated LinkedIn captures remain experimental. Company sites and social handles only enter the strict layer after independent identity verification.",
        )
    if "raw_score" in score:
        raw = score.get("raw_score", 0)
        if score.get("qualification_passed"):
            return (
                f"Competition proxy qualified · {raw}/100 · independent hidden score still required",
                "The corrected proxy passes its current gates. Final ranking still requires evaluator-owned hidden batches and a fresh independent review.",
            )
        failures = len(score.get("unproven_or_failed") or [])
        return (
            f"Competition proxy {raw}/100 · {failures} gate(s) unproven or failed",
            "This is an optimization measurement, not an official competition score. Open the scorecard to see the remaining hard gates.",
        )
    qualification = score.get("qualification") or {}
    weighted = score.get("weighted_score") or {}
    if qualification.get("poc_qualified"):
        points = weighted.get("verified_points")
        maximum = weighted.get("maximum_points")
        points_copy = f"{points}/{maximum} verified core points" if points is not None and maximum is not None else "core gate passed"
        return (
            f"POC qualified · {points_copy} · not production-qualified",
            "Frozen core checks passed. Search-based discovery and sentiment remain quarantined pending their own external evaluation.",
        )
    return (
        "1,000-entity frozen POC · not a production service",
        "Qualification is pending; inspect the evidence and scorecard before making a product claim.",
    )


def build(
    rows: list[dict],
    score: dict | None = None,
    control_loop: dict | None = None,
    independent_score: dict | None = None,
    external_by_org: dict[str, list[dict]] | None = None,
) -> str:
    return render_showcase_html(rows, external_by_org)


def main() -> None:
    parser = argparse.ArgumentParser(description="Build Signalpost Interactive Showcase HTML")
    parser.add_argument("--input", required=True, help="Path to profiles.jsonl")
    parser.add_argument("--score", help="Optional path to score report")
    parser.add_argument("--control-loop", help="Optional path to control loop report")
    parser.add_argument("--independent-score", help="Optional path to independent score")
    parser.add_argument("--external-observations", nargs="*", help="Paths to external observations JSONL files")
    parser.add_argument("--limit", type=int, help="Limit number of profiles embedded in showcase")
    parser.add_argument("--output", required=True, help="Output HTML file path")
    args = parser.parse_args()

    profiles_path = Path(args.input)
    if not profiles_path.exists():
        print(f"Error: {profiles_path} does not exist", file=sys.stderr)
        sys.exit(1)

    profiles = read_jsonl(profiles_path)
    if args.limit and args.limit > 0:
        profiles = profiles[:args.limit]

    observations: list[dict] = []
    for obs_file in args.external_observations or []:
        obs_path = Path(obs_file)
        if obs_path.exists():
            observations.extend(read_jsonl(obs_path))

    output_path = Path(args.output)
    write_showcase_html(profiles, observations, output_path, limit=args.limit)
    print(f"Wrote interactive showcase to {output_path} with {len(profiles)} profiles")


if __name__ == "__main__":
    main()
