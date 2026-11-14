from __future__ import annotations

from unittest.mock import patch

from norway_company_agent.connectors.ats_discovery import fetch_ats_jobs
from norway_company_agent.verified_items import validate_job_item


def test_experimental_linked_teamtailor_page_yields_individual_candidate() -> None:
    landing = "https://equinor.teamtailor.com/jobs"
    html = b"""
        <html><body>
          <h1>Equinor careers</h1>
          <a href=\"/jobs/123456-software-engineer\">Software Engineer</a>
        </body></html>
    """
    with patch("norway_company_agent.connectors.ats_discovery._fetch", return_value=(html, len(html))):
        jobs = fetch_ats_jobs(
            "EQUINOR ASA",
            "923609016",
            seed_urls=[landing],
            linked_from="https://www.equinor.com/careers",
            probe_patterns=False,
        )

    assert len(jobs) == 1
    job = jobs[0]
    assert job["source_class"] == "commercial_job_board"
    assert job["strategy"] == "ats_discovery_probe"
    assert any(proof["type"] == "company_site_career_link" for proof in job["identity_proof"])

    ok, reason = validate_job_item(
        {
            "title": job["job_title"],
            "url": job["source_url"],
            "content_sha256": job["content_sha256"],
            "kind": job["kind"],
        },
        org="923609016",
        company_name="EQUINOR ASA",
        site_url="https://www.equinor.com",
        site_publishable=True,
    )
    assert ok, reason


def test_company_linked_ats_listing_never_publishes_without_an_individual_ad() -> None:
    landing = "https://equinor.teamtailor.com/jobs"
    html = b"<html><body><h1>Equinor careers</h1><a href='/en-US/Global'>Open positions</a></body></html>"
    with patch("norway_company_agent.connectors.ats_discovery._fetch", return_value=(html, len(html))):
        jobs = fetch_ats_jobs(
            "EQUINOR ASA",
            "923609016",
            seed_urls=[landing],
            linked_from="https://www.equinor.com/careers",
            probe_patterns=False,
        )
    assert jobs == []
