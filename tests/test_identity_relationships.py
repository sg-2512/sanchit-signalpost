from norway_company_agent.identity import assess_website_identity, apply_website_identity_gate
from norway_company_agent.connectors.norid import verify_norid_domain_ownership
from unittest.mock import MagicMock, patch
import json


def profile_with_site(name: str, body: str, final_url: str = "https://aiota.no/") -> dict:
    return {
        "organisation_number": "933787141",
        "name": name,
        "evidence": {
            "website": {
                "status": "available",
                "source_url": final_url,
                "value": {
                    "final_url": final_url,
                    "title": "Aiota OS",
                    "main_text_excerpt": body,
                },
            },
        },
    }


def test_product_site_owned_by_other_legal_entity_is_not_exact() -> None:
    result = assess_website_identity(profile_with_site(
        "AIOTA AS",
        "Aiota OS fra Tagtech Solutions AS er et system for lager, utstyr og vedlikehold.",
    ))
    assert not result["publishable"]
    assert result["relationship"] == "public_brand_or_related_entity"


def test_exact_legal_entity_on_site_is_publishable() -> None:
    result = assess_website_identity(profile_with_site(
        "AIOTA AS",
        "AIOTA AS (organisation number 933787141) provides a system for warehouse management.",
    ))
    assert result["publishable"]
    assert result["relationship"] == "exact_legal_entity"


def test_corporate_group_parent_relationship() -> None:
    result = assess_website_identity(profile_with_site(
        "NORDIC DRIFT AS",
        "Velkommen til Nordic Gruppen AS. Nordic Drift AS er et datterselskap i konsernet og en del av konsernet.",
        final_url="https://nordicgruppen.no/",
    ))
    assert not result["publishable"]
    assert result["relationship"] == "corporate_group_parent"


def test_apply_website_identity_gate_attaches_labels() -> None:
    profile = profile_with_site(
        "AIOTA AS",
        "AIOTA AS (organisation number 933787141) provides warehouse solutions.",
    )
    gated = apply_website_identity_gate(profile, profile["evidence"]["website"])
    val = gated["website"]["value"]
    assert val["relationship_label"] == "exact_legal_entity"
    assert "Verified official website" in val["relationship_note"]


@patch("urllib.request.urlopen")
def test_norid_name_match_is_not_legal_org_ownership(mock_open) -> None:
    response = MagicMock()
    response.read.return_value = json.dumps({
        "entities": [{
            "roles": ["registrant"],
            "vcardArray": ["vcard", [["fn", {}, "text", "Example AS"]]],
        }],
    }).encode()
    response.__enter__.return_value = response
    mock_open.return_value = response

    observation = verify_norid_domain_ownership("example.no", "912345678", "Example AS")
    assert observation is not None
    assert observation["strategy"] == "norid_candidate_discovery"
    assert observation["signal_type"] == "candidate_domain_discovery"
    assert observation["metrics"]["ownership_state"] == "registrant_name_match"
    assert not observation["metrics"]["verified_ownership"]
    assert not observation["exact_entity"]


@patch("urllib.request.urlopen")
def test_norid_org_number_match_is_legal_ownership(mock_open) -> None:
    response = MagicMock()
    response.read.return_value = json.dumps({
        "entities": [{
            "roles": ["registrant"],
            "publicIds": [{"identifier": "912345678", "type": "organisation number"}],
            "vcardArray": ["vcard", [["fn", {}, "text", "Example AS"]]],
        }],
    }).encode()
    response.__enter__.return_value = response
    mock_open.return_value = response

    observation = verify_norid_domain_ownership("example.no", "912345678", "Example AS")
    assert observation is not None
    assert observation["strategy"] == "norid_rdap_verification"
    assert observation["signal_type"] == "legal_domain_ownership"
    assert observation["metrics"]["ownership_state"] == "org_number_match"
    assert observation["metrics"]["verified_ownership"]
    assert observation["exact_entity"]
