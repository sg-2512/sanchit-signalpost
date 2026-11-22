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


def test_crawl_surfaces_local_legal_links_only_on_the_same_host():
    from norway_company_agent.website import _norwegian_presence_links

    soup = BeautifulSoup("""
        <a href="/no/support/contact?ref=footer">Contact</a>
        <a href="/no/legal/company-information">Legal</a>
        <a href="/se/legal/company-information">Sweden</a>
        <a href="https://other.example.com/no/legal/company-information">Other operator</a>
        <a href="/no/nyheter">News</a>
        <a href="http://[broken">Malformed</a>
    """, "html.parser")
    assert _norwegian_presence_links("https://www.example.com/no/", soup) == [
        "https://www.example.com/no/legal/company-information",
        "https://www.example.com/no/support/contact",
    ]


def test_volvo_scoped_news_requires_exact_norwegian_entity_in_fetched_article(monkeypatch):
    from norway_company_agent.connectors import scoped_company_sources as sources
    from datetime import timedelta

    scope = "https://www.volvocars.com/no/"
    company, org = "Volvo Car Norway AS", "958684975"
    contact, newsroom = scope + "support/contact/", scope + "media/"
    release, global_release = newsroom + "leadership-release", newsroom + "global-release"
    today = datetime.now(timezone.utc).date()
    pages = {
        contact: {"url": contact, "content_sha256": "a" * 64,
                  "visible_text_excerpt": f"Organisasjonsnummer 958 684 975. Besøksadresse hovedkontor {company}.",
                  "newsroom_links": [newsroom]},
        newsroom: {"url": newsroom, "news_items": [{"url": global_release}, {"url": release, "excerpt": company}]},
        release: {"url": release, "heading": "Jovana Jovic tar over ledelsen i Volvo Car Norway",
                  "main_text_excerpt": f"Jovana Jovic overtar ledelsen for {company}.",
                  "article_publication_date": (today - timedelta(days=14)).isoformat(), "content_sha256": "b" * 64},
        global_release: {"url": global_release, "title": "Volvo Group launches a new feature",
                         "main_text_excerpt": "Volvo Cars presented a feature globally.",
                         "article_publication_date": today.isoformat(), "content_sha256": "c" * 64},
    }
    monkeypatch.setattr(sources, "fetch_source_page", lambda url, **_: (pages[url], None))
    presence = sources.fetch_norwegian_presence(contact, org=org, company=company, budget=None)
    assert presence["value"]["relationship"] == "norwegian_company_presence"
    articles = sources.fetch_scoped_newsroom(newsroom, scope=scope, org=org, company=company, budget=None)
    assert [item["source_url"] for item in articles] == [release]
    assert articles[0]["content_sha256"] == "b" * 64


def test_group_website_followup_caps_requests_and_publishes_local_presence(monkeypatch):
    import run_agent
    from norway_company_agent.connectors import scoped_company_sources

    org = "958684975"
    group = "https://www.example.com/no/"
    legal = group + "legal/company-information"
    contact = group + "support/contact"
    profile = {"organisation_number": org, "name": "Example Motor Norway AS", "website": group, "evidence": {}}
    monkeypatch.setattr(run_agent, "fetch_official_modules", lambda *args, **kwargs: ({}, []))
    for name in ("places_available", "keyless_search_available"):
        monkeypatch.setattr(run_agent, name, lambda: False)
    monkeypatch.setattr(run_agent, "probe_heuristic_domain", lambda *args, **kwargs: None)
    monkeypatch.setattr(run_agent, "fetch_website", lambda url, **_: ({
        "status": "available", "source_url": url, "value": {
            "final_url": url, "title": "Example Motor Group",
            "identity_text_excerpt": "Example Motor Group AB",
            "pages": [{"url": group, "norwegian_presence_links": [legal, contact]}],
        },
    }, {"requests": 0, "bytes": 0}))
    calls = []

    def get_page(url, *, budget):
        calls.append(url)
        assert [budget.try_start_request() for _ in range(6)] == [True, True, True, True, True, False]
        return {"url": url, "content_sha256": "a" * 64,
                "visible_text_excerpt": f"Example Motor Norway AS. Org.nr. {org}"}, None

    monkeypatch.setattr(scoped_company_sources, "fetch_source_page", get_page)
    budget = BudgetTracker(max_requests=100, request_reserve=2)
    enriched, _ = run_agent.enrich_single_company(profile, set(), ["website"], budget)
    assert calls == [legal]
    assert enriched["website"] is None
    assert enriched["website_followup"]["status"] == "available"
    assert enriched["evidence"]["norwegian_website_presence"]["value"]["url"] == group


def test_registered_subunit_website_is_only_a_discovery_lead():
    from norway_company_agent.official import normalize_locations

    locations = normalize_locations({"_embedded": {"underenheter": [
        {"organisasjonsnummer": "994223402", "hjemmeside": "branch.example.no"},
        {"organisasjonsnummer": "958684975"},
    ]}})["locations"]
    assert locations[0]["website"] == "branch.example.no"
    assert "website" not in locations[1]


def test_sult_frogner_withholds_negative_current_assets_and_liquidity():
    from norway_company_agent.official import normalize_financials, validate_balance_sheet

    filing = {"regnskapstype": "SELSKAP", "regnskapsperiode": {"fraDato": "2024-12-15", "tilDato": "2025-12-31"},
              "eiendeler": {"sumEiendeler": 15837195, "omloepsmidler": {"sumOmloepsmidler": -1191141}},
              "egenkapitalGjeld": {"egenkapital": {"sumEgenkapital": 548986},
                                  "gjeldOversikt": {"sumGjeld": 15288209, "kortsiktigGjeld": {"sumKortsiktigGjeld": 12345691}}}}
    result = normalize_financials([filing])
    record = result["records"][0]
    assert record["current_assets"] is None and record["current_ratio"] is None
    assert record["balance_sheet_issues"]["current_assets"] == "invalid_nonnegative_aggregate"
    assert "current_ratio" not in result["financial_trends"]
    assert record["equity_ratio"] > 0 and record["account_scope"] == "standalone_statutory"
    assert record["unusual_fiscal_period"]
    assert filing["eiendeler"]["omloepsmidler"]["sumOmloepsmidler"] == -1191141
    inconsistent = {"assets": 1000, "equity": 50, "debt": 50, "current_ratio": 1.5, "equity_ratio": 0.05}
    validate_balance_sheet(inconsistent)
    assert inconsistent["current_ratio"] is None and inconsistent["equity_ratio"] is None


def test_brreg_tree_outputs_moteks_own_parent_and_source_fields():
    target = "920295932"
    body = {"organisasjonsnummer": "979482620", "navn": "ASPELIN RAMM HANDEL AS", "children": [
        {"organisasjonsnummer": target, "navn": "MOTEK AS", "parentOrganisasjonsnummer": "979482620",
         "parentNavn": "ASPELIN RAMM HANDEL AS", "grunnlag": "100%", "children": [
             {"organisasjonsnummer": "927655020", "navn": "Example subsidiary", "parentOrganisasjonsnummer": target},
         ]},
        {"organisasjonsnummer": "958684975", "navn": "Example sibling", "parentOrganisasjonsnummer": "979482620"},
    ]}
    records, _ = fetch_official_modules(target, {"group"}, fetcher=lambda url: FetchResult(
        url, 200, 1, 100, body=body, content_sha256="a" * 64))
    claims, evidence = build_contract_claims_and_evidence(
        {"organisation_number": target, "name": "MOTEK AS", "evidence": records}, "2026-11-21T12:00:00Z")
    claim = next(item for item in claims if item["field"] == "group_membership")
    assert claim["value"]["parent"]["organisation_number"] == "979482620"
    assert [row["organisation_number"] for row in claim["value"]["subsidiaries"]] == ["927655020"]
    assert claim["value"]["subsidiaries"][0]["ownership_percentage"] is None
    source = next(item for item in evidence if item["id"] in claim["evidence_ids"])
    assert any(field["source_pointer"] == "/children/0/parentOrganisasjonsnummer" for field in source["source_fields"])


def test_verified_motek_job_sets_positive_hiring_signal_from_the_advert():
    site = "https://www.motek.no/"
    careers = "https://jobb.motek.no/"
    advert = careers + "jobs/8664980-butikk"
    now = datetime.now(timezone.utc).isoformat()
    job = {"title": "Butikkselger", "url": advert, "source_url": advert, "content_sha256": "a" * 64,
           "kind": "company_site_ad", "career_source_url": careers,
           "active_listing": {"source_url": careers, "content_sha256": "b" * 64, "retrieved_at": now},
           "ad_text_excerpt": "Motek AS søker en butikkselger. Send søknaden via vår egen rekrutteringsside. " * 4}
    website = {"status": "available", "source_url": site, "content_sha256": "c" * 64, "value": {
        "identity_assessment": {"publishable": True}, "verified_job_adverts": [job],
        "pages": [{"url": advert, "content_sha256": "a" * 64}]}}
    profile = {"organisation_number": "920295932", "name": "MOTEK AS", "evidence": {"website": website},
               "hiring": {"status": "unknown", "active_job_postings_count": 0}}
    claims, _ = build_contract_claims_and_evidence(profile, now)
    fields = {row["field"]: row for row in claims}
    assert fields["hiring_status"]["value"]["status"] == "actively_recruiting"
    assert fields["hiring_status"]["value"]["active_job_postings_count"] == 1
    assert fields["hiring_signal"]["availability"] == "available" and fields["hiring_signal"]["value"] == advert
    assert fields["hiring_signal"]["evidence_ids"] == fields["job_postings"]["evidence_ids"][:1]
    website["value"]["verified_job_adverts"] = []
    claims, _ = build_contract_claims_and_evidence(profile, now)
    assert next(row for row in claims if row["field"] == "hiring_signal")["availability"] == "not_available"


def test_vibakk_shortened_candidate_still_needs_first_party_operator_proof(monkeypatch):
    import socket
    from norway_company_agent.discovery import probe_heuristic_domain

    org, name = "927655020", "VIBAKK VVS TEKNIKK AS"
    profile = {"organisation_number": org, "name": name, "evidence": {}}
    def resolve(host, *args):
        if host != "vibakkvvs.no":
            raise OSError("No such host")

    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    url = probe_heuristic_domain(profile, accept_candidate=lambda url: url == "https://vibakkvvs.no")
    website = {"status": "available", "source_url": url, "value": {
        "final_url": url, "title": name, "identity_text_excerpt": f"{name} Fygleveien 74, 8370 Leknes. Org Nr: 927 655 020",
    }}
    operator = apply_website_identity_gate(profile, website, require_legal_operator=True)
    result = apply_entity_identity_gate(profile, operator["website"], operator_assessment=operator["assessment"])
    assert result["assessment"]["publishable"]


def test_mowi_registered_domain_uses_exact_controller_clause_with_page_hash():
    site, org = "https://mowi.com/", "964118191"
    profile = {"name": "MOWI ASA", "organisation_number": org, "evidence": {
        "registry_live": {"status": "available", "value": {"website": "www.mowi.com"}},
    }}
    policy = {"url": site + "privacy-policy/", "content_sha256": "a" * 64,
              "visible_text_excerpt": "In Mowi ASA (reg. 964 118 191) we are committed to protecting personal data.",
              "main_text_excerpt": "By contacting Mowi ASA through this form you consent to our processing."}
    website = {"status": "available", "source_url": site, "content_sha256": "b" * 64, "value": {
        "final_url": site, "title": "Mowi", "pages": [policy],
    }}
    operator = apply_website_identity_gate(profile, website, require_legal_operator=True)
    result = apply_entity_identity_gate(profile, operator["website"], operator_assessment=operator["assessment"])
    assert result["assessment"]["publishable"]
    assert result["website"]["value"]["operator_proof"]["source_url"] == policy["url"]
    assert result["website"]["value"]["operator_proof"]["content_sha256"] == policy["content_sha256"]


def test_company_news_excludes_incidental_mentions_and_groups_wix_translations():
    from norway_company_agent.verified_items import first_party_news_about_company
    from norway_company_agent.website import _news_translation_metadata

    assert not first_party_news_about_company("Vibakk VVS Teknikk AS", "Unique Bathroom Designs",
        "Choose a pattern for your bathroom. " * 50 + "Consult a company like Vibakk VVS Teknikk AS.")
    assert first_party_news_about_company("Mowi ASA", "Record-high revenues for Mowi",
        "Mowi reported record-high revenues for the quarter.")
    norwegian = "https://www.cageeye.com/post/ragnhild-ansatt-som-ny-leder"
    english = "https://www.cageeye.com/en/post/ragnhild-appointed-as-new-leader"
    page = BeautifulSoup(f"""<html lang="en">
        <link rel="alternate" hreflang="en-gb" href="{english}">
        <link rel="alternate" hreflang="no-no" href="{norwegian}">
        <link rel="alternate" hreflang="x-default" href="https://www.cageeye.com/">
        <link rel="alternate" hreflang="nb-no" href="https://unrelated.example/post/other">
    </html>""", "html.parser")
    assert _news_translation_metadata(page, english) == {"story_key": norwegian, "article_language": "en"}


def test_current_feed_precedes_the_generic_media_assets_page():
    soup = BeautifulSoup("""<link rel="https://api.w.org/" href="https://mowi.com/wp-json/">
        <a href="/media/">Press and media assets</a>
        <a href="/contact/">Contact</a>
        <a href="/privacy-policy/">Privacy policy</a>
        <a href="/careers/">Careers</a>
    """, "html.parser")
    links = _priority_links("https://mowi.com/", soup, limit=4)
    assert "https://mowi.com/wp-json/wp/v2/posts?per_page=5" in links
    assert "https://mowi.com/media/" not in links


def test_filing_year_and_cash_resolve_from_retained_financial_source():
    from norway_company_agent.batch import terminal_envelope, validate_envelopes
    from norway_company_agent.official import normalize_financials
    from norway_company_agent.showcase import compact_company_profile

    org = "985727139"
    newer = {"id": 2, "period": {"fraDato": "2025-01-01", "tilDato": "2025-12-31"}, "account_type": "SELSKAP",
             "record_id": 2, "currency": "NOK", "revenue": 33970000, "annual_result": 697477,
             "cash_and_bank_deposits": None}
    previous = {"period": {"fraDato": "2024-01-01", "tilDato": "2024-12-31"}, "record_id": 1,
                "account_type": "SELSKAP", "currency": "NOK", "revenue": 32000000, "annual_result": 1225000}
    financials = {"source_url": f"https://data.brreg.no/regnskapsregisteret/regnskap/{org}", "status": "available",
                  "content_sha256": "a" * 64, "value": {"records": [previous, newer]},
                  "source_fields": [{"source_pointer": "/2/id", "source_value": 2},
                                    {"source_pointer": "/2/eiendeler", "source_value": {"sumBankinnskuddOgKontanter": 781869}}]}
    profile = {"organisation_number": org, "name": "Sletten Elektro AS", "latest_submitted_accounts": "2024",
               "evidence": {"financials": financials}}
    record = terminal_envelope(profile, run_id="cash-regression", modules=[], started_at="2026-11-22T12:00:00Z",
                               completed_at="2026-11-22T12:01:00Z", first_party_activity_only=True)
    assert validate_envelopes([record], 1)["passed"]
    assert profile["latest_account_year"] == "2025"
    assert compact_company_profile(profile)["latestAccounts"] == "2025"
    assert compact_company_profile(profile)["registryLatestAccounts"] == "2024"
    assert next(claim["value"] for claim in record["claims"] if claim["field"] == "annual_accounts")["cash_and_bank_deposits"] == 781869
    assert "Annual result fell by 43.1%" in record["synthesis"]["what_changed"]
    assert "in 2024" in record["synthesis"]["what_changed"] and "in 2025" in record["synthesis"]["what_changed"]
    profile["latest_account_year"] = "2024"
    assert not validate_envelopes([record], 1)["checks"]["semantic_consistency"]
    body = [{"id": 2, "regnskapsperiode": {"tilDato": "2025-12-31"},
             "eiendeler": {"sumBankinnskuddOgKontanter": 418070,
                            "omloepsmidler": {"bankinnskuddKontanterOgLignende": 0}}}]
    assert normalize_financials(body)["records"][0]["cash_and_bank_deposits"] == 418070
    body[0]["eiendeler"]["sumBankinnskuddOgKontanter"] = 0
    assert normalize_financials(body)["records"][0]["cash_and_bank_deposits"] == 0


def test_compact_showcase_cannot_count_registry_candidate_as_exact_operator():
    from norway_company_agent.showcase import compact_company_profile, source_result_state

    url = "https://qivi.ai/"
    profile = {"organisation_number": "937987927", "name": "QIVI AS", "website": url, "evidence": {
        "website": {"source_url": url, "content_sha256": "a" * 64, "status": "available", "value": {
            "title": "Different operator", "final_url": url, "identity_assessment": {"publishable": False}}}}}
    output = compact_company_profile(profile)
    assert output["website"] is None
    assert not next(d for d in output["coverage"]["dimensions"] if d["key"] == "website")["available"]
    assert output["sourceResults"]["patents"]["state"] == "not_checked"
    assert source_result_state({"status": "not_found"}, []) == "checked_inconclusive"
    assert source_result_state({"status": "blocked"}, []) == "checked_inconclusive"


def test_site_number_with_trailing_mva_is_exact_but_conflicting_numbers_block():
    from norway_company_agent.identity import _has_legal_operator_proof
    from norway_company_agent.website import _site_identity_text

    profile = {"organisation_number": "925392820", "name": "ISOFAG AS"}
    soup = BeautifulSoup("<footer><div>925 392 820 MVA</div></footer>", "html.parser")
    record = {"final_url": "https://www.isofag.no/", "identity_text_excerpt": _site_identity_text(soup)}
    assert _has_legal_operator_proof(profile, record)
    record["identity_text_excerpt"] += "\nNO937987927MVA"
    assert not _has_legal_operator_proof(profile, record)


def test_batch_writer_preserves_unicode_without_embedding_extra_record_delimiters(tmp_path):
    import json
    from run_agent import write_jsonl

    rows = [{"title": "A\u0085B\u2028C\u2029D"}, {"title": "Årsregnskap"}]
    path = tmp_path / "envelopes.jsonl"
    write_jsonl(path, rows)
    assert [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] == rows


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
    assert record["evidence_kind"] == "structured_pointer"
    assert record["source_fields"] == [{"source_pointer": "/hjemmeside", "source_value": "www.example.no"}]
    assert "claim_span" not in record


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
                              "visible_text_excerpt": "Velkommen. Flaggstangspesialisten AS Org.nr.: 966 459 956",
                              "identity_text_excerpt": "Flaggstangspesialisten AS Org.nr.: 966 459 956"}},
    }}
    _, evidence = build_contract_claims_and_evidence(profile, "2026-11-15T00:00:00Z")
    site = next(item for item in evidence if item["id"] == "ev-website-966459956")
    assert site["evidence_kind"] == "literal_excerpt"
    assert "Org.nr.: 966 459 956" in site["claim_span"]


def test_public_summary_cites_only_finalized_claims():
    from norway_company_agent.batch import terminal_envelope

    org = "966459956"
    now = "2026-11-21T00:00:00Z"
    profile = {"organisation_number": org, "name": "EXAMPLE AS", "summary": "Unsupported appointment.",
               "synthesis": {"summary": "Unsupported appointment."}, "evidence": {
        "registry_live": {"status": "available", "source_url": f"https://data.brreg.no/api/{org}",
                          "content_sha256": "a" * 64,
                          "value": {"organisasjonsnummer": org, "navn": "EXAMPLE AS", "legal_form": "AS"}},
        "roles": {"status": "available", "value": {"roles": [{"role_code": "DAGL", "name": "Unsupported Person"}]}},
    }}
    result = terminal_envelope(profile, run_id="provenance-test", modules=[], started_at=now, completed_at=now)
    summary = result["synthesis"]
    assert "EXAMPLE AS" in summary["summary"]
    assert "Unsupported" not in summary["summary"]
    assert summary["sentence_citations"][0]["evidence_ids"] == [f"ev-registry_live-{org}"]
    assert not any(record.get("claim_span") for record in result["evidence"])


def test_concurrent_kartverket_calls_reserve_slots_before_transport(monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from io import BytesIO
    from time import sleep
    from norway_company_agent.connectors import kartverket

    tracker = BudgetTracker(max_requests=5, request_reserve=0)
    starts = []

    def send(*args, **kwargs):
        starts.append(1)
        sleep(0.01)
        return BytesIO(b'{"adresser":[]}')

    monkeypatch.setattr(kartverket.urllib.request, "urlopen", send)
    with ThreadPoolExecutor(max_workers=50) as pool:
        list(pool.map(lambda _: kartverket.geocode_address_kartverket("Eksempelvei 5", budget=tracker), range(50)))
    assert tracker.requests == len(starts) == 5


def test_public_transport_connects_to_the_validated_socket_address(monkeypatch):
    import socket
    from unittest.mock import Mock
    from norway_company_agent import public_http

    endpoint = ("93.184.216.34", 443)
    resolver = Mock(return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, "", endpoint)])
    connection = Mock()
    connection.getpeername.return_value = endpoint
    monkeypatch.setattr(public_http.socket, "getaddrinfo", resolver)
    monkeypatch.setattr(public_http.socket, "socket", Mock(return_value=connection))
    assert public_http.connect_public_socket(("public.example", 443), 2) is connection
    assert resolver.call_count == 1
    connection.connect.assert_called_once_with(endpoint)


def test_manifest_requires_active_connectors_even_with_a_valid_manifest_digest(tmp_path):
    import json
    from strategies.freeze import _manifest_digest, freeze_strategies, verify_strategies

    path = tmp_path / "strategy_manifest.json"
    data = freeze_strategies(manifest_path=path).to_dict()
    omitted = "src/norway_company_agent/connectors/dibk.py"
    del data["connector_files"][omitted]
    data["overall_manifest_hash"] = _manifest_digest(data)
    path.write_text(json.dumps(data), encoding="utf-8")
    result = verify_strategies(manifest_path=path)
    assert not result.is_valid
    assert omitted in result.missing_files


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
    website["value"]["structured_organisations"] = [{"url": "https://linkretail.com/",
                                                     "address": {"addressCountry": "United Kingdom"}}]
    assert not apply_website_identity_gate(profile, website, require_legal_operator=True)["assessment"]["publishable"]
    website["value"].pop("structured_organisations")
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


def test_refresh_ignores_failed_sources_and_unchanged_source_snapshots():
    from norway_company_agent.refresh import diff_profile

    previous = {"organisation_number": "923609016", "employees": 8, "evidence": {
        "registry_live": {"status": "available", "content_sha256": "a" * 64},
    }}
    current = {**previous, "employees": 9}
    assert diff_profile(previous, current) == []
    current["evidence"] = {"registry_live": {"status": "source_error", "content_sha256": "b" * 64}}
    assert diff_profile(previous, current) == []
    current["evidence"]["registry_live"]["status"] = "available"
    changes = diff_profile(previous, current)
    assert [change["field"] for change in changes] == ["registry.employees"]
    assert changes[0]["old_content_sha256"] == "a" * 64
    assert changes[0]["new_content_sha256"] == "b" * 64


def test_registry_does_not_retry_after_wall_budget_expires(monkeypatch):
    from unittest.mock import Mock

    from norway_company_agent import budget, official

    send_request = Mock()
    sleep = Mock()
    monkeypatch.setattr(budget, "_raw_fetch_json", send_request)
    monkeypatch.setattr(official.time, "sleep", sleep)
    tracker = budget.BudgetTracker(max_requests=10, max_time_seconds=0)
    records, results = official.fetch_official_modules("923609016", {"registry_live"}, tracker.fetch_json)
    assert records["registry_live"]["status"] == "source_error"
    assert results[0].error == "budget_exhausted"
    send_request.assert_not_called()
    sleep.assert_not_called()


def test_bulk_fallback_respects_request_budget_and_checks_registry_identity():
    from norway_company_agent.batch import profiles_from_bulk
    from norway_company_agent.budget import BudgetTracker
    from norway_company_agent.http import FetchResult

    org = "923609016"
    profile = profiles_from_bulk(None, [org], fetcher=BudgetTracker(max_requests=0).fetch_json)[0][0]
    assert profile["evidence"]["registry"]["note"] == "budget_exhausted"
    assert profile["evidence"]["registry"]["status"] == "source_error"

    def mismatched_registry(url, **kwargs):
        return FetchResult(url, 200, 0, 0, body={"organisasjonsnummer": "983650763", "navn": "Wrong company"})

    profile = profiles_from_bulk(None, [org], fetcher=mismatched_registry)[0][0]
    assert profile["name"] == f"Organisation {org}"
    assert profile["evidence"]["registry"]["status"] == "source_error"


def test_official_finalization_rechecks_cached_website_operator_proof():
    from norway_company_agent.batch import terminal_envelope

    stamp = "2026-11-22T00:00:00Z"
    profile = {"organisation_number": "923609016", "name": "Example AS", "website": "https://example.no/",
               "evidence": {"website": {"status": "available", "source_url": "https://example.no/",
                                        "content_sha256": "a" * 64, "retrieved_at": stamp,
                                        "value": {"final_url": "https://example.no/", "title": "Example",
                                                  "identity_assessment": {"publishable": True}}}}}
    article_url = "https://example.no/news/example-expands"
    profile["evidence"]["news"] = {"status": "available", "source_url": article_url,
                                   "retrieved_at": stamp, "content_sha256": "b" * 64, "value": {"items": [{
                                       "title": "Example AS expands", "url": article_url, "source_url": article_url,
                                       "published_at": "2026-11-20", "content_sha256": "b" * 64,
                                       "name_in_article": True, "verification": "article_fetched",
                                       "content_class": "independent_editorial_news",
                                   }]}}
    result = terminal_envelope(profile, run_id="test", modules=["website"], started_at=stamp,
                               completed_at=stamp, require_legal_operator=True)
    assert result["profile"]["website"] is None
    assert not any(row["field"] in {"official_website", "dated_news"} and row["availability"] == "available" for row in result["claims"])
