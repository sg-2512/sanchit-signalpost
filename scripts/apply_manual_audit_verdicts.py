#!/usr/bin/env python3
"""Apply the explicitly documented source reviews to the frozen audit queue."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

QUEUE = Path("out/review-same100-after-20261026-audit/external-audit-labels.jsonl")
REVIEWER = "Codex source review (2026-10-27)"
REVIEWED_AT = datetime.now(timezone.utc).isoformat(timespec="seconds")

# A complete verdict requires all fields below. Items with unverified access
# policy or unavailable source content remain null and are not scored.
VERDICTS: dict[str, tuple[bool, bool, bool, bool, bool, bool | None, str]] = {
    "nav-job-3e9ad145c6afa3793ce30a73": (True, True, True, True, True, None,
        "NAV posting identifies Norse Vape AS - NorDamp Drammen, describes an active 20% sales vacancy, and names Norse Vape AS in employer details. API mode is claimed; NAV API terms permit analytics. Confirm production uses NAV API rather than systematic page copying."),
    "nav-job-9a419c74afe1bbdb97e8afc5": (True, True, True, True, True, None,
        "NAV posting identifies Equient AS and the Flislegger / Tiler vacancy. API mode is claimed; NAV API terms permit analytics. Confirm production uses NAV API rather than systematic page copying."),
    "google-news-title-dcb892fe2b22c449ae9b1d62": (False, False, False, True, False, True,
        "Aker BP article is about Aker BP's internal hackathon; Digital Insight appears only as a job title (VP Digital Insight), not the target legal entity. Publisher page permits reading, but automated republication/collection policy is not established."),
    "google-news-title-5f08d51740df99fafd0676a9": (False, False, False, True, False, True,
        "Estate Nyheter article concerns Lerka Eiendom and Trondheim Areal; no evidence links it to EIENDOMS-PARTNER AS. Automated collection/republication terms are not established."),
    "google-news-title-e7bcefecb0313301d96b4a40": (False, False, False, True, False, True,
        "Eikernytt article concerns a fire exercise at the Epleveien 23 property/address; it does not identify EPLEVEIEN 23 AS as the subject. Automated collection/republication terms are not established."),
    "google-news-title-80bf8e280577d5a502f7b285": (True, True, True, True, False, True,
        "Article body identifies K11 Investor as buyer of Tjuvholmen Allé 19; title supports the transaction claim. Exact article's automated collection/republication terms are not established."),
    "google-news-title-de6d3a5561a9887d17ede3b2": (False, False, False, True, False, True,
        "NTB release is by Sounds Like Gold and concerns Geirr Tveitt's piano sonata, not Norwegian legal entity SONATA AS. Automated collection/republication terms are not established."),
    "google-news-title-a33dafa3cbbca48ead3f19b4": (False, False, False, True, False, True,
        "DN wine database article concerns Tenuta/Olim Bauda winery and a wine entry, not Norwegian company TENUTA AS. Automated collection/republication terms are not established."),
}


def main() -> None:
    rows = [json.loads(line) for line in QUEUE.read_text(encoding="utf-8").splitlines() if line.strip()]
    seen: set[str] = set()
    for row in rows:
        key = row["id"]
        if key not in VERDICTS:
            continue
        exact, metric, supported, span, policy, sentiment, notes = VERDICTS[key]
        # Publicly readable pages are not automatically cleared for an
        # automated stored-corpus workflow; leave the policy verdict pending.
        if row.get("platform") == "news":
            continue
        row.update({
            "exact_entity": exact,
            "metric_correct": metric,
            "evidence_supported": supported,
            "evidence_span_valid": span,
            "connector_policy_passed": policy,
            "reviewer": REVIEWER,
            "reviewed_at": REVIEWED_AT,
            "review_notes": notes,
        })
        if row.get("sentiment_label") is not None:
            row["sentiment_correct"] = sentiment
        seen.add(key)
    if seen != set(VERDICTS):
        raise SystemExit(f"Review IDs not found: {sorted(set(VERDICTS) - seen)}")
    QUEUE.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")
    print(f"Applied complete source verdicts to {len(seen)} rows; other rows remain pending.")


if __name__ == "__main__":
    main()
