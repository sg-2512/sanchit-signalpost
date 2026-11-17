"""Regression tests for source-traceability and coverage semantics."""

from __future__ import annotations

import hashlib

from norway_company_agent.batch import build_contract_claims_and_evidence
from norway_company_agent.website import deduplicate_dated_news


SHA = "a" * 64
SOURCE = "https://data.brreg.no/enhetsregisteret/api/enheter/981929055"


def _record(field: str, value: dict | None, *, digest: str = SHA) -> dict:
    return {
        "field": field,
        "status": "available",
        "source_type": "official_registry_live",
        "source_url": SOURCE,
        "retrieved_at": "2026-11-18T00:00:00Z",
        "content_sha256": digest,
        "value": value,
    }


def test_registered_address_is_not_an_operating_location():
    profile = {
        "organisation_number": "981929055",
        "name": "TELIA NORGE AS",
        "evidence": {
            "registry_live": _record("registry_live", {
                "organisation_number": "981929055", "name": "TELIA NORGE AS",
            }),
            "registered_address": _record("registered_address", {
                "organisation_number": "981929055",
                "location_type": "registered_business_address",
                "address": {"adresse": ["Example street 3"], "postnummer": "0123"},
            }),
            "locations": _record("locations", {"locations": []}),
        },
    }
    claims, evidence = build_contract_claims_and_evidence(
        profile, "2026-11-18T00:00:00Z",
    )
    by_field = {claim["field"]: claim for claim in claims}
    assert by_field["registered_business_address"]["availability"] == "available"
    assert by_field["operating_locations"]["availability"] == "not_available"
    assert by_field["operating_locations"]["value"] is None
    assert by_field["registered_business_address"]["evidence_ids"] == [
        "ev-registered_address-981929055"
    ]
    assert any(e["id"] == "ev-registered_address-981929055" for e in evidence)


def test_actual_subunits_remain_available_as_operating_locations():
    profile = {
        "organisation_number": "981929055",
        "name": "TELIA NORGE AS",
        "evidence": {
            "locations": _record("locations", {
                "locations": [{"organisation_number": "999999999", "name": "Example subunit"}],
            }),
        },
    }
    claims, _ = build_contract_claims_and_evidence(profile, "2026-11-18T00:00:00Z")
    by_field = {claim["field"]: claim for claim in claims}
    assert by_field["operating_locations"]["availability"] == "available"
    assert by_field["registered_business_address"]["availability"] == "not_available"


def test_missing_source_hash_is_not_fabricated():
    profile = {
        "organisation_number": "981929055", "name": "TELIA NORGE AS",
        "evidence": {"news": _record("news", None, digest="")},
    }
    _, evidence = build_contract_claims_and_evidence(profile, "2026-11-18T00:00:00Z")
    news = next(item for item in evidence if item["id"] == "ev-news-981929055")
    assert news["content_sha256"] == ""
    assert news["content_sha256"] != hashlib.sha256(b"").hexdigest()
    assert news["claim_span_verification"] == "unavailable"


def test_dated_articles_sharing_title_are_not_merged():
    items = [
        {"title": "Quarterly update", "url": "https://example.no/news/one",
         "published_at": "2026-11-11", "verification": "site_listing"},
        {"title": "Quarterly update", "url": "https://example.no/news/two",
         "published_at": "2026-11-12", "verification": "site_listing"},
    ]
    output = deduplicate_dated_news(items)
    assert len(output) == 2
    assert {item["url"] for item in output} == {item["url"] for item in items}


def test_news_dedupe_keeps_article_response_over_listing():
    items = [
        {"title": "Announcement", "url": "https://example.no/news/one#top",
         "published_at": "2026-11-11", "verification": "site_listing"},
        {"title": "Announcement", "url": "https://example.no/news/one",
         "published_at": "2026-11-11", "verification": "article_fetched",
         "content_sha256": SHA},
    ]
    output = deduplicate_dated_news(items)
    assert len(output) == 1
    assert output[0]["verification"] == "article_fetched"
    assert output[0]["content_sha256"] == SHA


def test_registry_fields_add_supported_coverage_without_inference():
    profile = {
        "organisation_number": "981929055",
        "name": "TELIA NORGE AS",
        "evidence": {
            "registry_live": _record("registry_live", {
                "organisation_number": "981929055",
                "naeringskode1": {"kode": "61.200", "beskrivelse": "Wireless telecommunications"},
                "vedtektsfestetFormaal": "Provide telecommunication services",
            }),
        },
    }
    claims, _ = build_contract_claims_and_evidence(profile, "2026-11-18T00:00:00Z")
    by_field = {claim["field"]: claim for claim in claims}
    assert by_field["industry_classification"]["availability"] == "available"
    assert by_field["industry_classification"]["value"]["code"] == "61.200"
    assert by_field["industry_classification"]["evidence_ids"] == ["ev-registry_live-981929055"]
    assert by_field["registered_business_purpose"]["value"] == "Provide telecommunication services"


def test_unobserved_registry_purpose_is_unavailable():
    profile = {
        "organisation_number": "981929055", "name": "TELIA NORGE AS",
        "evidence": {"registry_live": _record("registry_live", {"organisation_number": "981929055"})},
    }
    claims, _ = build_contract_claims_and_evidence(profile, "2026-11-18T00:00:00Z")
    by_field = {claim["field"]: claim for claim in claims}
    assert by_field["industry_classification"]["availability"] == "not_available"
    assert by_field["registered_business_purpose"]["availability"] == "not_available"
