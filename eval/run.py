"""SignalPost Benchmark Evaluation Runner.

Evaluates crawler and agent outputs against the gold standard ground truth corpus,
computing claim precision, recall, coverage, evidence grounding, wrong-company publications,
and executing the 6-gate promotion engine.

Usage:
    python -m eval.run --corpus eval/gold_companies.jsonl --profiles out/dev-100/profiles.jsonl
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

# Ensure project root and src/ are importable
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from eval.promotion_gate import GateVerdict, evaluate_promotion


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _tokenize(text: str) -> list[str]:
    if not text:
        return []
    cleaned = re.sub(r"[^\w\s]", " ", text.lower(), flags=re.UNICODE)
    tokens = [t.strip() for t in cleaned.split() if len(t.strip()) > 1]
    stopwords = {"as", "asa", "ans", "da", "enk", "esek", "brl", "ba", "sa", "og", "av", "i"}
    return [t for t in tokens if t not in stopwords]


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    """Load records from a JSONL file."""
    if not path.exists():
        return []
    records = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return records


def evaluate_benchmark(
    corpus_path: Path,
    profiles_path: Path,
    envelopes_path: Optional[Path] = None,
    baseline_metrics: Optional[dict[str, Any]] = None,
    budget_envelope: Optional[dict[str, Any]] = None,
    refresh_manifest_path: Optional[Path] = None,
) -> dict[str, Any]:
    """Run full evaluation suite over candidate profiles against the gold corpus."""
    gold_records = load_jsonl(corpus_path)
    if not gold_records:
        raise ValueError(f"No gold records loaded from {corpus_path}")

    gold_map = {str(r.get("organisation_number")): r for r in gold_records}
    total_gold_count = len(gold_records)

    raw_candidates = load_jsonl(profiles_path)
    # Profiles can either be raw profile dicts or envelope dicts containing a "profile" key
    profiles_map: dict[str, dict[str, Any]] = {}
    for item in raw_candidates:
        if "profile" in item and isinstance(item["profile"], dict):
            orgnr = str(item["profile"].get("organisation_number") or item.get("organisation_number"))
            profiles_map[orgnr] = item["profile"]
        else:
            orgnr = str(item.get("organisation_number") or "")
            if orgnr:
                profiles_map[orgnr] = item

    # Load envelopes if present
    envelopes_map: dict[str, dict[str, Any]] = {}
    env_file = envelopes_path
    if not env_file and profiles_path.parent:
        candidate_env = profiles_path.parent / "envelopes.jsonl"
        if candidate_env.exists():
            env_file = candidate_env

    if env_file and env_file.exists():
        for env in load_jsonl(env_file):
            orgnr = str(env.get("organisation_number") or "")
            if orgnr:
                envelopes_map[orgnr] = env

    # Evaluation counters
    total_published_claims = 0
    tp_claims = 0
    fp_claims = 0
    wrong_company_count = 0
    wrong_company_violations: list[dict[str, Any]] = []

    total_accepted_claims = 0
    grounded_accepted_claims = 0
    ungrounded_claim_violations: list[dict[str, Any]] = []

    companies_with_external_signal = 0

    # Budget tracking
    total_requests = 0
    total_cost_usd = 0.0
    total_runtime_seconds = 0.0

    company_reports: list[dict[str, Any]] = []

    for orgnr, gold in gold_map.items():
        profile = profiles_map.get(orgnr)
        gt = gold.get("ground_truth", {})
        vc = gt.get("verification_controls", {})
        confusers = vc.get("forbidden_confusers", [])
        forbidden_domains = set(vc.get("forbidden_domains", []))

        company_rep: dict[str, Any] = {
            "organisation_number": orgnr,
            "name": gold.get("name"),
            "matched_in_output": profile is not None,
            "claims_evaluated": 0,
            "tp_claims": 0,
            "fp_claims": 0,
            "wrong_company_flags": [],
            "grounding_flags": [],
        }

        if not profile:
            company_reports.append(company_rep)
            continue

        # Extract operational budget metrics
        run_metrics = profile.get("run_metrics") or {}
        env_ops = (envelopes_map.get(orgnr) or {}).get("operations") or {}
        reqs = int(run_metrics.get("requests") or env_ops.get("requests") or 0)
        cost = float(env_ops.get("third_party_cost_usd") or 0.0)
        runtime = float(env_ops.get("runtime_ms", 0)) / 1000.0

        total_requests += reqs
        total_cost_usd += cost
        total_runtime_seconds += runtime

        # External signals check
        ext_signals = (
            profile.get("website")
            or (profile.get("hiring", {}).get("active_job_postings_count", 0) > 0)
            or (profile.get("hiring", {}).get("signals"))
        )
        if ext_signals:
            companies_with_external_signal += 1

        # Check Claim 1: Legal Name & Municipality
        legal_name_pred = profile.get("name")
        gt_legal_name = gt.get("legal_identity", {}).get("name")
        total_published_claims += 1
        company_rep["claims_evaluated"] += 1
        if legal_name_pred and gt_legal_name and legal_name_pred.strip().upper() == gt_legal_name.strip().upper():
            tp_claims += 1
            company_rep["tp_claims"] += 1
        else:
            fp_claims += 1
            company_rep["fp_claims"] += 1

        # Check Claim 2: Official Website
        pred_website = profile.get("website")
        gt_web = gt.get("official_website", {})
        gt_web_status = gt_web.get("status")

        if pred_website:
            total_published_claims += 1
            company_rep["claims_evaluated"] += 1
            pred_netloc = pred_website.lower()
            if "://" in pred_netloc:
                pred_netloc = pred_netloc.split("://", 1)[1]
            pred_netloc = pred_netloc.split("/", 1)[0]
            if pred_netloc.startswith("www."):
                pred_netloc = pred_netloc[4:]

            # Check forbidden domains
            if pred_netloc in forbidden_domains:
                fp_claims += 1
                company_rep["fp_claims"] += 1
                wrong_company_count += 1
                flag = f"Forbidden aggregator domain '{pred_netloc}' published as company website"
                company_rep["wrong_company_flags"].append(flag)
                wrong_company_violations.append({"orgnr": orgnr, "violation": flag})
            elif gt_web_status == "available":
                acc_domains = [d.replace("www.", "").lower() for d in gt_web.get("acceptable_domains", [])]
                if pred_netloc in acc_domains or pred_netloc == gt_web.get("canonical_domain"):
                    tp_claims += 1
                    company_rep["tp_claims"] += 1
                else:
                    fp_claims += 1
                    company_rep["fp_claims"] += 1
            else:
                # Website was published but ground truth is not_available
                fp_claims += 1
                company_rep["fp_claims"] += 1

        # Check Claim 3: Hiring Status
        hiring_status_pred = profile.get("hiring", {}).get("status")
        gt_hiring = gt.get("hiring", {})
        gt_hiring_status = gt_hiring.get("expected_status")
        if hiring_status_pred:
            total_published_claims += 1
            company_rep["claims_evaluated"] += 1
            if gt_hiring_status and hiring_status_pred == gt_hiring_status:
                tp_claims += 1
                company_rep["tp_claims"] += 1
            else:
                fp_claims += 1
                company_rep["fp_claims"] += 1

        # Check Wrong-Company Confusers
        profile_text_blob = json.dumps(profile, ensure_ascii=False).lower()
        for confuser in confusers:
            conf_org = confuser.get("organisation_number", "")
            conf_name = confuser.get("name", "").lower()
            if conf_org and conf_org in profile_text_blob:
                wrong_company_count += 1
                flag = f"Forbidden confuser organisation number '{conf_org}' ({confuser.get('name')}) published"
                company_rep["wrong_company_flags"].append(flag)
                wrong_company_violations.append({"orgnr": orgnr, "violation": flag})
            elif conf_name and conf_name in profile_text_blob:
                # Distinguish if legal name itself matches (it shouldn't)
                if conf_name in profile.get("name", "").lower():
                    wrong_company_count += 1
                    flag = f"Forbidden confuser legal name '{conf_name}' published"
                    company_rep["wrong_company_flags"].append(flag)
                    wrong_company_violations.append({"orgnr": orgnr, "violation": flag})

        # Check Evidence Grounding
        evidence_dict = profile.get("evidence") or {}
        for ev_field, ev_item in evidence_dict.items():
            if not isinstance(ev_item, dict):
                continue
            if ev_item.get("status") != "available":
                continue

            total_accepted_claims += 1
            # Grounding check
            sha = str(ev_item.get("content_sha256") or "")
            src_url = str(ev_item.get("source_url") or "")
            span = str(ev_item.get("claim_span") or ev_item.get("value") or "")

            is_grounded = bool(sha and src_url and len(span.strip()) >= 5)
            if is_grounded:
                grounded_accepted_claims += 1
            else:
                flag = f"Field '{ev_field}' missing valid SHA-256 digest or source URL"
                company_rep["grounding_flags"].append(flag)
                ungrounded_claim_violations.append({"orgnr": orgnr, "field": ev_field, "issue": flag})

        company_reports.append(company_rep)

    # Compute aggregate metrics
    claim_precision = (tp_claims / total_published_claims) if total_published_claims > 0 else 1.0
    # Baseline expected claims per company ~ 3 (legal_identity, website/absence, hiring/accounts)
    expected_total_facts = total_gold_count * 3
    claim_recall = min(1.0, tp_claims / expected_total_facts) if expected_total_facts > 0 else 0.0
    company_recall = (companies_with_external_signal / total_gold_count) if total_gold_count > 0 else 0.0
    composite_coverage = 0.70 * company_recall + 0.30 * claim_recall

    grounded_rate = (grounded_accepted_claims / total_accepted_claims) if total_accepted_claims > 0 else 1.0

    eval_metrics: dict[str, Any] = {
        "total_gold_companies": total_gold_count,
        "matched_companies": len(profiles_map),
        "total_published_claims": total_published_claims,
        "true_positive_claims": tp_claims,
        "false_positive_claims": fp_claims,
        "claim_precision": round(claim_precision, 4),
        "claim_recall": round(claim_recall, 4),
        "company_recall": round(company_recall, 4),
        "coverage": round(composite_coverage, 4),
        "wrong_company_count": wrong_company_count,
        "wrong_company_violations": wrong_company_violations,
        "total_accepted_claims": total_accepted_claims,
        "grounded_accepted_claims": grounded_accepted_claims,
        "grounded_evidence_rate": round(grounded_rate, 4),
        "ungrounded_claims_count": len(ungrounded_claim_violations),
        "ungrounded_claim_violations": ungrounded_claim_violations,
        "total_requests": total_requests,
        "total_cost_usd": round(total_cost_usd, 5),
        "total_runtime_seconds": round(total_runtime_seconds, 3),
        "company_reports": company_reports,
    }

    # Evaluate promotion gate if baseline provided
    gate_verdict = None
    if baseline_metrics is not None:
        gate_verdict = evaluate_promotion(
            baseline_metrics=baseline_metrics,
            challenger_metrics=eval_metrics,
            budget_envelope=budget_envelope,
        )
        eval_metrics["gate_verdict"] = gate_verdict.to_dict()

    return eval_metrics


def print_evaluation_summary(metrics: dict[str, Any]) -> None:
    """Print formatted evaluation summary to terminal."""
    print("=" * 80)
    print("SignalPost Benchmark Evaluation Report")
    print(f"Timestamp: {_utc_now_iso()}")
    print("=" * 80)
    print(f"Total Benchmark Companies : {metrics['total_gold_companies']}")
    print(f"Matched in Target Output  : {metrics['matched_companies']}")
    print(f"Total Claims Evaluated    : {metrics['total_published_claims']}")
    print(f"Claim Precision           : {metrics['claim_precision']*100:.2f}%")
    print(f"Claim Recall              : {metrics['claim_recall']*100:.2f}%")
    print(f"Composite Coverage        : {metrics['coverage']*100:.2f}%")
    print(f"Evidence Grounding Rate   : {metrics['grounded_evidence_rate']*100:.2f}%")
    print(f"Wrong-Company Count       : {metrics['wrong_company_count']} (FATAL GATE: 0 required)")
    print(f"Total Requests            : {metrics['total_requests']}")
    print(f"Total API Cost (USD)      : ${metrics['total_cost_usd']:.4f}")
    print(f"Total Runtime             : {metrics['total_runtime_seconds']:.2f}s")
    print("-" * 80)

    verdict_dict = metrics.get("gate_verdict")
    if verdict_dict:
        print("PROMOTION GATE VERDICT:")
        print(f"  Decision       : {'PROMOTED' if verdict_dict['promoted'] else 'REJECTED'}")
        print(f"  Verdict Code   : {verdict_dict['verdict_code']}")
        print(f"  Gates Passed   : {verdict_dict['gates_passed']}/{verdict_dict['total_gates']}")
        print(f"  Rationale      : {verdict_dict['rationale']}")
        if verdict_dict['failed_gate_names']:
            print(f"  Failed Gates   : {', '.join(verdict_dict['failed_gate_names'])}")
    print("=" * 80)


def main() -> int:
    parser = argparse.ArgumentParser(description="SignalPost Benchmark Evaluator & Promotion Engine CLI")
    parser.add_argument("--corpus", default=str(ROOT / "eval" / "gold_companies.jsonl"), help="Path to gold benchmark corpus JSONL")
    parser.add_argument("--profiles", required=True, help="Path to candidate profiles JSONL")
    parser.add_argument("--envelopes", default=None, help="Path to envelopes JSONL (optional)")
    parser.add_argument("--baseline-metrics", default=None, help="Path to baseline metrics JSON")
    parser.add_argument("--output", default=str(ROOT / "out" / "eval-report.json"), help="Destination path for evaluation report JSON")
    parser.add_argument("--budget-requests", type=int, default=2000, help="Max requests envelope")
    parser.add_argument("--budget-cost", type=float, default=10.0, help="Max cost USD envelope")
    parser.add_argument("--budget-time", type=float, default=2700.0, help="Max runtime seconds envelope")
    parser.add_argument("--promote", action="store_true", help="Exit code 1 if promotion fails")
    parser.add_argument("--quiet", action="store_true", help="Suppress terminal output")
    args = parser.parse_args()

    corpus_path = Path(args.corpus)
    profiles_path = Path(args.profiles)
    envelopes_path = Path(args.envelopes) if args.envelopes else None
    output_path = Path(args.output)

    if not corpus_path.exists():
        print(f"Error: Corpus file not found at {corpus_path}", file=sys.stderr)
        return 1
    if not profiles_path.exists():
        print(f"Error: Profiles file not found at {profiles_path}", file=sys.stderr)
        return 1

    baseline_metrics = None
    if args.baseline_metrics:
        b_path = Path(args.baseline_metrics)
        if b_path.exists():
            baseline_metrics = json.loads(b_path.read_text(encoding="utf-8"))

    budget_envelope = {
        "max_requests": args.budget_requests,
        "max_cost_usd": args.budget_cost,
        "max_runtime_seconds": args.budget_time,
    }

    try:
        metrics = evaluate_benchmark(
            corpus_path=corpus_path,
            profiles_path=profiles_path,
            envelopes_path=envelopes_path,
            baseline_metrics=baseline_metrics,
            budget_envelope=budget_envelope,
        )

        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(metrics, indent=2, ensure_ascii=False), encoding="utf-8")

        if not args.quiet:
            print_evaluation_summary(metrics)

        if args.promote and baseline_metrics is not None:
            verdict = metrics.get("gate_verdict", {})
            if not verdict.get("promoted", False):
                return 1

        return 0
    except Exception as exc:
        print(f"Evaluation error: {exc}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
