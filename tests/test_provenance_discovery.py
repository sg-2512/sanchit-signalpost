"""Offline checks for evidence exports, request attribution and legal-entity gates.

No network access or unstated source permissions are required.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from bs4 import BeautifulSoup

from scripts.audit_publication import audit
from norway_company_agent.batch import terminal_envelope
from norway_company_agent.budget import BudgetTracker
from norway_company_agent.verified_items import validate_job_item, validate_news_item
from norway_company_agent.website import _priority_links


ORG = "985589003"
HASH = "a" * 64
STAMP = "2026-11-18T00:00:00Z"
REG_URL = f"https://data.brreg.no/enhetsregisteret/api/enheter/{ORG}"


def _record(field: str, value: dict) -> dict:
    return {
        "field": field,
        "status": "available",
        "source_type": "official_registry_live",
        "source_url": REG_URL,
        "retrieved_at": STAMP,
        "content_sha256": HASH,
        "value": value,
    }


def test_request_attribution_reconciles_while_preserving_shared_work():
    budget = BudgetTracker(max_requests=200, max_cost_usd=5, request_reserve=0)
    budget.record_request(count=4, cost_usd=0.1)
    with budget.company_context("111111111"):
        budget.record_request(count=2, bytes_received=120, cost_usd=0.05)
        assert budget.try_start_request()
        with budget.company_context(""):
            budget.record_request(count=3, cost_usd=0.03)
        budget.record_request(count=1)
    with budget.company_context("222222222"):
        budget.record_request(count=5, cost_usd=0.4)
    report = budget.report()
    assert report["total_requests"] == 16
    assert report["requests_unattributed"] == 7
    assert report["attributed_requests_reconcile"]
    assert report["requests_by_company"] == {"111111111": 4, "222222222": 5}
    assert budget.company_metrics("111111111")["third_party_cost_usd"] == 0.05
    assert budget.company_metrics("222222222")["requests"] == 5
    assert abs(report["unattributed_cost_usd"] - 0.13) < 1e-8


def test_company_envelope_exports_valid_observation_and_fixes_dangling_citations():
    observation_id = "license-finanstilsynet-exact"
    profile = {
        "organisation_number": ORG, "name": "ARKITEKTFIRMA JON VIKOREN AS",
        "legal_form": "AS",
        "evidence": {
            "registry_live": _record("registry_live", {
                "organisation_number": ORG, "name": "ARKITEKTFIRMA JON VIKOREN AS",
                "legal_form": "AS",
            }),
        },
        "_synthesis_observations": [
            {"id": observation_id, "organisation_number": ORG, "exact_entity": True,
             "rights_status": "approved", "signal_type": "regulatory_license_record",
             "source_class": "official_api", "source_url": "https://example.no/license",
             "retrieved_at": STAMP, "content_sha256": HASH,
             "evidence_span": "Captured exact source text confirming the registration.",
             "evidence_span_kind": "source_excerpt"},
        ],
        "synthesis": {
            "summary": "A supported registration is documented.",
            "evidence_ids": [f"ev-registry_live-{ORG}", observation_id, "not-in-envelope"],
            "sentence_citations": [
                {"sentence": "Source-backed registration.", "evidence_ids": [observation_id, "not-in-envelope"], "cited": True},
                {"sentence": "Unverified other observation.", "evidence_ids": ["not-in-envelope"], "cited": True},
            ],
        },
    }
    envelope = terminal_envelope(
        profile, run_id="offline-test", modules=["registry_live"],
        started_at=STAMP, completed_at=STAMP,
    )
    evidence = {row["id"]: row for row in envelope["evidence"]}
    assert observation_id in evidence
    assert evidence[observation_id]["claim_span_verification"] == "source_excerpt"
    assert "not-in-envelope" not in envelope["synthesis"]["evidence_ids"]
    first, second = envelope["synthesis"]["sentence_citations"]
    assert first["evidence_ids"] == [observation_id] and first["cited"]
    assert second["evidence_ids"] == [] and not second["cited"]
    assert envelope["synthesis"]["citation_density_pct"] == 50.0


def test_generated_observation_note_does_not_count_as_verbatim_excerpt():
    profile = {
        "organisation_number": ORG, "name": "ARKITEKTFIRMA JON VIKOREN AS",
        "evidence": {"registry_live": _record("registry_live", {"organisation_number": ORG})},
        "_synthesis_observations": [{
            "id": "observed-note", "organisation_number": ORG,
            "source_url": REG_URL, "content_sha256": HASH,
            "evidence_span": "Generated observation summary", "source_class": "official",
        }],
        "synthesis": {
            "evidence_ids": ["observed-note"],
            "sentence_citations": [{"sentence": "An inference", "evidence_ids": ["observed-note"], "cited": True}],
        },
    }
    env = terminal_envelope(profile, run_id="test", modules=["registry_live"], started_at=STAMP, completed_at=STAMP)
    evid = next(e for e in env["evidence"] if e["id"] == "observed-note")
    assert evid["claim_span_verification"] == "observation_summary"
    assert env["synthesis"]["citation_density_pct"] == 0.0


def test_company_website_article_requires_real_full_article_proof():
    data = {
        "title": "The new company office expands in Oslo",
        "url": "https://example.no/nyheter/2026-expansion",
        "source_url": "https://example.no/nyheter/2026-expansion",
        "published_at": "2026-11-05",
        "content_sha256": HASH,
        "verification": "article_fetched",
        "exact_site_verified": True,
        "name_in_article": False,
        "article_text_excerpt": "Detailed verified first party press announcement. " * 7,
    }
    okay, reason = validate_news_item(data, company_name="EXAMPLE COMPANY AS", site_url="https://example.no/",
                                      today=date(2026, 11, 18))
    assert okay and reason == "verified_first_party_article"
    okay, _ = validate_news_item({**data, "source_url": "https://example.no/nyheter"},
                                 company_name="EXAMPLE COMPANY AS", site_url="https://example.no/",
                                 today=date(2026, 11, 18))
    assert not okay
    okay, _ = validate_news_item({**data, "exact_site_verified": False},
                                 company_name="EXAMPLE COMPANY AS", site_url="https://example.no/",
                                 today=date(2026, 11, 18))
    assert not okay


def test_job_ad_can_prove_employer_by_exact_org_number_not_just_name():
    today = datetime.now(timezone.utc).date()
    career = "https://example.no/karriere"
    ad = "https://example.no/karriere/engineer"
    item = {
        "title": "Experienced engineer",
        "kind": "company_site_ad",
        "url": ad, "source_url": ad,
        "content_sha256": HASH,
        "career_source_url": career,
        "active_listing": {"source_url": career, "content_sha256": HASH, "retrieved_at": today.isoformat()},
        "ad_text_excerpt": ("Employer registration " + ORG + ". To apply for the engineering position, "
                            "submit a structured application. " + "Description of responsibilities. " * 9),
        "valid_through": (today + timedelta(days=10)).isoformat(),
    }
    assert validate_job_item(item, org=ORG, company_name="ARCHITECTURE VIKOREN AS",
                             site_url="https://example.no/", site_publishable=True)[0]
    assert not validate_job_item({**item, "ad_text_excerpt": item["ad_text_excerpt"].replace(ORG, "111111111")},
                                 org=ORG, company_name="ARCHITECTURE VIKOREN AS",
                                 site_url="https://example.no/", site_publishable=True)[0]


def test_priority_pages_include_one_legal_one_careers_one_news_before_extra_legal():
    soup = BeautifulSoup(
        '<a href="/about">About</a>'
        '<a href="/terms">Terms</a>'
        '<a href="/karriere">Career opportunities</a>'
        '<a href="/nyheter">Newsroom</a>',
        "html.parser",
    )
    urls = _priority_links("https://example.no/", soup, limit=4, check_sitemap=False)
    assert any("/karriere" in u for u in urls)
    assert any("/nyheter" in u for u in urls)
    assert any("/about" in u or "/terms" in u for u in urls)


def test_live_audit_reconciles_shared_budget_and_detects_unverified_claims():
    record = {
        "id": "ev-primary", "source_url": REG_URL,
        "content_sha256": HASH, "retrieved_at": STAMP,
        "claim_span": "", "claim_span_verification": "unavailable",
    }
    env = {
        "organisation_number": ORG,
        "claims": [{
            "field": "legal_identity", "availability": "available",
            "evidence_ids": ["ev-primary"],
        }],
        "evidence": [record],
        "operations": {"requests": 2, "request_attribution": "measured_thread_context"},
        "synthesis": {
            "sentence_citations": [{
                "sentence": "Official registry identity", "evidence_ids": ["ev-primary"],
            }],
        },
    }
    report = {"expected_count": 1, "budget": {
        "total_requests": 5, "requests_unattributed": 3,
    }}
    result = audit([env], report)
    assert result["passed"]
    assert result["requests"]["reconciles"]
    assert result["counts"]["companies_with_request_attribution"] == 1
    assert result["counts"]["available_claims_with_source_excerpt"] == 0

    broken = {**env, "claims": [{
        "field": "legal_identity", "availability": "available",
        "evidence_ids": ["ev-missing"],
    }]}
    result = audit([broken], report)
    assert not result["passed"]
    assert result["counts"]["available_claims_without_valid_source"] == 1
