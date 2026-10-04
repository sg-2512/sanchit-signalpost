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
        self.assertEqual(notice["platform"], "news")
        self.assertEqual(notice["signal_type"], "public_mention")
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
    # 4. Norwegian Transliteration in Domain Discovery
    # ------------------------------------------------------------------------
    def test_norwegian_email_transliteration(self) -> None:
        profile_ae = {
            "evidence": {
                "registry_live": {
                    "value": {"epostadresse": "post@blåbær.no"}
                }
            }
        }
        url_ae = extract_email_domain_candidate(profile_ae)
        self.assertEqual(url_ae, "https://www.blabaer.no")

        profile_oe = {
            "evidence": {
                "registry": {
                    "value": {"epostadresse": "kontakt@møbeldesign.no"}
                }
            }
        }
        url_oe = extract_email_domain_candidate(profile_oe)
        self.assertEqual(url_oe, "https://www.mobeldesign.no")

        # Test generic personal emails are rejected
        profile_gmail = {
            "evidence": {
                "registry": {
                    "value": {"epostadresse": "ceo@gmail.com"}
                }
            }
        }
        self.assertIsNone(extract_email_domain_candidate(profile_gmail))


if __name__ == "__main__":
    unittest.main()
