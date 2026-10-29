from norway_company_agent.source_policy import (
    EXPERIMENTAL,
    OFFICIAL_SAFE,
    filter_publishable_observations,
    observation_decision,
)
from norway_company_agent.batch import evidence_terminal_state


def observation(**overrides):
    row = {
        "platform": "brreg",
        "strategy": "official_subunit_workforce",
        "source_class": "official_subunits",
        "acquisition_mode": "official_api",
    }
    row.update(overrides)
    return row


def test_official_safe_accepts_declared_official_route():
    assert observation_decision(observation()).publishable


def test_official_safe_accepts_declared_brreg_workforce_route():
    result = observation_decision(observation(strategy="official_registry_workforce", source_class="official_registry"))
    assert result.publishable
    assert result.reason == "source_rights_registry_official_safe"


def test_official_safe_quarantines_linkedin_even_when_row_claims_approved():
    result = observation_decision(observation(platform="linkedin", strategy="linkedin_guest_api", rights_status="approved"))
    assert not result.publishable
    assert result.reason == "restricted_platform:linkedin"


def test_official_safe_quarantines_undeclared_keyed_route():
    result = observation_decision(observation(platform="google_places", strategy="google_places_api_new"))
    assert not result.publishable
    assert result.reason.startswith("registry_conditional:")


def test_experimental_mode_keeps_candidates_for_measurement():
    accepted, quarantined = filter_publishable_observations(
        [observation(platform="linkedin", strategy="linkedin_guest_api")], policy=EXPERIMENTAL
    )
    assert len(accepted) == 1
    assert not quarantined


def test_official_safe_preserves_quarantine_reason():
    accepted, quarantined = filter_publishable_observations(
        [observation(platform="proff", strategy="proff_directory_lookup")], policy=OFFICIAL_SAFE
    )
    assert not accepted
    assert quarantined[0]["publication_status"] == "quarantined"


def test_internal_evidence_failures_normalize_to_contract_states():
    assert evidence_terminal_state({"status": "source_error"}) == "failed"
    assert evidence_terminal_state({"status": "budget_exhausted"}) == "failed"
    assert evidence_terminal_state({"status": "not_found"}) == "not_available"
    assert evidence_terminal_state({"status": "blocked", "note": "robots"}) == "blocked"
