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

    @patch("urllib.request.urlopen")
    def test_fetch_nav_jobs_publishable(self, mock_urlopen):
        from norway_company_agent.external_footprint import publishable_observation, validate_observation

        mock_resp = MagicMock()
        mock_resp.read.return_value = b'{"hits": {"hits": [{"_source": {"uuid": "abc-123", "title": "Senior Developer", "businessName": "Equinor ASA", "employer": {"orgnr": "923609016", "name": "Equinor ASA"}}}]}}'
        mock_resp.__enter__.return_value = mock_resp
        mock_urlopen.return_value = mock_resp

        obs = fetch_nav_jobs("Equinor ASA", "923609016")
        self.assertEqual(len(obs), 1)
        self.assertEqual(obs[0]["platform"], "job_board")
        self.assertEqual(obs[0]["signal_type"], "job_posting")
        self.assertEqual(validate_observation(obs[0]), [])
        self.assertTrue(publishable_observation(obs[0]))

    @patch("urllib.request.urlopen")
    def test_fetch_linkedin_jobs_publishable(self, mock_urlopen):
        from norway_company_agent.external_footprint import publishable_observation, validate_observation

        html = b"""
        <ul>
            <li>
                <div class="base-search-card">
                    <h4 class="base-search-card__subtitle">Equinor ASA</h4>
                    <h3 class="base-search-card__title">Subsea Engineer</h3>
                    <a class="base-card__full-link" href="https://no.linkedin.com/jobs/view/subsea-engineer-123"></a>
                    <time datetime="2026-09-20">2 weeks ago</time>
                </div>
            </li>
        </ul>
        """
        mock_resp = MagicMock()
        mock_resp.read.return_value = html
        mock_resp.__enter__.return_value = mock_resp
        mock_urlopen.return_value = mock_resp

        obs = fetch_linkedin_jobs("Equinor ASA", "923609016")
        self.assertEqual(len(obs), 1)
        self.assertEqual(obs[0]["platform"], "linkedin")
        self.assertEqual(obs[0]["signal_type"], "job_posting")
        self.assertEqual(obs[0]["acquisition_mode"], "permitted_public_page")
        self.assertEqual(validate_observation(obs[0]), [])
        self.assertTrue(publishable_observation(obs[0]))


if __name__ == "__main__":
    unittest.main()
