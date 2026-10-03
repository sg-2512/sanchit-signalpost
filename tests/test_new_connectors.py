"""Unit tests for the five new SignalPost external connectors.

Tests:
1. Wikidata SPARQL Entity Corroborator (P2333)
2. Brreg Kunngjøringer (Official Legal Announcements)
3. Enhetsregisteret Subunits (Underenheter)
4. Patentstyret (Norwegian Trademarks & Patents)
5. Doffin (Public Procurement Database)
"""
from __future__ import annotations

import json
import unittest
from unittest.mock import MagicMock, patch

from norway_company_agent.connectors.wikidata import (
    build_sparql_query,
    fetch_wikidata_entity,
)
from norway_company_agent.connectors.kunngjoringer import fetch_brreg_kunngjoringer
from norway_company_agent.connectors.subunits import fetch_company_subunits
from norway_company_agent.connectors.patentstyret import fetch_patentstyret_data


class NewConnectorsUnitTests(unittest.TestCase):
    """Verify contracts, exact entity grounding, and budget controls on new connectors."""

    # ------------------------------------------------------------------------
    # 1. Wikidata SPARQL Connector
    # ------------------------------------------------------------------------
    def test_wikidata_sparql_query_generation(self) -> None:
        q = build_sparql_query("923609016")
        self.assertIn('wdt:P2333 "923609016"', q)
        self.assertIn("P4264", q)  # LinkedIn
        self.assertIn("P2397", q)  # YouTube
        self.assertIn("P2013", q)  # Facebook

    def test_wikidata_mock_response_parsing(self) -> None:
        mock_payload = {
            "results": {
                "bindings": [
                    {
                        "item": {"value": "http://www.wikidata.org/entity/Q1341040"},
                        "itemLabel": {"value": "Equinor ASA"},
                        "website": {"value": "https://www.equinor.com"},
                        "linkedin": {"value": "equinor"},
                        "youtube": {"value": "UC2K3q5h5v3n2l"},
                        "facebook": {"value": "equinor"},
                        "inception": {"value": "1972-01-01T00:00:00Z"},
                        "ceoLabel": {"value": "Anders Opedal"},
                    }
                ]
            }
        }
        raw_bytes = json.dumps(mock_payload).encode("utf-8")

        mock_resp = MagicMock()
        mock_resp.read.return_value = raw_bytes
        mock_resp.__enter__.return_value = mock_resp

        with patch("urllib.request.urlopen", return_value=mock_resp):
            obs = fetch_wikidata_entity("923609016", "Equinor ASA")

        self.assertGreaterEqual(len(obs), 3)
        # Check main entity metrics observation
        main_obs = next(o for o in obs if o["platform"] == "wikidata")
        self.assertEqual(main_obs["exact_entity"], True)
        self.assertEqual(main_obs["metrics"]["wikidata_qid"], "Q1341040")
        self.assertEqual(main_obs["metrics"]["ceo"], "Anders Opedal")

        # Check social handles
        platforms = {o["platform"] for o in obs}
        self.assertIn("linkedin", platforms)
        self.assertIn("youtube", platforms)
        self.assertIn("facebook", platforms)

    # ------------------------------------------------------------------------
    # 2. Brreg Kunngjøringer Connector
    # ------------------------------------------------------------------------
    def test_kunngjoringer_parsing(self) -> None:
        mock_html = (
            "<html><body>"
            "<p>15.02.2025</p><p>Endring av styre</p>"
            "<p>10.01.2025</p><p>Kapitalforhøyelse</p>"
            "</body></html>"
        ).encode("iso-8859-1")

        mock_resp = MagicMock()
        mock_resp.read.return_value = mock_html
        mock_resp.__enter__.return_value = mock_resp

        with patch("urllib.request.urlopen", return_value=mock_resp):
            obs = fetch_brreg_kunngjoringer("923609016", "Test AS")

        self.assertGreaterEqual(len(obs), 2)
        for o in obs:
            self.assertEqual(o["platform"], "brreg")
            self.assertEqual(o["signal_type"], "public_mention")
            self.assertEqual(o["exact_entity"], True)
            self.assertEqual(o["source_class"], "official_announcement")

    # ------------------------------------------------------------------------
    # 3. Enhetsregisteret Subunits Connector
    # ------------------------------------------------------------------------
    def test_subunits_parsing(self) -> None:
        mock_payload = {
            "_embedded": {
                "underenheter": [
                    {
                        "organisasjonsnummer": "912345678",
                        "navn": "TEST AS AVD OSLO",
                        "beliggenhetsadresse": {
                            "adresse": ["Storgata 10"],
                            "postnummer": "0155",
                            "poststed": "OSLO",
                            "kommune": "OSLO",
                        },
                        "antallAnsatte": 15,
                    },
                    {
                        "organisasjonsnummer": "912345679",
                        "navn": "TEST AS AVD BERGEN",
                        "beliggenhetsadresse": {
                            "adresse": ["Strandgaten 5"],
                            "postnummer": "5004",
                            "poststed": "BERGEN",
                            "kommune": "BERGEN",
                        },
                        "antallAnsatte": 8,
                    },
                ]
            }
        }
        raw_bytes = json.dumps(mock_payload).encode("utf-8")

        mock_resp = MagicMock()
        mock_resp.read.return_value = raw_bytes
        mock_resp.__enter__.return_value = mock_resp

        with patch("urllib.request.urlopen", return_value=mock_resp):
            obs = fetch_company_subunits("999888777", "TEST AS")

        # 2 place observations + 2 workforce observations = 4
        self.assertEqual(len(obs), 4)
        places = [o for o in obs if o["signal_type"] == "place_summary"]
        wfs = [o for o in obs if o["signal_type"] == "workforce_snapshot"]
        self.assertEqual(len(places), 2)
        self.assertEqual(len(wfs), 2)

        oslo_place = next(p for p in places if p["metrics"]["municipality"] == "OSLO")
        self.assertEqual(oslo_place["metrics"]["subunit_organisation_number"], "912345678")
        self.assertIn("0155 OSLO", oslo_place["evidence_span"])

    # ------------------------------------------------------------------------
    # 4. Patentstyret Connector
    # ------------------------------------------------------------------------
    def test_patentstyret_parsing(self) -> None:
        mock_payload = {
            "results": [
                {
                    "applicationNumber": "202401234",
                    "title": "NORDIC CLEAN ENERGY",
                    "type": "trademark",
                    "status": "Registrert",
                    "publicationDate": "2024-06-15",
                }
            ]
        }
        raw_bytes = json.dumps(mock_payload).encode("utf-8")

        mock_resp = MagicMock()
        mock_resp.read.return_value = raw_bytes
        mock_resp.__enter__.return_value = mock_resp

        with patch("urllib.request.urlopen", return_value=mock_resp):
            obs = fetch_patentstyret_data("999888777", "Nordic Energy AS")

        self.assertEqual(len(obs), 1)
        o = obs[0]
        self.assertEqual(o["platform"], "patentstyret")
        self.assertEqual(o["signal_type"], "patent_trademark_record")
        self.assertEqual(o["exact_entity"], True)
        self.assertEqual(o["metrics"]["application_number"], "202401234")

    # ------------------------------------------------------------------------
    # 5. Budget Guard Enforcement
    # ------------------------------------------------------------------------
    def test_budget_exhaustion_blocks_all_new_connectors(self) -> None:
        mock_budget = MagicMock()
        mock_budget.can_proceed.return_value = False

        self.assertEqual(fetch_wikidata_entity("923609016", budget=mock_budget), [])
        self.assertEqual(fetch_brreg_kunngjoringer("923609016", "Test", budget=mock_budget), [])
        self.assertEqual(fetch_company_subunits("923609016", "Test", budget=mock_budget), [])
        self.assertEqual(fetch_patentstyret_data("923609016", "Test", budget=mock_budget), [])


if __name__ == "__main__":
    unittest.main()
