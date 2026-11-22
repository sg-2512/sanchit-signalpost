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
        self.assertEqual(hiring_block["status"], "unknown")
        self.assertIsNone(hiring_block["appears_to_be_hiring"])

    def test_evaluate_company_hiring_with_zero_employees(self):
        profile = {
            "name": "SHELL CORP AS",
            "organisation_number": "987654321",
            "employees": 0,
            "evidence": {},
        }
        hiring_block, obs = evaluate_company_hiring(profile)
        self.assertIsNone(hiring_block["appears_to_be_hiring"])
        self.assertEqual(hiring_block["status"], "unknown")

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
    def test_fetch_nav_jobs_uses_one_query_and_keeps_exact_employer_gate(self, mock_urlopen):
        first = MagicMock()
        first.read.return_value = b'{"hits":{"hits":[{"_source":{"uuid":"job-456","title":"Senior Engineer","businessName":"FJELLGL\u00d8D HOLDING AS","employer":{"orgnr":"923609016","name":"FJELLGL\u00d8D HOLDING AS"}}}]}}'
        first.__enter__.return_value = first
        mock_urlopen.return_value = first
        obs = fetch_nav_jobs("FJORDLYS HOLDING AS", "923609016")
        self.assertEqual(len(obs), 1)
        self.assertEqual(mock_urlopen.call_count, 2)
        self.assertIn("q=FJORDLYS", mock_urlopen.call_args_list[0].args[0].full_url)

    @patch("urllib.request.urlopen")
    def test_fetch_nav_jobs_paginates_and_deduplicates_subunit_ads(self, mock_urlopen):
        first = MagicMock()
        first.read.return_value = b'{"hits":{"hits":[]}}'
        first.__enter__.return_value = first
        api = MagicMock()
        api.read.return_value = b'{"hits":{"hits":[{"_source":{"uuid":"job-1","title":"Engineer","businessName":"Nordic Works Bergen","employer":{"orgnr":"998877665","name":"Nordic Works Bergen"}}}]}}'
        api.__enter__.return_value = api
        mock_urlopen.side_effect = [first, api]
        obs = fetch_nav_jobs("Nordic Works AS", "912345678", subunit_orgs=["998877665"])
        self.assertEqual(len(obs), 1)
        self.assertEqual(obs[0]["match_type"], "nav_company_subunit_match")
        self.assertEqual(obs[0]["employer_org"], "998877665")

    @patch("urllib.request.urlopen")
    def test_nav_empty_result_exposes_non_claim_diagnostics(self, mock_urlopen):
        response = MagicMock()
        response.read.return_value = b'{"hits":{"hits":[]}}'
        response.__enter__.return_value = response
        mock_urlopen.return_value = response

        status = {}
        obs = fetch_nav_jobs("Nordic Works AS", "912345678", status_out=status)

        self.assertEqual(obs, [])
        self.assertEqual(status["status"], "available")
        self.assertEqual(status["matched_active_ads"], 0)
        self.assertIn("candidate_ads_seen", status)
        self.assertIn("query", status)

    def test_jsonld_job_uses_declared_employer_organisation_number(self):
        profile = {
            "organisation_number": "912345678",
            "name": "Nordic Works AS",
            "website": "https://nordic.no",
            "evidence": {"website": {"value": {
                "identity_assessment": {"publishable": True},
                "job_postings": [{
                    "title": "Engineer",
                    "url": "https://nordic.no/jobs/engineer",
                    "source_url": "https://nordic.no/jobs/engineer",
                    "content_sha256": "a" * 64,
                    "kind": "jsonld_job",
                    "employer_org": "912345678",
                    "hiring_org": "Different display name",
                }],
            }}},
        }
        hiring, _ = evaluate_company_hiring(profile)
        self.assertEqual(hiring["active_job_postings_count"], 1)

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
