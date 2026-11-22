"""Tests for the factual synthesis generator."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from norway_company_agent.synthesis import generate_company_synthesis


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


    def test_synthesis_takes_newest_year_when_records_lag_register(self):
        """Verify that synthesis always takes the newest year (e.g. 2025 from register over 2023 from laggy line items)."""
        profile = {
            "name": "Lagging Accounts AS",
            "organisation_number": "993578843",
            "legal_form": "AS",
            "latest_submitted_accounts": "2025",
            "evidence": {
                "registry_live": {
                    "value": {
                        "sisteInnsendteAarsregnskap": "2025"
                    }
                },
                "financials": {
                    "value": {
                        "records": [
                            {
                                "record_id": 1234,
                                "period": {"tilDato": "2023-12-31"},
                                "revenue": 10000000,
                                "annual_result": 500000,
                            }
                        ],
                        "latest": {
                            "filing_year": 2023,
                            "revenue": 10000000,
                            "profit": 500000,
                        }
                    }
                }
            }
        }
        synthesis = generate_company_synthesis(profile)
        self.assertIn("2025", synthesis["summary"])
        self.assertIn("latest annual accounts filed for fiscal year 2025", synthesis["what_changed"])

if __name__ == "__main__":
    unittest.main()
