"""Regressions from the independent November company-attribution audit."""

from unittest.mock import patch
from datetime import datetime, timezone

from bs4 import BeautifulSoup

from run_agent import apply_live_registry_precedence, extract_social_observations, extract_workforce_observation, merge_verified_jobs
from norway_company_agent.connectors.patentstyret import classify_ip_status
from norway_company_agent.connectors.kartverket import matches_registered_address
from norway_company_agent.batch import build_contract_claims_and_evidence
from norway_company_agent.budget import BudgetTracker
from norway_company_agent.hiring import evaluate_company_hiring
from norway_company_agent.http import FetchResult
from norway_company_agent.identity import assess_entity_identity, apply_entity_identity_gate, apply_website_identity_gate
from norway_company_agent.official import fetch_official_modules, normalize_group, normalize_roles
from norway_company_agent.synthesis import generate_company_synthesis
from norway_company_agent.website import _priority_links, extract_website_jobs
from norway_company_agent.verified_items import validate_job_item


def test_service_page_with_jobber_in_url_is_not_recruitment():
    from norway_company_agent.hiring import extract_website_hiring_signals

    service = {"url": "https://example.no/tjenester/nybygg-og-takjobber/", "title": "Nybygg og takjobber",
               "main_text_excerpt": "Vi gjør store og små takjobber.", "content_sha256": "a" * 64}
    site = {"careers_url": service["url"], "pages": [service]}
    assert not extract_website_hiring_signals(site)["has_career_page"]
    opening = {"url": "https://example.no/stillinger/", "title": "Ledige stillinger",
               "main_text_excerpt": "Ingen ledige stillinger for tiden.", "content_sha256": "b" * 64}
    site["pages"].append(opening)
    assert extract_website_hiring_signals(site)["career_urls"] == [opening["url"]]


def test_article_publication_date_uses_article_evidence_and_ignores_modification():
    from norway_company_agent.website import _article_publication_date

    url = "https://example.no/aktuelt/ny-avtale-signert"
    modified_only = BeautifulSoup(
        '<meta property="article:modified_time" content="2026-11-21"><article>Contract date 12 November 2026</article>',
        "html.parser",
    )
    assert _article_publication_date(modified_only, url, []) is None
    assert _article_publication_date(modified_only, url, [
        {"url": "https://example.no/other", "published_at": "2026-11-01"},
    ]) is None
    article = BeautifulSoup('<main><h1>Ny avtale</h1><p>Publisert: 12. november 2026</p></main>', "html.parser")
    assert _article_publication_date(article, url, []) == "2026-11-12"


def test_job_deadline_requires_an_explicit_label_in_the_advert():
    from norway_company_agent.website import extract_job_deadline

    assert extract_job_deadline("Søknadsfrist: 29. november. 2026.") == "2026-11-29"
    assert extract_job_deadline("Publisert: 29. november 2026.") is None
    assert extract_job_deadline("Søknadsfrist: Snarest mulig.") is None


def test_dibk_uses_exact_approval_row(monkeypatch):
    from norway_company_agent.connectors import dibk
    from norway_company_agent.external_footprint import publishable_observation

    org = "994223402"
    result = FetchResult(f"{dibk.ENDPOINT}/{org}", 200, 1, 100,
                         body={"enterprise": {"organizational_number": org, "name": "EXAMPLE AS"},
                               "status": {"approved": True, "approval_period_to": "2099-01-01"}},
                         retrieved_at="2026-11-21T10:00:00Z", content_sha256="a" * 64)
    monkeypatch.setattr(dibk, "fetch_json", lambda *args, **kwargs: result)
    approval = dibk.fetch_construction_approval(org)
    assert approval["metrics"]["status"] == "approved"
    assert publishable_observation(approval)
    result.body["enterprise"]["organizational_number"] = "923609016"
    assert dibk.fetch_construction_approval(org) is None


def test_scoped_country_contact_and_company_release_use_fetched_identity(monkeypatch):
    from norway_company_agent.connectors import scoped_company_sources as sources

    contact = "https://example.com/no/support/contact/"
    release = "https://news.cision.com/no/example-motor/r/ny-norsk-avtale,c9999999"
    company = "EXAMPLE MOTOR NORWAY AS"
    org = "958684975"
    pages = {
        contact: {"url": contact, "content_sha256": "a" * 64,
                  "visible_text_excerpt": f"Organisasjonsnummer {org} {company}"},
        release: {"url": release, "content_sha256": "b" * 64,
                  "main_text_excerpt": f"{company} har inngått en avtale i Norge.",
                  "news_items": [{"source_url": release, "title": "Norsk avtale inngått", "published_at": "2026-11-01"}]},
    }
    monkeypatch.setattr(sources, "fetch_source_page", lambda url, **_: (pages[url], None))
    presence = sources.fetch_norwegian_presence(contact, org=org, company=company, budget=None)
    assert presence["value"]["url"] == "https://example.com/no/"
    item = sources.fetch_company_press_release(release, org=org, company=company, budget=None)
    assert item["source_url"] == release and item["content_sha256"] == "b" * 64
    pages[release]["main_text_excerpt"] = "Et annet selskap har signert avtalen."
    pages[release]["visible_text_excerpt"] = company
    assert sources.fetch_company_press_release(release, org=org, company=company, budget=None) is None


def test_registered_address_does_not_count_as_operating_location():
    profile = {"organisation_number": "981929055", "name": "TELIA NORGE AS", "evidence": {
        "locations": {"status": "not_available"},
        "registered_address": {"status": "available", "source_url": "https://data.brreg.no/example",
                               "content_sha256": "a" * 64, "value": {"street": "Example 1"}},
    }}
    claims, evidence = build_contract_claims_and_evidence(profile, "2026-11-15T00:00:00Z")
    by_field = {claim["field"]: claim for claim in claims}
    assert by_field["operating_locations"]["availability"] == "not_available"
    assert by_field["registered_address"]["availability"] == "available"
    assert by_field["registered_address"]["evidence_ids"] == ["ev-registered_address-981929055"]
    assert all(item["content_sha256"] != "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855" for item in evidence)


def test_registered_address_and_subunit_are_both_published():
    profile = {"organisation_number": "981929055", "name": "TELIA NORGE AS", "evidence": {
        "locations": {"status": "available", "source_url": "https://data.brreg.no/subunits",
                      "content_sha256": "a" * 64,
                      "value": {"locations": [{"organisation_number": "123456789", "name": "TELIA SHOP"}]}},
        "registered_address": {"status": "available", "source_url": "https://data.brreg.no/entity",
                               "content_sha256": "b" * 64,
                               "value": {"location_type": "registered_business_address", "address": {"postnummer": "0150"}}},
    }}
    claims, _ = build_contract_claims_and_evidence(profile, "2026-11-21T00:00:00Z")
    by_field = {claim["field"]: claim for claim in claims}
    assert by_field["registered_address"]["availability"] == "available"
    assert by_field["operating_locations"]["availability"] == "available"
    assert by_field["registered_address"]["evidence_ids"] != by_field["operating_locations"]["evidence_ids"]


def test_registry_declared_url_does_not_verify_blocked_website():
    profile = {"organisation_number": "888567232", "evidence": {
        "registry_live": {"status": "available", "source_url": "https://data.brreg.no/enheter/888567232",
                          "content_sha256": "a" * 64,
                          "value": {"organisasjonsnummer": "888567232", "hjemmeside": "www.example.no"}},
        "website": {"status": "blocked", "source_url": "https://www.example.no/"},
    }}
    claims, evidence = build_contract_claims_and_evidence(profile, "2026-11-21T00:00:00Z")
    by_field = {claim["field"]: claim for claim in claims}
    assert by_field["official_website"]["availability"] == "blocked"
    assert by_field["registry_declared_website"]["value"] == "www.example.no"
    claim_id, = by_field["registry_declared_website"]["evidence_ids"]
    record = next(row for row in evidence if row["id"] == claim_id)
    assert record["content_sha256"] == "a" * 64
    assert "hjemmeside: www.example.no" in record["claim_span"]


def test_group_404_preserves_status_without_empty_response_evidence():
    empty_digest = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    def fetcher(url):
        return FetchResult(url, 404, 1, 0, error="HTTP 404", content_sha256=empty_digest)
    records, _ = fetch_official_modules("981929055", {"group"}, fetcher=fetcher)
    group = records["group"]
    assert group["status"] == "not_found"
    assert group["http_status"] == 404
    assert group["content_sha256"] is None


def test_geocode_requires_exact_registry_address_match():
    registered = {"adresse": ["Kongens gate 24"], "postnummer": "0153", "kommunenummer": "0301"}
    geocoded = {"formatted_address": "Kongens gate 24", "postnummer": "0153", "kommunenummer": "0301"}
    assert matches_registered_address(registered, geocoded)
    assert not matches_registered_address(registered, {**geocoded, "postnummer": "0154"})
    assert not matches_registered_address(registered, {**geocoded, "formatted_address": "Kongens gate 26"})
    assert not matches_registered_address(registered, {**geocoded, "kommunenummer": None})


def test_synthesis_distinguishes_operating_profit_from_net_loss_without_revenue():
    profile = {"organisation_number": "986308970", "name": "GURIÅSEN AS", "legal_form": "AS",
               "latest_submitted_accounts": "2025", "evidence": {
                   "financials": {"status": "available", "source_url": "https://data.brreg.no/accounts",
                                  "value": {"records": [{"period": {"tilDato": "2025-12-31"},
                                                         "revenue": None, "operating_result": 100,
                                                         "annual_result": -250}]}},
               }}
    profile["evidence"]["locations"] = {"status": "available", "value": {"locations": [{"organisation_number": "123456789"}]}}
    synthesis = generate_company_synthesis(profile)
    summary = synthesis["summary"]
    assert "operating profit of 100 NOK" in summary
    assert "net loss of 250 NOK" in summary
    assert "net profit of -250" not in summary
    assert "headquartered" not in summary
    assert {"sentence": "BRREG lists 1 registered subunit.", "evidence_ids": ["ev-locations-986308970"], "cited": True} in synthesis["sentence_citations"]


def test_request_attribution_reconciles_company_and_shared_calls():
    budget = BudgetTracker(max_requests=20, request_reserve=0)
    budget.record_request(count=2)
    with budget.attributed_to("986308970"):
        budget.record_request(count=3, cost_usd=0.01)
    with budget.attributed_to("928940837"):
        assert budget.try_start_request()
    report = budget.report()
    attribution = report["request_attribution"]
    assert attribution["shared"]["requests"] == 2
    assert attribution["by_company"]["986308970"]["requests"] == 3
    assert attribution["by_company"]["928940837"]["requests"] == 1
    assert sum(row["requests"] for row in attribution["by_company"].values()) + attribution["shared"]["requests"] == report["total_requests"]


def test_missing_source_digest_never_becomes_empty_content_hash():
    profile = {"organisation_number": "981929055", "name": "TELIA NORGE AS", "evidence": {
        "roles": {"status": "available", "source_url": "https://data.brreg.no/example",
                  "value": {"roles": [{"name": "Example"}]}},
    }}
    claims, evidence = build_contract_claims_and_evidence(profile, "2026-11-15T00:00:00Z")
    assert not any(item["id"] == "ev-roles-981929055" for item in evidence)
    assert next(claim for claim in claims if claim["field"] == "roles")["availability"] != "available"


def test_captured_privacy_operator_vetoes_a_positive_website_association():
    profile = {"organisation_number": "937691513", "name": "DVERSI AS"}
    url = "https://example.no/"
    website = {"status": "available", "source_url": url, "content_sha256": "a" * 64, "value": {
        "final_url": url, "title": "Dversi AS", "identity_text_excerpt": "Dversi AS. Org nr 937 691 513",
        "identity_assessment": {"publishable": True},
        "pages": [{"url": url + "privacy", "content_sha256": "b" * 64,
                   "main_text_excerpt": 'Hundred Solutions AS ("we," "our," or "us") operates the Dversi platform.'}],
    }}
    profile["evidence"] = {"website": website}
    claims, _ = build_contract_claims_and_evidence(profile, "2026-11-21T00:00:00Z", first_party_activity_only=True)
    claim = next(c for c in claims if c["field"] == "official_website")
    assert claim["availability"] == "ambiguous"
    assert "ev-website-operator-conflict-937691513" in claim["evidence_ids"]
    conflict = website["value"]["entity_identity_assessment"]["operator_conflict"]
    assert conflict["operator"] == "Hundred Solutions AS"
    assert conflict["source_url"] == url + "privacy"
    profile["website_attempts"] = [{"source_url": url, "publishable": True}]
    build_contract_claims_and_evidence(profile, "2026-11-21T00:00:00Z", first_party_activity_only=True)
    assert not profile["website_attempts"][0]["publishable"]
    assert profile["website_attempts"][0]["operator_conflict"] == conflict


def test_norwegian_ehf_elma_number_can_verify_site_operator():
    profile = {"organisation_number": "956298660", "name": "MUSEENE ARVEN AS"}
    url = "https://example.no/"
    website = {"status": "available", "source_url": url, "content_sha256": "a" * 64, "value": {
        "final_url": url, "title": "Museene Arven", "identity_text_excerpt": "EHF-faktura: ELMA 956 298 660",
    }}
    gated = apply_website_identity_gate(profile, website, require_legal_operator=True)
    checked = apply_entity_identity_gate(profile, gated["website"], operator_assessment=gated["assessment"])
    assert checked["website"]["value"]["identity_assessment"]["publishable"]


def test_website_evidence_uses_captured_operator_text():
    profile = {"organisation_number": "966459956", "name": "FLAGGSTANGSPESIALISTEN AS", "evidence": {
        "website": {"status": "available", "source_url": "https://flaggstang.no/",
                    "content_sha256": "a" * 64,
                    "value": {"final_url": "https://flaggstang.no/",
                              "identity_text_excerpt": "Flaggstangspesialisten AS Org.nr.: 966 459 956"}},
    }}
    _, evidence = build_contract_claims_and_evidence(profile, "2026-11-15T00:00:00Z")
    site = next(item for item in evidence if item["id"] == "ev-website-966459956")
    assert "Org.nr.: 966 459 956" in site["claim_span"]


def test_live_registry_history_and_purpose_are_exposed_with_source():
    live = {"status": "available", "source_type": "official_registry_live",
            "source_url": "https://data.brreg.no/enhetsregisteret/api/enheter/998401836",
            "content_sha256": "b" * 64,
            "value": {"name": "STIFTELSEN ØYNI", "aktivitet": ["Religiøs virksomhet."],
                      "historiskeNavn": [{"navn": "STIFTELSEN ØYNI MENIGHET", "tilDato": "2025-02-20"},
                                         {"navn": "STIFTELSEN ØYNI"}]}}
    profile = {"organisation_number": "998401836", "name": "STIFTELSEN ØYNI", "evidence": {"registry_live": live}}
    apply_live_registry_precedence(profile, live)
    claims, _ = build_contract_claims_and_evidence(profile, "2026-11-20T00:00:00Z")
    by_field = {claim["field"]: claim for claim in claims}
    assert profile["former_names"] == ["STIFTELSEN ØYNI MENIGHET"]
    assert by_field["registered_business_purpose"]["value"] == "Religiøs virksomhet."
    assert by_field["legal_name_history"]["value"] == [
        {"name": "STIFTELSEN ØYNI MENIGHET", "from": None, "until": "2025-02-20"}]
    assert by_field["legal_name_history"]["evidence_ids"] == ["ev-registry_live-998401836"]


def test_social_urls_deduplicate_www_aliases():
    profile = {"organisation_number": "981929055", "name": "TELIA NORGE AS", "evidence": {},
               "social_profiles": [
                   {"platform": "facebook", "url": "https://facebook.com/telianorge",
                    "content_sha256": "a" * 64},
                   {"platform": "facebook", "url": "https://www.facebook.com/telianorge",
                    "content_sha256": "a" * 64}]}
    claims, _ = build_contract_claims_and_evidence(profile, "2026-11-20T00:00:00Z")
    links = next(claim["value"] for claim in claims if claim["field"] == "social_profiles")
    assert len(links) == 1


def _role(code: str, description: str, first: str, last: str) -> dict:
    return {"type": {"kode": code, "beskrivelse": description},
            "person": {"navn": {"fornavn": first, "etternavn": last}}}


def test_ceo_does_not_become_board_chair():
    body = {"rollegrupper": [{"roller": [
        _role("DAGL", "Daglig leder", "Bjørn", "Moen"),
        _role("LEDE", "Styrets leder", "Per", "Backman"),
    ]}]}
    roles = normalize_roles(body)
    assert roles["ceo"] == "Bjørn Moen"
    assert roles["chair"] == "Per Backman"
    profile = {"name": "TELIA NORGE AS", "organisation_number": "981929055",
               "evidence": {"roles": {"status": "available", "value": roles}}}
    summary = generate_company_synthesis(profile)["summary"]
    assert "Per Backman" in summary and "Bjørn Moen" in summary


def test_live_registry_replaces_stale_current_fields_without_losing_old_name():
    profile = {"name": "SP TOPCO 4 AS", "employees": 11}
    live = {"status": "available", "source_type": "official_registry_live",
            "value": {"name": "SP VENTURE AS", "employees": 12}}
    apply_live_registry_precedence(profile, live)
    assert (profile["name"], profile["employees"]) == ("SP VENTURE AS", 12)
    assert profile["former_names"] == ["SP TOPCO 4 AS"]
    apply_live_registry_precedence(profile, {"status": "available", "source_type": "official_registry_bulk",
                                            "value": {"name": "SP TOPCO 4 AS", "employees": 11}})
    assert (profile["name"], profile["employees"]) == ("SP VENTURE AS", 12)


def test_zero_live_employee_count_keeps_live_source_and_value():
    profile = {"organisation_number": "931915231", "evidence": {
        "registry_live": {"status": "available", "source_type": "official_registry_live",
                          "source_url": "https://data.brreg.no/enhetsregisteret/api/enheter/931915231",
                          "content_sha256": "a" * 64, "value": {"employees": 0}},
        "registry": {"status": "available", "source_type": "official_registry_bulk",
                     "source_url": "https://example.org/bulk.csv", "content_sha256": "b" * 64,
                     "value": {"antallAnsatte": 11}},
    }}
    obs = extract_workforce_observation(profile)[0]
    assert obs["metrics"]["workforce_value"] == 0
    assert obs["content_sha256"] == "a" * 64


def test_social_link_cites_the_page_where_it_was_observed():
    profile = {"organisation_number": "981929055", "evidence": {"website": {
        "status": "available", "source_url": "https://telia.no/", "retrieved_at": "2026-11-15T00:00:00Z",
        "value": {"final_url": "https://telia.no/", "identity_assessment": {"publishable": True},
                  "social_links": [{"platform": "youtube", "url": "https://youtube.com/@telia",
                                    "source_url": "https://telia.no/om-oss/", "content_sha256": "c" * 64}]}}}}
    obs = extract_social_observations(profile)[0]
    assert obs["source_url"] == "https://telia.no/om-oss/"
    assert obs["content_sha256"] == "c" * 64


def test_bounded_job_search_cannot_claim_company_has_no_openings():
    profile = {"organisation_number": "981929055", "name": "TELIA NORGE AS",
               "website": "https://telia.no/", "evidence": {"website": {
                   "status": "available", "value": {"final_url": "https://telia.no/",
                                                       "identity_assessment": {"publishable": True}}}}}
    hiring, _ = evaluate_company_hiring(profile, [], nav_feed_jobs=[])
    assert hiring["status"] == "unknown"
    assert hiring["active_job_postings_count"] == 0


def test_inactive_ip_is_not_called_registered():
    assert classify_ip_status("Ceased") == "inactive_or_unsuccessful"
    assert classify_ip_status("Finally shelved") == "inactive_or_unsuccessful"
    assert classify_ip_status("Refused") == "inactive_or_unsuccessful"
    assert classify_ip_status("Registered") == "active_registered"
    profile = {"name": "TRONTEK AS", "organisation_number": "941768326", "evidence": {}}
    observations = [
        {"platform": "patentstyret", "signal_type": "patent_trademark_record",
         "metrics": {"status_category": status}}
        for status in ("active_registered", "inactive_or_unsuccessful")
    ]
    summary = generate_company_synthesis(profile, observations)["summary"]
    assert "1 currently registered IP right" in summary
    assert "2 registered" not in summary


def test_missing_ownership_is_unknown_instead_of_full_ownership():
    group = normalize_group({"datterselskaper": [{"organisasjonsnummer": "912345678", "navn": "Example AS"}]})
    assert group["subsidiaries"][0]["ownership_percentage"] is None


def test_other_module_evidence_cannot_verify_website_operator():
    profile = {"organisation_number": "981929055", "name": "TELIA NORGE AS",
               "evidence": {"website": {"status": "available", "source_url": "https://unrelated.no/",
                                        "value": {"final_url": "https://unrelated.no/", "title": "Unrelated business"}}}}
    other_module = [{"text": "TELIA NORGE AS org nr 981929055"}]
    result = assess_entity_identity(profile, other_module)
    assert result["orgnr_match"] == 0
    assert not result["publishable"]


def test_registry_declared_brand_still_needs_onsite_name():
    profile = {"organisation_number": "913997174", "name": "LOYDS INDUSTRI AS",
               "evidence": {
                   "registry_live": {"status": "available", "value": {"website": "www.loyds.no"}},
                   "website": {"status": "available", "source_url": "https://www.loyds.no/",
                               "value": {"final_url": "https://www.loyds.no/",
                                         "main_text_excerpt": "Loyds Industri AS makes industrial products."}},
               }}
    assert assess_entity_identity(profile)["publishable"]
    profile["evidence"]["website"]["value"]["main_text_excerpt"] = "A different business."
    assert not assess_entity_identity(profile)["publishable"]


def test_registry_domain_email_and_exact_onsite_address_can_verify_trading_name():
    profile = {"organisation_number": "999593828", "name": "LINK RETAIL AS", "evidence": {
        "registry_live": {"status": "available", "value": {
            "hjemmeside": "www.linkretail.com", "epostadresse": "geir@linkretail.com",
            "forretningsadresse": {"adresse": ["Meltzers gate 4"], "postnummer": "0257"}}}}}
    website = {"status": "available", "source_url": "https://linkretail.com/", "value": {
        "final_url": "https://linkretail.com/", "title": "Link Retail solutions",
        "main_text_excerpt": "Link Retail makes retail software for stores.",
        "visible_text_excerpt": "Link Retail makes retail software.",
        "pages": [{"url": "https://linkretail.com/contact/",
                   "visible_text_excerpt": "Head office: Meltzers gate 4, 0257 Oslo, Norway"}]}}
    result = apply_website_identity_gate(profile, website, require_legal_operator=True)
    assert result["assessment"]["publishable"]
    website["value"]["pages"][0]["visible_text_excerpt"] = "Head office: elsewhere in Oslo"
    assert not apply_website_identity_gate(profile, website, require_legal_operator=True)["assessment"]["publishable"]


def test_prior_onsite_operator_verdict_survives_entity_gate_redirect():
    website = {"status": "available", "source_url": "https://www.lyngentourist.com/",
               "value": {"final_url": "https://www.lyngentourist.com/",
                         "main_text_excerpt": "Lyngentourist AS runs local tours.",
                         "identity_assessment": {"method": "onsite_legal_operator_v1", "publishable": True,
                                                 "relationship": "exact_legal_entity"}}}
    profile = {"organisation_number": "922988374", "name": "LYNGENTOURIST AS",
               "evidence": {"registry_live": {"status": "available", "value": {"website": "www.lyngentourist.no"}}}}
    result = apply_entity_identity_gate(profile, website, operator_assessment=website["value"]["identity_assessment"])
    assert not result.get("gate_rejected")
    assert result["website"]["value"]["identity_assessment"]["publishable"]


def test_failed_official_module_is_not_reported_as_absent():
    def failed_fetch(url: str) -> FetchResult:
        return FetchResult(url, 502, 0, 0, error="upstream unavailable")

    with patch("norway_company_agent.official.time.sleep"):
        records, _ = fetch_official_modules("981929055", {"financials", "group", "locations"}, fetcher=failed_fetch)
    assert {records[k]["status"] for k in records} == {"source_error"}


def test_career_page_keeps_distinct_same_title_advert_links():
    html = ('<a href="/ledig-stilling-2">Konstruktør til utviklingsavdelingen</a>'
            '<a href="/ledig-stilling-3">Konstruktør til utviklingsavdelingen</a>')
    jobs = extract_website_jobs("https://loyds.no/ledige-stillinger", BeautifulSoup(html, "html.parser"), html)
    assert {job["url"] for job in jobs} == {
        "https://loyds.no/ledig-stilling-2", "https://loyds.no/ledig-stilling-3"}
    assert all(job["requires_ad_verification"] for job in jobs)


def test_press_archive_beats_product_news_link_with_one_news_slot():
    html = ('<a href="/produkt/nyheter-i-app/">Nyheter i appen</a>'
            '<a href="/presse/">Presse</a>')
    links = _priority_links("https://example.no/", BeautifulSoup(html, "html.parser"), limit=1)
    assert links == ["https://example.no/presse/"]


def test_direct_newsroom_takes_priority_over_feed_and_sitemap(monkeypatch):
    def unexpected_sitemap(_):
        raise AssertionError("The linked newsroom should be fetched first")

    monkeypatch.setattr("norway_company_agent.website._discover_sitemap_urls", unexpected_sitemap)
    html = '<link rel="alternate" type="application/rss+xml" href="/feed/"><a href="/nytt/">Les mer</a>'
    links = _priority_links("https://example.no/", BeautifulSoup(html, "html.parser"), limit=1, check_sitemap=True)
    assert links == ["https://example.no/nytt/"]


def test_company_site_ad_requires_fresh_career_link_and_named_employer():
    today = datetime.now(timezone.utc).isoformat()
    item = {"title": "Konstruktør til utviklingsavdelingen", "url": "https://loyds.no/ledig-stilling-2",
            "source_url": "https://loyds.no/ledig-stilling-2", "content_sha256": "a" * 64,
            "kind": "company_site_ad", "career_source_url": "https://loyds.no/ledige-stillinger",
            "active_listing": {"source_url": "https://loyds.no/ledige-stillinger",
                               "content_sha256": "b" * 64, "retrieved_at": today},
            "ad_text_excerpt": "Loyds Industri AS søker en konstruktør. Send søknad til personalavdelingen. " * 4}
    ok, _ = validate_job_item(item, org="913997174", company_name="LOYDS INDUSTRI AS",
                              site_url="https://loyds.no/", site_publishable=True)
    assert ok
    item["ad_text_excerpt"] = "Another company seeks a constructor. Send application. " * 5
    assert not validate_job_item(item, org="913997174", company_name="LOYDS INDUSTRI AS",
                                 site_url="https://loyds.no/", site_publishable=True)[0]


def test_later_ats_lookup_does_not_erase_verified_company_ad():
    site_ad = {"url": "https://loyds.no/ledig-stilling-2", "kind": "company_site_ad"}
    assert merge_verified_jobs([site_ad], []) == [site_ad]
    assert merge_verified_jobs([site_ad], [dict(site_ad)]) == [site_ad]


def test_career_index_link_cannot_be_published_as_a_fetched_ad():
    candidate = {"title": "Konstruktør til utviklingsavdelingen",
                 "url": "https://loyds.no/ledig-stilling-2",
                 "source_url": "https://loyds.no/ledige-stillinger",
                 "content_sha256": "a" * 64, "kind": "ats_ad"}
    assert not validate_job_item(candidate, org="913997174", company_name="LOYDS INDUSTRI AS",
                                 site_url="https://loyds.no/", site_publishable=True)[0]
