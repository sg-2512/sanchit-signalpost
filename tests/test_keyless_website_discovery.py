from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

from norway_company_agent import discovery
from norway_company_agent.identity import apply_entity_identity_gate, assess_entity_identity
from norway_company_agent.discovery import candidate_site_origin
from norway_company_agent.website import _priority_links, _site_identity_text
from norway_company_agent.verified_items import name_in_text
from bs4 import BeautifulSoup


def test_search_result_company_listing_is_crawled_at_operator_root() -> None:
    assert candidate_site_origin(
        "https://directory.example/selskaper/target-as-123456789"
    ) == "https://directory.example/"


def test_registry_profile_is_not_a_company_website_candidate() -> None:
    from norway_company_agent.discovery import score_search_candidate

    result = score_search_candidate(
        {"organisation_number": "123456789", "name": "TARGET AS"},
        {"url": "https://virksomhet.brreg.no/nb/oppslag/enheter/123456789",
         "title": "TARGET AS 123456789", "snippet": "TARGET AS"},
    )
    assert not result["publishable_candidate"]


def test_contract_builder_cannot_promote_rejected_directory_page() -> None:
    from norway_company_agent.batch import build_contract_claims_and_evidence

    profile = {
        "organisation_number": "123456789", "name": "TARGET AS", "website": None,
        "evidence": {"website": {
            "status": "available", "source_url": "https://directory.example/selskaper/target-123456789",
            "content_sha256": "a" * 64,
            "value": {"final_url": "https://directory.example/selskaper/target-123456789",
                      "title": "Target AS 123456789", "identity_text_excerpt": "Target AS org.nr 123456789",
                      "identity_assessment": {"publishable": False}},
        }},
    }
    claims, _ = build_contract_claims_and_evidence(profile, "2026-11-14T00:00:00Z")
    website = next(claim for claim in claims if claim["field"] == "official_website")
    assert website["availability"] != "available"


class _Budget:
    def __init__(self) -> None:
        self.requests = 0
        self.bytes = 0

    def try_start_request(self) -> bool:
        self.requests += 1
        return True

    def record_request(self, *, count: int = 1, bytes_received: int = 0) -> None:
        self.bytes += bytes_received


def test_search_result_with_exact_org_and_name_can_nominate_branded_domain() -> None:
    profile = {"name": "NORDLYS TEKNOLOGI AS", "organisation_number": "923456789"}
    result = {
        "url": "https://aurora.example/about",
        "title": "Nordlys Teknologi AS",
        "snippet": "Organisation number 923 456 789",
        "rank": 1,
    }
    assert discovery.score_search_candidate(profile, result)["publishable_candidate"] is True


def test_exact_search_identity_still_rejects_directory_hosts() -> None:
    profile = {"name": "NORDLYS TEKNOLOGI AS", "organisation_number": "923456789"}
    result = {
        "url": "https://proff.no/selskap/nordlys-teknologi-as/923456789",
        "title": "Nordlys Teknologi AS",
        "snippet": "923456789",
        "rank": 1,
    }
    assert discovery.score_search_candidate(profile, result)["publishable_candidate"] is False


def test_keyless_search_returns_candidates_but_not_claims() -> None:
    payload = {
        "results": [{
            "url": "https://nordlysteknologi.no/kontakt",
            "title": "Nordlys Teknologi AS",
            "content": "Org. nr. 923 456 789",
        }]
    }
    response = MagicMock()
    response.read.return_value = json.dumps(payload).encode("utf-8")
    response.__enter__.return_value = response
    budget = _Budget()
    with (
        patch.object(discovery, "_KEYLESS_SEARCH_REQUESTS", 0),
        patch.object(discovery, "_KEYLESS_SEARCH_DISABLED", False),
        patch("urllib.request.urlopen", return_value=response) as request,
    ):
        candidates = discovery.discover_company_websites_keyless(
            {"name": "NORDLYS TEKNOLOGI AS", "organisation_number": "923456789"},
            budget=budget,
        )
    assert candidates == ["https://nordlysteknologi.no/kontakt"]
    assert budget.requests == 1
    assert budget.bytes > 0
    sent = request.call_args.args[0]
    assert sent.headers["X-tavily-access-mode"] == "keyless"


def test_norwegian_domain_variants_include_idn_and_legal_suffix() -> None:
    candidates = discovery._domain_slugs("RØR & VÅTROM AS")
    assert "rorvatrom" in candidates
    assert any(candidate.startswith("xn--") for candidate in candidates)
    assert "rorvatromas" in candidates


def test_short_prefix_and_full_hyphenated_dot_com_are_probed(monkeypatch) -> None:
    monkeypatch.setattr("socket.getaddrinfo", lambda *args, **kwargs: [(2, 1, 6, "", ("93.184.216.34", 443))])
    result = discovery.probe_heuristic_domain(
        {"name": "A-SEAFOOD AS", "organisation_number": "933989208"},
        accept_candidate=lambda url: url == "https://a-seafood.com",
        max_candidates=4,
    )
    assert result == "https://a-seafood.com"


def test_norwegian_mva_footer_is_exact_legal_operator_proof() -> None:
    profile = {"name": "NOVOOI AS", "organisation_number": "827340162", "evidence": {}}
    website = {
        "status": "available",
        "source_url": "https://novooi.com/",
        "value": {
            "final_url": "https://novooi.com/",
            "title": "NOVOOI – Norwegian Design, Interior and Craft",
            "main_text_excerpt": "NOVOOI formidler norsk design. © NOVOOI MVA 827 340 162",
            "identity_text_excerpt": "© NOVOOI MVA 827 340 162",
            "structured_organisations": [],
            "pages": [],
        },
    }
    assessed = apply_entity_identity_gate(profile, website)
    assert assessed["website"]["value"]["identity_assessment"]["publishable"] is True
    assert assessed["entity_assessment"]["found_org_numbers"] == ["827340162"]


def test_about_and_contact_get_legal_proof_slots_before_news_and_careers() -> None:
    soup = BeautifulSoup(
        '<a href="/news">News</a><a href="/careers">Careers</a>'
        '<a href="/Omoss.html">Om oss</a><a href="/kontakt">Kontakt</a>',
        "html.parser",
    )
    assert _priority_links("https://example.no/", soup, limit=4) == [
        "https://example.no/kontakt",
        "https://example.no/Omoss.html",
        "https://example.no/careers",
        "https://example.no/news",
    ]


def test_legal_number_in_div_with_strong_label_is_retained() -> None:
    soup = BeautifulSoup(
        "<div><strong>Foretaksnr.:</strong>&nbsp;923 609 016</div>",
        "html.parser",
    )
    assert "Foretaksnr.: 923 609 016" in _site_identity_text(soup)


def test_short_brand_needs_its_exact_number_in_same_operator_block() -> None:
    profile = {
        "name": "ACME AS", "organisation_number": "923609016",
        "evidence": {"website": {"status": "available", "value": {
            "final_url": "https://acme.no/",
            "title": "ACME",
            "main_text_excerpt": "Download software ACME AS",
            "identity_text_excerpt": "ACME AS. Org nr 923 609 016",
            "pages": [],
        }}},
    }
    assert assess_entity_identity(profile)["publishable"]
    profile["evidence"]["website"]["value"]["identity_text_excerpt"] = "ACME AS. Org nr 987 654 321"
    assert not assess_entity_identity(profile)["publishable"]


def test_short_brand_article_match_is_opt_in_for_verified_company_sites() -> None:
    article = "The new integration from ACME sends orders to accounting automatically."
    assert not name_in_text("ACME AS", article)
    assert name_in_text("ACME AS", article, allow_short_single_token=True)

