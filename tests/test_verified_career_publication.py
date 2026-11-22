from datetime import datetime, timedelta, timezone
import hashlib
from io import BytesIO
import json
import ssl
import urllib.error
import urllib.request
from unittest.mock import patch

import pytest

from norway_company_agent.batch import terminal_envelope
from norway_company_agent.budget import BudgetTracker
from norway_company_agent.connectors.ats_discovery import verified_career_jobs
from norway_company_agent.source_policy import filter_publishable_observations
from norway_company_agent.verified_items import validate_job_item, validate_news_item


def test_successful_empty_robots_file_differs_from_refused_access(monkeypatch):
    from norway_company_agent.connectors import ats_discovery

    url = "https://careers.soprasteria.no/robots.txt"
    budget = BudgetTracker(max_requests=10, request_reserve=0)
    monkeypatch.setattr(ats_discovery, "assert_public_url", lambda _: None)
    monkeypatch.setattr(ats_discovery, "_pace_ats_request", lambda: None)
    monkeypatch.setattr(ats_discovery._JOB_OPENER, "open", lambda *args, **kwargs: BytesIO())
    assert ats_discovery._read_job_source(url, budget, 3) == b"User-agent: *\nDisallow:\n"
    denied = urllib.error.HTTPError(url, 403, "Forbidden", {}, None)
    with patch.object(ats_discovery._JOB_OPENER, "open", side_effect=denied):
        assert ats_discovery._read_job_source(url, budget, 3) == b""


def test_linked_norwegian_careers_portal_uses_live_listing_and_real_deadline(monkeypatch):
    from norway_company_agent.connectors import ats_discovery

    site = "https://www.soprasteria.no/"
    board = "https://careers.soprasteria.no/jobs"
    advert = "https://careers.soprasteria.no/job/nyutdannet-sikkerhet-jid-9387"
    wrong = "https://careers.soprasteria.no/job/other-employer-jid-1234"
    today = datetime.now(timezone.utc).date()
    deadline = (today + timedelta(days=4)).isoformat()

    def details(employer):
        data = {"@type": "JobPosting", "title": "Nyutdannet Sikkerhet", "datePosted": today.isoformat(),
                "validThrough": "2126-11-06", "hiringOrganization": {"name": employer}}
        return ('<script type="application/ld+json">' + json.dumps(data) + '</script><main>'
                + 'Sopra Steria søker en sikkerhetskonsulent i Oslo. Søk her via vår rekrutteringsside. ' * 4
                + f'Søknadsfrist: {deadline}</main>').encode()

    pages = {"https://careers.soprasteria.no/robots.txt": b"User-agent: *\nDisallow:\n",
             board: f'<a href="{wrong}">Wrong employer</a><a href="{advert}">Nyutdannet Sikkerhet</a>'.encode(),
             wrong: details("Different Norway"), advert: details("Sopra Steria Norway")}
    monkeypatch.setattr(ats_discovery, "_read_job_source", lambda url, *_: pages[url])
    jobs = verified_career_jobs([{"url": board, "source_url": site + "kontakt"}], site_url=site,
                                company_name="SOPRA STERIA AS", org="910909088", subunit_orgs=[],
                                budget=BudgetTracker(max_requests=20), max_requests=6)
    assert [job["url"] for job in jobs] == [advert]
    assert jobs[0]["valid_through"] == deadline and jobs[0]["application_due"] == deadline
    assert validate_job_item(jobs[0], org="910909088", company_name="SOPRA STERIA AS",
                             site_url=site, site_publishable=True)[0]


def test_news_gate_rejects_dated_policy_pages_and_retains_privacy_news():
    site = "https://example.no"
    policy = f"{site}/personvern-cookies/"
    news = f"{site}/nyheter/nye-personvernverktoy-lansert/"
    item = {
        "title": "Personvern og cookies - Example",
        "url": policy,
        "source_url": policy,
        "published_at": datetime.now(timezone.utc).date().isoformat(),
        "content_sha256": "a" * 64,
        "verification": "article_fetched",
        "name_in_article": True,
    }
    args = {"company_name": "Example AS", "site_url": site}
    assert validate_news_item(item, **args) == (False, "website_policy_page_not_news")
    assert validate_news_item({**item, "url": news}, **args) == (False, "website_policy_page_not_news")
    assert validate_news_item({
        **item, "url": news, "source_url": news, "title": "Example lanserer nye personvernverktøy",
    }, **args)[0]

    saved_observation = {
        "signal_type": "dated_news", "strategy": "website_news_discovery",
        "source_class": "company_site", "source_url": policy, "article_url": policy,
        "metrics": {"title": item["title"]},
    }
    accepted, quarantined = filter_publishable_observations([saved_observation])
    assert accepted == []
    assert quarantined[0]["quarantine_reason"] == "website_policy_page_not_news"

    news_record = {
        "status": "available", "source_url": policy, "content_sha256": item["content_sha256"],
        "value": {"items": [item], "count": 1},
    }
    profile = {"organisation_number": "923609016", "name": "Example AS", "evidence": {"news": news_record}}
    now = datetime.now(timezone.utc).isoformat()
    result = terminal_envelope(profile, run_id="news-regression", modules=[], started_at=now, completed_at=now)
    claim = next(claim for claim in result["claims"] if claim["field"] == "dated_news")
    assert claim["availability"] == "not_available"
    assert news_record["status"] == "not_available"
    assert news_record["value"] == {"items": [], "count": 0}


def test_job_ad_is_excluded_from_news_and_kept_in_job_observations():
    from run_agent import extract_website_job_observations

    site = "https://example.no"
    url = f"{site}/jobs/12345-senior-engineer"
    careers = f"{site}/careers"
    now = datetime.now(timezone.utc).isoformat()
    digest = "a" * 64
    job = {
        "title": "Senior engineer", "url": url, "source_url": url,
        "kind": "company_site_ad", "content_sha256": digest, "career_source_url": careers,
        "active_listing": {"source_url": careers, "content_sha256": "b" * 64, "retrieved_at": now},
        "ad_text_excerpt": "Example AS is hiring a senior engineer. Please apply through our careers site. " * 3,
    }
    profile = {"organisation_number": "923609016", "name": "Example AS", "evidence": {"website": {
        "status": "available", "source_url": site,
        "value": {"identity_assessment": {"publishable": True}, "verified_job_adverts": [job]},
    }}}
    assert extract_website_job_observations(profile)[0]["source_url"] == url
    news = {**job, "verification": "article_fetched", "name_in_article": True, "published_at": now}
    assert validate_news_item(news, company_name="Example AS", site_url=site) == (False, "recruitment_page_not_news")
    accepted, quarantined = filter_publishable_observations([
        {"signal_type": "dated_news", "source_url": url, "strategy": "website_news_discovery"},
    ])
    assert not accepted and quarantined[0]["quarantine_reason"] == "recruitment_page_not_news"


def test_linked_sitemap_job_reaches_envelope_with_its_own_evidence():
    site = "https://example.no"
    source = f"{site}/careers"
    portal = "https://example.wd3.myworkdayjobs.com/NOR"
    sitemap = f"{portal}/siteMap.xml"
    advert = f"{portal}/job/Oslo-Norway/Software-Engineer_JR1234"
    other_employer = f"{portal}/job/Oslo-Norway/Field-Engineer_JR5678"
    blocked = f"{portal}/job/Oslo-Norway/Blocked-Engineer_JR9012"
    today = datetime.now(timezone.utc).date()

    def job_page(employer):
        data = {
            "@type": "JobPosting", "title": "Software Engineer",
            "datePosted": today.isoformat(),
            "validThrough": (today + timedelta(days=7)).isoformat(),
            "hiringOrganization": {"name": employer},
        }
        return f'<script type="application/ld+json">{json.dumps(data)}</script>'.encode()

    raw = job_page("1200 Example ASA")
    pages = {
        "https://example.wd3.myworkdayjobs.com/robots.txt": (
            f"Sitemap: {sitemap}\nUser-agent: *\nDisallow: /NOR/job/*/Blocked*"
        ).encode(),
        sitemap: (
            f'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
            f"<url><loc>{advert}</loc></url><url><loc>{other_employer}</loc></url>"
            f"<url><loc>{blocked}</loc></url></urlset>"
        ).encode(),
        advert: raw,
        other_employer: job_page("Example UK Ltd"),
    }
    with patch("norway_company_agent.connectors.ats_discovery._read_job_source", side_effect=lambda url, *_: pages[url]) as read:
        jobs = verified_career_jobs(
            [{"url": portal, "source_url": source}], site_url=site, company_name="EXAMPLE ASA",
            org="923609016", subunit_orgs=(), budget=BudgetTracker(max_requests=20), max_requests=5,
        )
    assert blocked not in [call.args[0] for call in read.call_args_list]
    assert [job["url"] for job in jobs] == [advert]
    assert validate_job_item(
        jobs[0], org="923609016", company_name="EXAMPLE ASA", site_url=site, site_publishable=True,
    )[0]
    profile = {
        "organisation_number": "923609016", "name": "EXAMPLE ASA", "website": site,
        "evidence": {
            "registry": {"status": "available", "content_sha256": "b" * 64,
                         "source_url": "https://data.brreg.no/enhetsregisteret/api/enheter/923609016",
                         "value": {"navn": "EXAMPLE ASA"}},
            "website": {"status": "available", "source_url": site, "content_sha256": "a" * 64,
                        "value": {"final_url": site, "title": "Example ASA",
                                  "identity_text_excerpt": "Example ASA, org.nr 923609016",
                                  "identity_assessment": {"publishable": True}}},
            "jobs": {"status": "available", "source_url": advert, "value": {"items": jobs}},
        },
    }
    stamp = f"{today.isoformat()}T12:00:00Z"
    envelope = terminal_envelope(
        profile, run_id="test", modules=["registry", "website"], started_at=stamp,
        completed_at=stamp, first_party_activity_only=True,
    )
    claim = next(c for c in envelope["claims"] if c["field"] == "job_postings")
    assert claim["availability"] == "available"
    assert claim["value"]["url"] == advert
    evidence = next(e for e in envelope["evidence"] if e["id"] in claim["evidence_ids"])
    assert evidence["source_url"] == advert
    assert evidence["content_sha256"] == hashlib.sha256(raw).hexdigest()


def test_official_envelope_rejects_search_derived_news_even_if_marked_verified():
    from norway_company_agent.batch import build_contract_claims_and_evidence

    source = "https://www.fotmob.com/players/1740316"
    profile = {
        "organisation_number": "929634691", "name": "SEMBER AS",
        "evidence": {"news": {"status": "available", "value": {"items": [{
            "title": "Bella Sember", "url": source, "source_url": source,
            "published_at": "2026-10-01", "content_sha256": "a" * 64,
            "verification": "article_fetched", "name_in_article": True,
        }]}}},
    }
    claims, _ = build_contract_claims_and_evidence(
        profile, "2026-11-13T12:00:00Z", first_party_activity_only=True,
    )
    assert next(c for c in claims if c["field"] == "dated_news")["availability"] == "not_available"


def test_brand_overlap_cannot_publish_another_legal_operators_website_or_news():
    from norway_company_agent.batch import build_contract_claims_and_evidence
    from norway_company_agent.identity import apply_website_identity_gate
    from norway_company_agent.website import _site_identity_text
    from bs4 import BeautifulSoup

    site = "https://www.seafood.no/"
    article = f"{site}news/seafood-council-campaign"
    footer = BeautifulSoup("<footer>Norges Sjomatrad AS · Org.nr. 988 597 627</footer>", "html.parser")
    website = {
        "status": "available", "source_url": site, "content_sha256": "a" * 64,
        "value": {
            "final_url": site, "title": "Norwegian Seafood Council", "identity_assessment": {"publishable": True},
            "identity_text_excerpt": _site_identity_text(footer),
            "main_text_excerpt": "The Norwegian Seafood Council works to promote Norwegian seafood.",
            "news_items": [{"title": "Seafood Council launches a campaign", "url": article,
                            "source_url": site, "published_at": "2026-11-01", "content_sha256": "a" * 64}],
        },
    }
    profile = {"organisation_number": "933989208", "name": "A-SEAFOOD AS", "website": site, "evidence": {
        "registry": {"value": {"hjemmeside": site}}, "website": website,
    }}
    assessed = apply_website_identity_gate(profile, website, require_legal_operator=True)
    assert not assessed["website"]["value"]["identity_assessment"]["publishable"]
    claims, _ = build_contract_claims_and_evidence(profile, "2026-11-13T12:00:00Z", first_party_activity_only=True)
    by_field = {claim["field"]: claim for claim in claims}
    assert by_field["official_website"]["availability"] == "ambiguous"
    assert by_field["dated_news"]["availability"] == "not_available"


def test_official_site_proof_accepts_structured_exact_legal_name():
    from norway_company_agent.identity import apply_website_identity_gate

    profile = {"organisation_number": "923609016", "name": "EQUINOR ASA"}
    website = {"status": "available", "value": {
        "final_url": "https://www.equinor.com/", "title": "Energy for people and society",
        "structured_organisations": [{"name": "Equinor ASA"}],
    }}
    assessed = apply_website_identity_gate(profile, website, require_legal_operator=True)
    assert assessed["website"]["value"]["identity_assessment"]["publishable"]


def test_retail_directory_listing_does_not_publish_the_host_or_its_social_profile():
    from norway_company_agent.batch import build_contract_claims_and_evidence

    site = "https://www.interflora.no/om-oss/finn-butikk/blomsterbutikken-as"
    profile = {
        "organisation_number": "931654004", "name": "BLOMSTERBUTIKKEN AS", "website": site,
        "evidence": {"website": {
            "status": "available", "source_url": site, "content_sha256": "a" * 64,
            "value": {
                "final_url": site, "title": "Blomsterbutikken AS | Interflora",
                "identity_text_excerpt": "© 2026 Interflora Norge SA",
                "structured_organisations": [{"name": "Interflora"}],
                "social_links": [{"platform": "instagram", "url": "https://instagram.com/interflora_norge"}],
            },
        }},
    }
    claims, _ = build_contract_claims_and_evidence(profile, "2026-11-13T12:00:00Z", first_party_activity_only=True)
    by_field = {claim["field"]: claim for claim in claims}
    assert by_field["official_website"]["availability"] == "ambiguous"
    assert by_field["social_profile"]["availability"] == "not_available"


@pytest.mark.parametrize("status,allowed", [(404, True), (410, True), (403, False), (429, False), (503, False)])
def test_site_robots_access_errors(status, allowed):
    from norway_company_agent import website

    page = "https://example.no/careers"
    error = urllib.error.HTTPError("https://example.no/robots.txt", status, "", {}, None)
    with (
        patch.object(website, "assert_public_url"),
        patch.object(website, "_ROBOTS_CACHE", {}),
        patch.object(website.ROBOTS_OPENER, "open", side_effect=error),
    ):
        assert website._robots_allowed(page, 4) is allowed


def test_site_redirect_checks_destination_robots():
    from norway_company_agent import website

    request = urllib.request.Request("https://example.no/")
    destination = "https://www.example.no/careers"
    with (
        patch.object(website, "assert_public_url"),
        patch.object(website, "_robots_allowed", return_value=False) as check,
        pytest.raises(urllib.error.HTTPError, match="robots.txt disallows redirect target"),
    ):
        website.SafeRedirectHandler(check_robots=True).redirect_request(request, None, 302, "", {}, destination)
    check.assert_called_once_with(destination, 4.0)


def test_site_robots_honors_wildcard_and_more_specific_allow():
    from norway_company_agent import website

    robots = BytesIO(b"User-agent: *\nDisallow: /jobs/*\nAllow: /jobs/public$\n")
    with (
        patch.object(website, "assert_public_url"),
        patch.object(website, "_ROBOTS_CACHE", {}),
        patch.object(website.ROBOTS_OPENER, "open", return_value=robots),
    ):
        assert not website._robots_allowed("https://example.no/jobs/private", 4)
        assert website._robots_allowed("https://example.no/jobs/public", 4)


def test_site_certificate_error_never_retries_over_http():
    from norway_company_agent import website

    error = urllib.error.URLError(ssl.SSLCertVerificationError("invalid certificate"))
    with (
        patch.object(website, "assert_public_url"),
        patch.object(website, "_robots_allowed", return_value=True),
        patch.object(website.SAFE_OPENER, "open", side_effect=error) as fetch,
    ):
        record, _ = website.fetch_website("example.no")
    assert record["status"] == "source_error"
    assert fetch.call_count == 1
