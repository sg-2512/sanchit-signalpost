"""Independent regression guards for defects found in the 100-company output audit.

These are deterministic fixtures, not claims of an independent live data audit.
"""
from __future__ import annotations

from norway_company_agent.batch import build_contract_claims_and_evidence, terminal_envelope
from norway_company_agent.official import normalize_financials
from norway_company_agent.synthesis import generate_company_synthesis
from run_agent import attach_registered_business_address
from scripts.check_audit_regressions import score

ORG = "985589003"
SHA = "b" * 64
DATE = "2026-11-18T00:00:00Z"
REG_URL = f"https://data.brreg.no/enhetsregisteret/api/enheter/{ORG}"


def official_record(field, value):
    return {
        "field": field,
        "status": "available",
        "source_type": "official_registry_live",
        "source_url": REG_URL,
        "retrieved_at": DATE,
        "content_sha256": SHA,
        "value": value,
    }


def profile_fixture():
    return {
        "organisation_number": ORG, "name": "ARKITEKTFIRMA JON VIKOREN AS",
        "legal_form": "AS", "municipality": "VIK",
        "industry_label": "Architecture",
        "latest_submitted_accounts": "2025",
        "evidence": {
            "registry_live": official_record("registry_live", {
                "name": "ARKITEKTFIRMA JON VIKOREN AS",
                "organisation_number": ORG, "legal_form": "AS",
                "forretningsadresse": {"adresse": ["Example 2"], "poststed": "VIK"},
                "hjemmeside": "https://example.no/",
            }),
            "accounting_obligation": official_record("accounting_obligation", {
                "classification": "filing_observed", "latest_submitted_accounts": "2025",
            }),
            "locations": official_record("locations", {
                "locations": [{"organisation_number": "999999999", "name": "Registered subunit"}],
            }),
        },
    }


def claims_by_field(profile):
    claims, _ = build_contract_claims_and_evidence(profile, DATE)
    return {item["field"]: item for item in claims}


def test_accounting_obligation_does_not_get_overwritten_by_registered_address():
    profile = profile_fixture()
    attach_registered_business_address(
        profile, profile["evidence"]["registry_live"], {},
    )
    claims = claims_by_field(profile)
    assert claims["registered_business_address"]["availability"] == "available"
    assert claims["accounting_obligation"]["availability"] == "available"
    assert claims["accounting_obligation"]["value"] == "filing_observed"
    assert "ev-accounting_obligation-" + ORG in claims["accounting_obligation"]["evidence_ids"]


def test_registered_address_still_publishes_with_existing_operating_subunits():
    profile = profile_fixture()
    attach_registered_business_address(profile, profile["evidence"]["registry_live"], {})
    claims = claims_by_field(profile)
    assert claims["registered_business_address"]["value"]["address"]["poststed"] == "VIK"
    assert claims["operating_locations"]["availability"] == "available"
    assert claims["operating_locations"]["value"][0]["name"] == "Registered subunit"


def test_declared_website_remains_unverified_candidate():
    profile = profile_fixture()
    claims = claims_by_field(profile)
    assert claims["registry_declared_website"]["availability"] == "available"
    assert claims["registry_declared_website"]["value"]["site_identity_verified"] is False
    assert claims["official_website"]["availability"] != "available"


def test_financial_zero_numerators_are_not_missing():
    data = [{
        "id": 1,
        "regnskapsperiode": {"tilDato": "2025-12-31"},
        "eiendeler": {"sumEiendeler": 100, "omloepsmidler": {"sumOmloepsmidler": 0}},
        "egenkapitalGjeld": {
            "egenkapital": {"sumEgenkapital": 100},
            "gjeldOversikt": {"sumGjeld": 0, "kortsiktigGjeld": {"sumKortsiktigGjeld": 10}},
        },
    }]
    record = normalize_financials(data)["records"][0]
    assert record["current_ratio"] == 0.0
    assert record["liquidity_ratio"] == 0.0
    assert record["debt_to_equity"] == 0.0


def test_missing_denominator_is_still_unknown():
    data = [{
        "id": 1,
        "regnskapsperiode": {"tilDato": "2025-12-31"},
        "eiendeler": {"omloepsmidler": {"sumOmloepsmidler": 0}},
        "egenkapitalGjeld": {
            "egenkapital": {"sumEgenkapital": 0},
            "gjeldOversikt": {"sumGjeld": 0, "kortsiktigGjeld": {"sumKortsiktigGjeld": 0}},
        },
    }]
    record = normalize_financials(data)["records"][0]
    assert record["current_ratio"] is None
    assert record["debt_to_equity"] is None


def test_operating_loss_and_equity_based_solvency_are_not_conflated():
    profile = profile_fixture()
    profile["evidence"]["financials"] = official_record("financials", {
        "records": [{
            "period": {"tilDato": "2025-12-31"}, "revenue": 1000,
            "operating_result": -150, "annual_result": 5, "equity": 800,
            "equity_ratio": 0.8,
        }],
        "financial_trends": {"solvency_status": "Strong", "equity_ratio": 0.8},
    })
    synthesis = generate_company_synthesis(profile)
    assert "operating loss" in synthesis["financial_health"].lower()
    assert synthesis["solvency_status"] == "Strong"
    assert synthesis["profitability_status"] == "operating_loss"


def test_source_links_do_not_count_as_verified_claim_spans():
    profile = profile_fixture()
    synth = generate_company_synthesis(profile)
    assert synth["source_link_density"] > 0
    assert synth["citation_density"] == 0.0
    assert synth["citation_density_kind"] == "source_excerpt_presence_not_independently_audited"


def test_runtime_is_measured_when_provided_without_inventing_company_cost():
    profile = profile_fixture()
    profile["run_metrics"] = {"requests": 8, "elapsed_s": 2.53}
    envelope = terminal_envelope(
        profile, run_id="audit-test", modules=["registry_live", "locations"],
        started_at=DATE, completed_at=DATE,
    )
    assert envelope["operations"]["runtime_ms"] == 2530
    assert envelope["operations"]["third_party_cost_usd"] is None



def test_comparison_counts_missing_zero_ratio_and_misleading_health():
    profile = profile_fixture()
    profile["evidence"]["financials"] = official_record("financials", {
        "records": [{
            "period": {"tilDato": "2025-12-31"},
            "equity": 50, "debt": 0, "debt_to_equity": None,
            "operating_result": -10, "current_assets": 0,
            "short_term_debt": 25, "current_ratio": None,
        }],
    })
    env = {
        "organisation_number": ORG,
        "legal_identity": {"name": profile["name"]},
        "profile": profile,
        "claims": [],
        "synthesis": {"financial_health": "Strong", "citation_density_pct": 100},
    }
    report = score({ORG: env})
    assert report["totals"]["valid_zero_ratios_omitted"] == 2
    assert report["totals"]["operating_losses_labeled_strong_financial_health"] == 1
    assert report["totals"]["claimed_100_percent_citation_density"] == 1
