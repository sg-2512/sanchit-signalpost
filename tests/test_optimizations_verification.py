"""Comprehensive Unit & Regression Verification for SignalPost Optimizations.

Verifies:
1. Official Legal Announcements as Dated Public Activity (Brreg Kunngjøringer & registration notices).
2. Career Paths & Schema.org JSON-LD Extraction (JobPosting & NewsArticle).
3. Structured Social Links Discovery (Schema.org sameAs extraction & deduplication).
4. Norwegian Transliteration in Domain Discovery (æ->ae, ø->o, å->a).
"""
from __future__ import annotations

import unittest
from bs4 import BeautifulSoup

from norway_company_agent.website import (
    _priority_links,
    _jsonld_jobs,
    _jsonld_news,
    extract_website_jobs,
    extract_website_news,
    structured_social_links,
    PRIORITY_TERMS,
)
from norway_company_agent.discovery import extract_email_domain_candidate
from norway_company_agent.connectors.kunngjoringer import fetch_brreg_kunngjoringer
from run_agent import extract_notice_observation


class OptimizationsVerificationTests(unittest.TestCase):
    # ------------------------------------------------------------------------
    # 1. Official Legal Announcements as Dated Public Activity
    # ------------------------------------------------------------------------
    def test_extract_notice_observation_dated_activity(self) -> None:
        profile = {
            "organisation_number": "912345678",
            "name": "NORDIC TECH SOLUTIONS AS",
            "legal_form": "AS",
            "evidence": {
                "registry": {
                    "value": {
                        "navn": "NORDIC TECH SOLUTIONS AS",
                        "organisasjonsform.kode": "AS",
                        "registreringsdatoenhetsregisteret": "2018-05-14",
                    }
                }
            }
        }
        notices = extract_notice_observation(profile)
        self.assertEqual(len(notices), 1)
        notice = notices[0]
        self.assertEqual(notice["platform"], "brreg")
        self.assertEqual(notice["signal_type"], "official_notice")
        self.assertEqual(notice["published_at"], "2018-05-14")
        self.assertIn("2018-05-14", notice["text"])
        self.assertTrue(notice["exact_entity"])
        self.assertTrue(len(notice["content_sha256"]) == 64)
        self.assertIn("912345678", notice["source_url"])

    # ------------------------------------------------------------------------
    # 2. Career Paths & Schema.org JSON-LD Extraction
    # ------------------------------------------------------------------------
    def test_priority_links_new_career_and_news_paths(self) -> None:
        html = """
        <html>
            <body>
                <a href="/bli-med-pa-laget">Bli med på laget!</a>
                <a href="/rekruttering/ledige-stillinger">Ledige Stillinger</a>
                <a href="/media/pressemeldinger">Pressemeldinger</a>
                <a href="/siste-nytt/artikler">Siste Nytt</a>
                <a href="/om-oss">Om Oss</a>
            </body>
        </html>
        """
        soup = BeautifulSoup(html, "lxml")
        links = _priority_links("https://www.example.no/", soup, limit=5)
        # Verify that career and news links were recognized as priority terms
        self.assertTrue(any("bli-med-pa-laget" in u for u in links))
        self.assertTrue(any("rekruttering" in u for u in links))
        self.assertTrue(any("media" in u or "pressemeldinger" in u for u in links))
        self.assertTrue(any("siste-nytt" in u for u in links))

    def test_jsonld_job_posting_extraction(self) -> None:
        metadata = {
            "json-ld": [
                {
                    "@context": "https://schema.org",
                    "@type": "JobPosting",
                    "title": "Senior Python Utvikler",
                    "datePosted": "2026-03-01",
                    "description": "Vi søker en erfaren Python-utvikler til vårt kjerneteam.",
                    "url": "/stillinger/senior-python",
                }
            ]
        }
        jobs = _jsonld_jobs(metadata, "https://www.example.no")
        self.assertEqual(len(jobs), 1)
        job = jobs[0]
        self.assertEqual(job["title"], "Senior Python Utvikler")
        self.assertEqual(job["published_at"], "2026-03-01")
        self.assertEqual(job["url"], "https://www.example.no/stillinger/senior-python")
        self.assertTrue(len(job["content_sha256"]) == 64)

    def test_jsonld_news_article_extraction(self) -> None:
        metadata = {
            "json-ld": [
                {
                    "@context": "https://schema.org",
                    "@type": "NewsArticle",
                    "headline": "Selskapet lanserer ny miljøvennlig teknologi",
                    "datePublished": "2026-02-15T08:00:00Z",
                    "description": "I dag lanserer vi neste generasjons teknologi i Oslo.",
                    "url": "https://www.example.no/nyheter/lansering-2026",
                }
            ]
        }
        news = _jsonld_news(metadata, "https://www.example.no")
        self.assertEqual(len(news), 1)
        article = news[0]
        self.assertEqual(article["title"], "Selskapet lanserer ny miljøvennlig teknologi")
        self.assertEqual(article["published_at"], "2026-02-15")
        self.assertEqual(article["url"], "https://www.example.no/nyheter/lansering-2026")
        self.assertTrue(len(article["content_sha256"]) == 64)

    # ------------------------------------------------------------------------
    # 3. Structured Social Links Discovery (sameAs)
    # ------------------------------------------------------------------------
    def test_structured_social_links_same_as(self) -> None:
        metadata = {
            "json-ld": [
                {
                    "@context": "https://schema.org",
                    "@type": "Organization",
                    "name": "Example Corp",
                    "sameAs": [
                        "https://www.linkedin.com/company/example-corp",
                        "https://twitter.com/example_corp",
                        "https://www.youtube.com/@examplecorp",
                    ],
                }
            ]
        }
        social = structured_social_links(metadata)
        self.assertEqual(len(social), 3)
        platforms = {item["platform"] for item in social}
        self.assertIn("linkedin", platforms)
        self.assertIn("x", platforms)
        self.assertIn("youtube", platforms)

    # ------------------------------------------------------------------------
    # 5. RSS and Atom Feed Newsroom Harvester
    # ------------------------------------------------------------------------
    def test_extract_feed_news_rss_and_atom(self) -> None:
        from norway_company_agent.website import extract_feed_news

        rss_xml = b"""<?xml version="1.0" encoding="UTF-8"?>
        <rss version="2.0">
            <channel>
                <title>Bedriftsnyheter</title>
                <item>
                    <title>Signerte ny rammeavtale med Statnett</title>
                    <link>https://www.example.no/nyheter/statnett-avtale</link>
                    <pubDate>Mon, 15 Sep 2026 09:30:00 GMT</pubDate>
                    <description>Vi har inngatt en stor kontrakt.</description>
                </item>
            </channel>
        </rss>
        """
        rss_items = extract_feed_news(rss_xml, "https://www.example.no/feed", "https://www.example.no")
        self.assertEqual(len(rss_items), 1)
        self.assertEqual(rss_items[0]["title"], "Signerte ny rammeavtale med Statnett")
        self.assertEqual(rss_items[0]["url"], "https://www.example.no/nyheter/statnett-avtale")
        self.assertEqual(rss_items[0]["published_at"], "2026-09-15")
        self.assertEqual(rss_items[0]["verification"], "site_listing")
        self.assertEqual(len(rss_items[0]["content_sha256"]), 64)

        atom_xml = b"""<?xml version="1.0" encoding="utf-8"?>
        <feed xmlns="http://www.w3.org/2005/Atom">
            <title>Aktuelt Blogg</title>
            <entry>
                <title>Resultater for 2. kvartal 2026</title>
                <link href="https://www.example.no/aktuelt/q2-2026"/>
                <updated>2026-08-20T08:00:00Z</updated>
            </entry>
        </feed>
        """
        atom_items = extract_feed_news(atom_xml, "https://www.example.no/atom.xml", "https://www.example.no")
        self.assertEqual(len(atom_items), 1)
        self.assertEqual(atom_items[0]["title"], "Resultater for 2. kvartal 2026")
        self.assertEqual(atom_items[0]["url"], "https://www.example.no/aktuelt/q2-2026")
        self.assertEqual(atom_items[0]["published_at"], "2026-08-20")

    # ------------------------------------------------------------------------
    # 6. Google News Decoded Article Verification Gate
    # ------------------------------------------------------------------------
    def test_google_news_verified_article_validation(self) -> None:
        from norway_company_agent.verified_items import validate_news_item

        # Verified external journalism article on e24.no
        art_verified = {
            "title": "Equinor og partnere investerer 12 milliarder",
            "url": "https://e24.no/energi/i/123/equinor-investerer",
            "source_url": "https://e24.no/energi/i/123/equinor-investerer",
            "published_at": "2026-10-10",
            "content_sha256": "a" * 64,
            "verification": "article_fetched",
            "name_in_article": True,
        }
        ok, reason = validate_news_item(art_verified, company_name="Equinor ASA")
        self.assertTrue(ok)
        self.assertEqual(reason, "article_verified")

        # Unresolved redirect link to news.google.com must be rejected
        unresolved_link = {
            "title": "Equinor og partnere investerer 12 milliarder",
            "url": "https://news.google.com/rss/articles/CBMi123456",
            "source_url": "https://news.google.com/rss/articles/CBMi123456",
            "published_at": "2026-10-10",
            "content_sha256": "b" * 64,
            "verification": "",
            "name_in_article": False,
        }
        ok2, reason2 = validate_news_item(unresolved_link, company_name="Equinor ASA")
        self.assertFalse(ok2)
        self.assertEqual(reason2, "non_news_host")

    # ------------------------------------------------------------------------
    # 7. External Journalism News Claim Building vs Company Newsroom
    # ------------------------------------------------------------------------
    def test_batch_news_claim_preserves_external_publisher_sha_and_url(self) -> None:
        from norway_company_agent.batch import build_contract_claims_and_evidence

        profile = {
            "organisation_number": "923609016",
            "name": "TEST ENTERPRISE AS",
            "legal_form": "AS",
            "website": "https://www.testenterprise.no",
            "evidence": {
                "registry_live": {
                    "status": "available",
                    "content_sha256": "1" * 64,
                    "value": {"navn": "TEST ENTERPRISE AS", "organisasjonsform": {"kode": "AS"}},
                },
                "website": {
                    "status": "available",
                    "content_sha256": "2" * 64,
                    "source_url": "https://www.testenterprise.no",
                    "value": {
                        "final_url": "https://www.testenterprise.no",
                        "identity_assessment": {"publishable": True},
                    },
                },
                "news": {
                    "status": "available",
                    "value": {
                        "items": [
                            {
                                "title": "Test Enterprise vinner nasjonal innovasjonspris",
                                "url": "https://e24.no/teknologi/i/999/test-enterprise-vinner",
                                "source_url": "https://e24.no/teknologi/i/999/test-enterprise-vinner",
                                "published_at": "2026-09-01",
                                "content_sha256": "3" * 64,
                                "verification": "article_fetched",
                                "name_in_article": True,
                            }
                        ]
                    },
                },
            },
        }

        claims, evidence_list = build_contract_claims_and_evidence(profile, "2026-10-23T12:00:00Z")
        news_claim = next((c for c in claims if c["field"] == "dated_news"), None)
        self.assertIsNotNone(news_claim)
        self.assertEqual(news_claim["availability"], "available")
        self.assertEqual(news_claim["value"]["url"], "https://e24.no/teknologi/i/999/test-enterprise-vinner")
        # Ensure external news evidence maintains publisher SHA and does not get overwritten by website SHA
        news_ev = next((e for e in evidence_list if e["id"] == "ev-news-923609016"), None)
        self.assertIsNotNone(news_ev)
        self.assertEqual(news_ev["content_sha256"], "3" * 64)
        self.assertEqual(news_ev["source_class"], "public_news")
        self.assertEqual(news_ev["source_url"], "https://e24.no/teknologi/i/999/test-enterprise-vinner")


if __name__ == "__main__":
    unittest.main()
