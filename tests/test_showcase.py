"""
Unit tests for Signalpost Interactive Showcase (showcase.py).
Verifies that the generated showcase matches builderr.ai/signalpost specifications.
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from norway_company_agent.showcase import (
    compact_company_profile,
    render_showcase_html,
    write_showcase_html,
)


class ShowcaseTests(unittest.TestCase):
    def setUp(self):
        self.sample_profile = {
            "organisation_number": 979543883,
            "name": "DIPS AS",
            "legal_form": "AS",
            "employees": 365,
            "municipality": "BODØ",
            "industry_code": "62.020",
            "industry_label": "Konsulentvirksomhet tilknyttet IT",
            "website": "https://www.dips.no/",
            "bankrupt": False,
            "liquidating": False,
            "evidence": {
                "registry": {"status": "available", "source_url": "https://data.brreg.no/api"},
                "financials": {
                    "status": "available",
                    "source_url": "https://data.brreg.no/regnskap",
                    "value": {
                        "records": [{
                            "revenue": 790400865,
                            "operating_result": 192626830,
                            "annual_result": 163960244,
                            "currency": "NOK",
                            "period": {"fraDato": "2025-01-01", "tilDato": "2025-12-31"},
                        }]
                    },
                },
                "roles": {
                    "status": "available",
                    "source_url": "https://data.brreg.no/roller",
                    "value": {
                        "roles": [{"name": "Thomas Smedsrud", "role": "Daglig leder", "inactive": False}]
                    },
                },
                "locations": {
                    "status": "available",
                    "source_url": "https://data.brreg.no/subunits",
                    "value": {
                        "locations": [{"name": "DIPS AS AVD BODØ", "address": {"kommune": "BODØ"}}]
                    },
                },
                "website": {
                    "status": "available",
                    "source_url": "https://www.dips.no/",
                    "value": {
                        "title": "DIPS - Vi skaper helseteknologi",
                        "description": "DIPS utvikler helseteknologi for norske sykehus.",
                        "pages": [{"url": "https://www.dips.no/", "title": "Home"}],
                        "social_links": [{"platform": "linkedin", "url": "https://linkedin.com/company/dips-as"}],
                        "identity_assessment": {"publishable": True, "score": 0.95, "reasons": ["Exact name match"]},
                    },
                },
            },
        }

        self.sample_obs = [
            {
                "organisation_number": 979543883,
                "platform": "job_board",
                "signal_type": "job_posting",
                "exact_entity": True,
                "source_url": "https://arbeidsplassen.nav.no/stillinger/stilling/123",
                "metrics": {"title": "Senior Cloud Engineer", "location": "Bodø"},
                "rights_status": "approved",
            },
            {
                "organisation_number": 979543883,
                "platform": "linkedin",
                "signal_type": "profile_handle",
                "exact_entity": True,
                "source_url": "https://linkedin.com/company/dips-as",
                "rights_status": "approved",
            },
            {
                "organisation_number": 979543883,
                "platform": "linkedin",
                "signal_type": "public_post",
                "exact_entity": True,
                "source_url": "https://linkedin.com/posts/dips_update_1",
                "evidence_span": "Visste du at KI-agenter kan nå bruke komplekse datasystemer?",
                "metrics": {"likes": 16, "comments": 1},
                "rights_status": "approved",
            },
        ]

    def test_compact_company_profile(self):
        compacted = compact_company_profile(self.sample_profile, self.sample_obs)
        self.assertEqual(compacted["name"], "DIPS AS")
        self.assertEqual(compacted["org"], "979543883")
        self.assertEqual(compacted["coverage"]["score"], 5)
        self.assertEqual(compacted["coverage"]["total"], 5)
        self.assertGreater(compacted["richness"], 40)
        self.assertEqual(len(compacted["external"]["linkedin"]["jobs"]), 1)
        self.assertEqual(compacted["external"]["linkedin"]["jobs"][0]["title"], "Senior Cloud Engineer")
        self.assertEqual(len(compacted["external"]["linkedin"]["posts"]), 1)

    def test_render_and_write_showcase_html(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            out_path = Path(tmp_dir) / "showcase.html"
            written = write_showcase_html([self.sample_profile], self.sample_obs, out_path)
            self.assertTrue(written.exists())
            html = written.read_text(encoding="utf-8")
            self.assertIn("Signalpost", html)
            self.assertIn("DIPS AS", html)
            self.assertIn("Ask Signalpost", html)
            self.assertIn("Company directory", html)
            self.assertIn("const DATA =", html)


if __name__ == "__main__":
    unittest.main()
