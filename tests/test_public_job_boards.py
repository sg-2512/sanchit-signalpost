"""Exercise linked public boards through acquisition and final claim validation."""
import hashlib
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest


def test_inline_newsroom_requires_dated_entry_and_matching_source():
    from bs4 import BeautifulSoup
    from norway_company_agent.website import extract_website_news
    from norway_company_agent.verified_items import validate_news_item

    url = "https://example.no/Nyheter.html"
    html = "<table><tr><td><strong>15. januar 2026: New mounting rail launched</strong> Example AS has launched a mounting rail for wall boxes this month.</td></tr></table>"
    items = extract_website_news(url, BeautifulSoup(html, "html.parser"), html)
    assert len(items) == 1
    item = items[0]
    args = {"company_name": "Example AS", "site_url": "https://example.no", "today": datetime(2026, 11, 14).date()}
    assert not validate_news_item(item, **args)[0]
    assert validate_news_item({**item, "name_in_article": True}, **args)[0]
    assert not validate_news_item({**item, "name_in_article": True, "source_url": "https://other.no/news"}, **args)[0]


@pytest.mark.parametrize("board", ["https://example.teamtailor.com", "https://job-boards.eu.greenhouse.io/example"])
def test_linked_html_board_checks_individual_ad_and_exact_employer(board):
    ad = board + "/jobs/123"
    today = datetime.now(timezone.utc).date()
    other = "https://job-boards.eu.greenhouse.io/other/jobs/987"
    for employer in ["Example AS", "Example UK Ltd"]:
        payload = json.dumps({
            "@type": "JobPosting", "title": "Senior Software Engineer",
            "datePosted": today.isoformat(), "validThrough": (today + timedelta(days=7)).isoformat(),
            "hiringOrganization": {"name": employer},
        })
        def read(url, *_):
            if url.endswith("/robots.txt"):
                return b"User-agent: *\nAllow: /"
            if url == board:
                return f'<a href="{other}">Other tenant</a><a href="{ad}">Vacancy</a>'.encode()
            assert url == ad
            return f'<script type="application/ld+json">{payload}</script>'.encode()
        with patch("norway_company_agent.connectors.ats_discovery._read_job_source", side_effect=read):
            jobs = verified_career_jobs(
                [{"url": board, "source_url": "https://example.no/careers"}], site_url="https://example.no",
                company_name="EXAMPLE AS", org="923609016", subunit_orgs=(),
                budget=BudgetTracker(max_requests=20), max_requests=6,
            )
        assert len(jobs) == (1 if employer == "Example AS" else 0)
        for job in jobs:
            assert validate_job_item(job, org="923609016", company_name="EXAMPLE AS", site_url="https://example.no", site_publishable=True)[0]

from norway_company_agent.budget import BudgetTracker
from norway_company_agent.connectors.job_boards import board_endpoint, listed_adverts
from norway_company_agent.connectors.ats_discovery import verified_career_jobs
from norway_company_agent.verified_items import validate_job_item
from norway_company_agent.batch import terminal_envelope


@pytest.mark.parametrize("board,field", [
    ("https://jobs.lever.co/example", "hostedUrl"),
    ("https://jobs.eu.lever.co/example", "hostedUrl"),
    ("https://job-boards.greenhouse.io/example", "absolute_url"),
    ("https://jobs.ashbyhq.com/example", "jobUrl"),
])
def test_linked_board_advert_without_deadline_keeps_listing_evidence(board, field):
    ad = board + "/jobs/123456"
    rows = [{field: ad, "location": "Oslo, Norway"}, {field: board.replace("example", "other") + "/123456"}]
    response = json.dumps(rows if field == "hostedUrl" else {"jobs": rows}).encode()
    page = json.dumps({"@type": "JobPosting", "title": "Senior Software Engineer", "hiringOrganization": {"name": "Example AS"}})
    page_raw = f'<script type="application/ld+json">{page}</script>'.encode()
    def read(url, *_):
        if url.endswith("/robots.txt"):
            return b"User-agent: *\nAllow: /"
        if url == board_endpoint(board):
            return response
        assert url == ad
        return page_raw
    with patch("norway_company_agent.connectors.ats_discovery._read_job_source", side_effect=read):
        jobs = verified_career_jobs(
            [{"url": board, "source_url": "https://example.no/careers"}], site_url="https://example.no",
            company_name="EXAMPLE AS", org="923609016", subunit_orgs=(),
            budget=BudgetTracker(max_requests=20), max_requests=6,
        )
    assert len(jobs) == 1
    assert jobs[0]["published_at"] is None  # Never invent the publication date.
    kwargs = dict(org="923609016", company_name="EXAMPLE AS", site_url="https://example.no", site_publishable=True)
    assert validate_job_item(jobs[0], **kwargs)[0]
    stale = {**jobs[0], "active_listing": {**jobs[0]["active_listing"], "retrieved_at": "2020-01-01"}}
    assert not validate_job_item(stale, **kwargs)[0]
    wrong = {**jobs[0], "employer_name": "Example UK Ltd"}
    assert not validate_job_item(wrong, **kwargs)[0]
    expired = {**jobs[0], "valid_through": (datetime.now(timezone.utc).date() - timedelta(days=1)).isoformat()}
    assert not validate_job_item(expired, **kwargs)[0]
    profile = {"organisation_number": "923609016", "name": "EXAMPLE AS", "website": "https://example.no", "evidence": {
        "website": {"status": "available", "source_url": "https://example.no", "content_sha256": "a" * 64,
                    "value": {"final_url": "https://example.no", "title": "Example AS",
                              "identity_text_excerpt": "Example AS, org.nr 923609016",
                              "identity_assessment": {"publishable": True}}},
        "jobs": {"status": "available", "source_url": ad, "value": {"items": jobs}},
    }}
    stamp = datetime.now(timezone.utc).isoformat()
    envelope = terminal_envelope(profile, run_id="test", modules=["website"], started_at=stamp, completed_at=stamp, first_party_activity_only=True)
    claim = next(c for c in envelope["claims"] if c["field"] == "job_postings")
    assert claim["availability"] == "available"
    assert any(e["id"] in claim["evidence_ids"] and e["source_url"] == board_endpoint(board)
               and e["content_sha256"] == hashlib.sha256(response).hexdigest() for e in envelope["evidence"])


def test_board_discovery_rejects_hidden_wrong_tenant_and_unsafe_urls():
    board = "https://jobs.ashbyhq.com/example"
    rows = [
        {"jobUrl": board + "/one", "isListed": False},
        {"jobUrl": "https://jobs.ashbyhq.com/other/two"},
        {"jobUrl": "https://jobs.ashbyhq.com:bad/example/three"},
        {"jobUrl": "https://jobs.ashbyhq.com.evil.test/example/four"},
    ]
    assert listed_adverts(board, lambda _: json.dumps({"jobs": rows}).encode()) == {}
    assert board_endpoint("https://user:secret@jobs.lever.co/example") is None
    assert listed_adverts(board, lambda _: b'{"jobs": null}') == {}


def test_greenhouse_host_migration_preserves_same_board_only():
    data = {"jobs": [{"absolute_url": "https://job-boards.greenhouse.io/example/jobs/12345"}]}
    assert listed_adverts("https://boards.greenhouse.io/example", lambda _: json.dumps(data).encode())


def test_contact_page_can_prove_operator_without_matching_an_article_mention():
    from bs4 import BeautifulSoup
    from norway_company_agent.website import _site_identity_text
    from norway_company_agent.identity import apply_website_identity_gate
    text = _site_identity_text(BeautifulSoup('<p>Example AS. Org.nr. 923 609 016</p>', "html.parser"))
    value = {"final_url": "https://example.no/en", "title": "Example", "pages": [{"identity_text_excerpt": text}]}
    profile = {"organisation_number": "923609016", "name": "EXAMPLE AS"}
    assert apply_website_identity_gate(profile, {"status": "available", "value": value}, require_legal_operator=True)["assessment"]["publishable"]
    value["pages"][0]["identity_text_excerpt"] = _site_identity_text(BeautifulSoup('<article>A news story mentions Example AS</article>', "html.parser"))
    assert not apply_website_identity_gate(profile, {"status": "available", "value": value}, require_legal_operator=True)["assessment"]["publishable"]


def test_website_transport_stops_before_exceeding_request_budget():
    import urllib.error
    from norway_company_agent import website
    budget = BudgetTracker(max_requests=2, request_reserve=0)
    context = {"budget": budget, "requests": 0}
    token = website._REQUEST_CONTEXT.set(context)
    try:
        handler = website.WebsiteBudgetHandler()
        handler.http_request(object())  # robots
        handler.https_request(object())  # page
        with pytest.raises(urllib.error.URLError, match="budget exhausted"):
            handler.https_request(object())  # redirect cannot start
        assert context["requests"] == budget.requests == 2
    finally:
        website._REQUEST_CONTEXT.reset(token)


def test_linked_youtube_handle_uses_exact_lookup_and_no_search(monkeypatch):
    from io import BytesIO
    from urllib.parse import parse_qs, urlsplit
    from norway_company_agent.connectors.youtube import fetch_linked_youtube_data
    monkeypatch.setenv("YOUTUBE_API_KEY", "test-key")
    channel_id = "UC" + "a" * 22
    raw = json.dumps({"items": [{"id": channel_id, "snippet": {"title": "Example"}, "statistics": {"viewCount": "120", "hiddenSubscriberCount": True}}]}).encode()
    budget = BudgetTracker(max_requests=2, request_reserve=0)
    with patch("urllib.request.urlopen", return_value=BytesIO(raw)) as read:
        observations = fetch_linked_youtube_data("923609016", [{"platform": "youtube", "url": "https://youtube.com/@example"}], site_url="https://example.no", budget=budget)
    query = parse_qs(urlsplit(read.call_args.args[0].full_url).query)
    assert query["forHandle"] == ["@example"]
    assert "q" not in query
    assert len(observations) == 1 and budget.requests == 1
    assert "subscriber_count" not in observations[0]["metrics"]
    with patch("urllib.request.urlopen") as read:
        assert fetch_linked_youtube_data("923609016", [{"platform": "youtube", "url": "https://youtube.com/c/uncertain"}], site_url="https://example.no", budget=budget) == []
    read.assert_not_called()


def test_heuristic_discovery_honors_robots_and_rejects_private_destinations():
    from norway_company_agent import discovery
    with patch.object(discovery, "assert_public_url"), patch.object(discovery, "_robots_allowed", return_value=False), patch.object(discovery, "_safe_urlopen") as fetch:
        assert discovery._read_candidate_page("https://example.no", None, 1) is None
        fetch.assert_not_called()
    with patch.object(discovery, "assert_public_url", side_effect=ValueError("private destination")), patch.object(discovery, "_safe_urlopen") as fetch:
        assert discovery._read_candidate_page("https://127.0.0.1", None, 1) is None
        fetch.assert_not_called()


def test_places_quota_failure_is_counted_and_reported_without_credentials(monkeypatch):
    from io import BytesIO
    from urllib.error import HTTPError
    from norway_company_agent.connectors import google_places
    monkeypatch.setenv("GOOGLE_PLACES_API_KEY", "test-secret")
    monkeypatch.setattr(google_places, "_QUOTA_EXHAUSTED", False)
    monkeypatch.setattr(google_places, "_CIRCUIT_REASON", "")
    budget = BudgetTracker(max_requests=3, request_reserve=0)
    error = HTTPError("https://places.googleapis.com/v1/places:searchText", 429, "Quota exceeded", {}, BytesIO(b'{"error":{"status":"RESOURCE_EXHAUSTED"}}'))
    with patch("urllib.request.urlopen", side_effect=error):
        assert google_places.fetch_place_data("923609016", "Example AS", budget=budget) == []
    assert budget.requests == 1
    status = google_places.connector_status()
    assert status["disabled_reason"] == "http_429_quota_exhausted"
    assert "test-secret" not in json.dumps(status)
