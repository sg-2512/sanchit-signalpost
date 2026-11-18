"""Precision-preserving website recall tests for PR4; no real HTTP needed."""
from __future__ import annotations

import hashlib
import io
import json
import urllib.error
from unittest.mock import patch

from norway_company_agent.budget import BudgetTracker
from norway_company_agent.connectors.regulatory_licenses import fetch_regulatory_licenses
from norway_company_agent.connectors.serper_search import (
    discover_company_websites_serper, reset_serper_search,
)
from norway_company_agent.http import fetch_json
from norway_company_agent.identity import (
    _declared_single_brand_operator_proof, apply_entity_identity_gate,
    apply_website_identity_gate,
)
from norway_company_agent.synthesis import generate_company_synthesis
from norway_company_agent.website import fetch_website


ORG = "933787141"
SOURCE_HASH = "a" * 64


def _profile():
    return {
        "organisation_number": ORG, "name": "AIOTA AS",
        "legal_form": "AS", "municipality": "OSLO",
        "evidence": {
            "registry_live": {
                "status": "available", "content_sha256": SOURCE_HASH,
                "source_url": f"https://data.brreg.no/enhetsregisteret/api/enheter/{ORG}",
                "value": {"organisation_number": ORG, "name": "AIOTA AS",
                          "hjemmeside": "https://aiota.no/"},
            },
        },
    }


def _site():
    return {
        "status": "available",
        "source_url": "https://aiota.no/",
        "content_sha256": SOURCE_HASH,
        "value": {
            "final_url": "https://aiota.no/",
            "title": "AIOTA – Technology Solutions",
            "main_text_excerpt": "AIOTA provides Norwegian industrial software and develops secure "
                                 "data products for its clients. " * 3,
            "visible_text_excerpt": "AIOTA software research and digital technology products",
            "identity_text_excerpt": "AIOTA software services",
            "pages": [],
            "structured_organisations": [],
        },
    }


def test_distinctive_registry_declared_brand_can_pass_two_actual_identity_gates():
    profile = _profile()
    site = _site()
    assert _declared_single_brand_operator_proof(profile, site["value"])
    first = apply_website_identity_gate(profile, site, require_legal_operator=True)
    assert first["website"]["value"]["identity_assessment"]["publishable"]
    second = apply_entity_identity_gate(
        profile, first["website"], operator_assessment=first["assessment"]
    )
    assert not second.get("gate_rejected")
    assert second["website"]["value"]["identity_assessment"]["publishable"]


def test_brand_with_different_legal_operator_number_is_never_promoted():
    profile = _profile()
    site = _site()
    site["value"]["identity_text_excerpt"] += " Org.nr. 930 692 522"
    site["value"]["main_text_excerpt"] += " Registered NO 930692522 MVA"
    assert not _declared_single_brand_operator_proof(profile, site["value"])
    first = apply_website_identity_gate(profile, site, require_legal_operator=True)
    assert not first["website"]["value"]["identity_assessment"]["publishable"]


def test_unregistered_brand_or_franchise_url_not_promoted():
    profile = _profile()
    site = _site()
    profile["evidence"]["registry_live"]["value"]["hjemmeside"] = "https://7-eleven.no/"
    assert not _declared_single_brand_operator_proof(profile, site["value"])
    first = apply_website_identity_gate(profile, site, require_legal_operator=True)
    assert not first["assessment"]["publishable"]


def test_serper_returns_crawl_hints_only_for_allowed_exact_name_results(monkeypatch):
    monkeypatch.setenv("SERPER_API_KEY", "dummy-test-only-key")
    reset_serper_search()
    budget = BudgetTracker(max_requests=40, max_cost_usd=5, request_reserve=0)
    body = json.dumps({
        "organic": [
            {"link": "https://aiota.no/om-oss", "title": "AIOTA AS | About us", "snippet": "AIOTA in Norway"},
            {"link": "https://www.1881.no/aiota", "title": "AIOTA AS company", "snippet": "Norway"},
        ]
    }).encode()
    class Response:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read(self, n=-1): return body[:n] if n >= 0 else body

    with patch("urllib.request.urlopen", return_value=Response()):
        result = discover_company_websites_serper(_profile(), budget=budget)
    assert len(result) == 1
    assert result[0]["origin"] == "https://aiota.no/"
    assert result[0]["proof_url"] == "https://aiota.no/om-oss"
    assert budget.report()["total_requests"] == 1
    assert not any("1881" in entry["origin"] for entry in result)


def test_serper_does_not_send_http_without_an_api_key(monkeypatch):
    monkeypatch.delenv("SERPER_API_KEY", raising=False)
    reset_serper_search()
    budget = BudgetTracker(max_requests=40, max_cost_usd=5, request_reserve=0)
    with patch("urllib.request.urlopen") as request:
        assert discover_company_websites_serper(_profile(), budget=budget) == []
        request.assert_not_called()
    assert budget.report()["total_requests"] == 0


def test_fetch_website_forwards_fast_identity_mode():
    with patch("norway_company_agent.website._fetch_website") as fake:
        fake.return_value = ({"status": "available", "value": {}}, {"requests": 0, "bytes": 0})
        fetch_website("https://aiota.no/", identity_only=True)
        assert fake.call_args.kwargs["identity_only"] is True
        fetch_website("https://aiota.no/")
        assert fake.call_args.kwargs["identity_only"] is False


def test_404_does_not_create_empty_response_sha_digest():
    response = urllib.error.HTTPError(
        "https://data.brreg.no/enhetsregisteret/api/konsernstruktur/933787141",
        404, "Not found", {}, io.BytesIO(b""),
    )
    with patch("urllib.request.urlopen", side_effect=response):
        result = fetch_json(response.url, attempts=1)
    assert result.status == 404
    assert result.content_sha256 is None


def test_financial_industry_code_does_not_invent_regulatory_approval():
    budget = BudgetTracker(max_requests=40, max_cost_usd=5, request_reserve=0)
    with patch("urllib.request.urlopen") as request:
        results = fetch_regulatory_licenses(ORG, "AIOTA AS", industry_code="66.100", budget=budget)
        request.assert_not_called()
    assert results == []
    assert budget.report()["total_requests"] == 0


def test_net_loss_disclosed_even_when_revenue_is_missing():
    profile = _profile()
    profile["evidence"]["financials"] = {
        "status": "available",
        "content_sha256": SOURCE_HASH,
        "source_url": "https://data.brreg.no/regnskapsregisteret/regnskap/" + ORG,
        "value": {
            "records": [{
                "period": {"tilDato": "2025-12-31"},
                "revenue": None,
                "annual_result": -2415,
                "operating_result": 100,
                "equity": 10000,
            }],
            "financial_trends": {"solvency_status": "Strong", "equity_ratio": 0.8},
        },
    }
    synthesis = generate_company_synthesis(profile)
    assert synthesis["annual_net_loss"] is True
    assert "annual net loss" in synthesis["financial_health"].lower()
    assert "annual net loss" in synthesis["summary"].lower()
