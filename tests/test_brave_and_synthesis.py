"""Tests for Brave Search connector and Factual Synthesis generator."""

from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from norway_company_agent.connectors.brave_search import (
    discover_company_website,
    is_available as brave_available,
)
from norway_company_agent.synthesis import generate_company_synthesis


class BraveSearchConnectorTests(unittest.TestCase):
    def test_brave_availability_check(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(brave_available())
        with patch.dict(os.environ, {"BRAVE_API_KEY": "test_key"}):
            self.assertTrue(brave_available())
        with patch.dict(os.environ, {"BRAVE_SEARCH_API_KEY": "test_key_2"}):
            self.assertTrue(brave_available())

    def test_discover_company_website_no_key(self):
        with patch.dict(os.environ, {}, clear=True):
            profile = {"organisation_number": "993578843", "name": "TEST AS"}
            self.assertIsNone(discover_company_website(profile))

    def test_discover_company_website_success(self):
        profile = {"organisation_number": "923609016", "name": "Norsk Fiskeeksport AS", "municipality": "NOTODDEN"}
        mock_payload = {
            "web": {
                "results": [
                    {
                        "url": "https://norskfiskeeksport.no/",
                        "title": "Norsk Fiskeeksport AS",
                        "description": "Seafood exporter in Notodden 923609016",
                    }
                ]
            }
        }
        with patch.dict(os.environ, {"BRAVE_API_KEY": "mock_key"}):
            with patch("urllib.request.urlopen") as mock_urlopen:
                mock_resp = MagicMock()
                mock_resp.read.return_value = json.dumps(mock_payload).encode("utf-8")
                mock_resp.__enter__.return_value = mock_resp
                mock_urlopen.return_value = mock_resp

                candidate_url = discover_company_website(profile)
                self.assertEqual(candidate_url, "https://norskfiskeeksport.no/")


class CompanySynthesisTests(unittest.TestCase):
    def test_deterministic_grounded_synthesis(self):
        profile = {
            "name": "Acme Nordic AS",
            "organisation_number": "993578843",
            "legal_form": "AS",
            "municipality": "Oslo",
            "industry_label": "Software consultancy",
            "employees": 42,
            "evidence": {
                "financials": {
                    "value": {
                        "latest": {
                            "filing_year": 2024,
                            "revenue": 50000000,
                            "profit": 4500000,
                        }
                    }
                },
                "roles": {
                    "value": {
                        "roles": [
                            {"name": "Alice Hanson", "role_type": "Styreleder"},
                            {"name": "Bob Olsen", "role_type": "Daglig leder"},
                        ]
                    }
                },
                "website": {
                    "value": {
                        "final_url": "https://acmenordic.no",
                        "social_links": [{"platform": "linkedin", "url": "https://linkedin.com/company/acme"}],
                    }
                },
            },
        }
        synthesis = generate_company_synthesis(profile)
        self.assertTrue(synthesis["grounded"])
        self.assertIn("Acme Nordic AS", synthesis["summary"])
        self.assertIn("993578843", synthesis["summary"])
        self.assertIn("Bob Olsen", synthesis["summary"])
        self.assertIn("Alice Hanson", synthesis["summary"])
        self.assertIn("50,000,000 NOK", synthesis["summary"])
        self.assertIn("deterministic_grounded_v1", synthesis["model"])

    def test_explicit_unknowns_recorded_when_missing(self):
        profile = {
            "name": "Bare Bones ENK",
            "organisation_number": "123456789",
            "legal_form": "ENK",
            "employees": None,
            "evidence": {},
        }
        synthesis = generate_company_synthesis(profile)
        self.assertIn("annual accounts not filed or unavailable", synthesis["unknowns"])
        self.assertIn("employee count not reported to registry", synthesis["unknowns"])
        self.assertIn("no verified official website confirmed", synthesis["unknowns"])

    def test_active_roles_prioritized_over_inactive(self):
        profile = {
            "name": "Active Test AS",
            "organisation_number": "993578843",
            "legal_form": "AS",
            "evidence": {
                "roles": {
                    "value": {
                        "roles": [
                            {"name": "Old Resigned CEO", "role": "daglig leder", "inactive": True},
                            {"name": "Current Active CEO", "role": "daglig leder", "inactive": False},
                        ]
                    }
                }
            }
        }
        synthesis = generate_company_synthesis(profile)
        self.assertIn("Current Active CEO", synthesis["summary"])
        self.assertNotIn("Old Resigned CEO", synthesis["summary"])

    def test_string_financial_metrics_do_not_crash(self):
        profile = {
            "name": "String Financial AS",
            "organisation_number": "993578843",
            "legal_form": "AS",
            "evidence": {
                "financials": {
                    "value": {
                        "latest": {
                            "filing_year": 2024,
                            "revenue": "12345678",
                            "profit": "987654",
                        }
                    }
                }
            }
        }
        synthesis = generate_company_synthesis(profile)
        self.assertIn("12,345,678 NOK revenue", synthesis["summary"])
        self.assertIn("987,654 NOK", synthesis["summary"])


if __name__ == "__main__":
    unittest.main()
