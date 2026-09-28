import unittest
from unittest.mock import patch, MagicMock

from norway_company_agent.connectors.nav_jobs import normalize_core, fetch_nav_jobs
from norway_company_agent.connectors.linkedin_jobs import fetch_linkedin_jobs
from norway_company_agent.hiring import (
    extract_website_hiring_signals,
    evaluate_company_hiring,
)


class TestHiringModules(unittest.TestCase):
    def test_normalize_core(self):
        self.assertEqual(normalize_core("Equinor ASA"), "equinor")
        self.assertEqual(normalize_core("ARKITEKTFIRMA JON VIKØREN AS"), "arkitektfirma jon vikoren")
        self.assertEqual(normalize_core("FJELLGLØD HOLDING AS"), "fjellglod")

    def test_extract_website_hiring_signals_empty(self):
        signals = extract_website_hiring_signals(None)
        self.assertFalse(signals["has_career_page"])
        self.assertEqual(signals["career_urls"], [])

    def test_extract_website_hiring_signals_detected(self):
        web_val = {
            "description": "Vi søker dyktige utviklere til vårt team!",
            "pages": [
                {
                    "url": "https://example.no/karriere",
                    "title": "Karriere hos oss",
                    "main_text_excerpt": "Ledige stillinger hos oss. Vi søker senior ingeniør.",
                }
            ],
        }
        signals = extract_website_hiring_signals(web_val)
        self.assertTrue(signals["has_career_page"])
        self.assertIn("https://example.no/karriere", signals["career_urls"])
        self.assertTrue(len(signals["hiring_keywords_found"]) > 0)

    def test_evaluate_company_hiring_with_employees(self):
        profile = {
            "name": "NORDIC TECH AS",
            "organisation_number": "912345678",
            "employees": 12,
            "evidence": {
                "website": {"status": "available", "value": {"pages": []}}
            },
        }
        hiring_block, obs = evaluate_company_hiring(profile)
        self.assertEqual(hiring_block["registered_employees"], 12)
        self.assertIn("12 employee(s)", hiring_block["assessment"])
        self.assertEqual(hiring_block["status"], "workforce_active_no_open_postings")

    def test_evaluate_company_hiring_with_zero_employees(self):
        profile = {
            "name": "SHELL CORP AS",
            "organisation_number": "987654321",
            "employees": 0,
            "evidence": {},
        }
        hiring_block, obs = evaluate_company_hiring(profile)
        self.assertFalse(hiring_block["appears_to_be_hiring"])
        self.assertIn("0 registered employees", hiring_block["assessment"])


if __name__ == "__main__":
    unittest.main()
