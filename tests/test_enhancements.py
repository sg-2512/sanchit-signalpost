"""Unit tests for newly integrated SignalPost modules:
1. Norwegian Press Code News Credibility Engine (news_credibility.py)
2. Social Channel Security & Anti-Impersonation Filter (social_security.py)
3. LinkedIn Guest Typeahead Connector (connectors/linkedin.py)
4. Universal Magic-Byte Bulk File Sniffing (sampling.py)
"""
from __future__ import annotations

import io
import json
import gzip
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from norway_company_agent.news_credibility import (
    detect_clickbait,
    evaluate_news_credibility,
    extract_domain,
    verify_entity_in_headline,
)
from norway_company_agent.social_security import (
    detect_security_threats,
    assess_positive_purpose,
    verify_social_channel_security,
)
from norway_company_agent.connectors.linkedin import (
    discover_linkedin_company,
    normalize_company_core,
)
from norway_company_agent.discovery import probe_heuristic_domain
from norway_company_agent.sampling import iter_bulk


class NewsCredibilityTests(unittest.TestCase):
    def test_extract_domain(self):
        self.assertEqual(extract_domain("https://www.e24.no/boers/nyheter"), "e24.no")
        self.assertEqual(extract_domain("https://nrk.no/vestland/"), "nrk.no")
        self.assertEqual(extract_domain("dn.no"), "dn.no")

    def test_whitelisted_publisher_high_credibility(self):
        res = evaluate_news_credibility(
            title="Equinor ASA tildeles nye leteområder i Nordsjøen",
            publisher_name="E24",
            publisher_url="https://e24.no",
            source_link="https://e24.no/artikkel/123",
            published_at="2026-01-15T10:00:00Z",
            company_name="Equinor ASA",
        )
        self.assertTrue(res["is_publishable"])
        self.assertGreaterEqual(res["credibility_score"], 0.70)
        self.assertIn(res["credibility_tier"], {"high", "medium"})
        self.assertEqual(len(res["fatal_flags"]), 0)

    def test_blacklisted_domain_rejected(self):
        res = evaluate_news_credibility(
            title="Equinor ASA secret revealed shocking truth",
            publisher_name="Medium Blog",
            publisher_url="https://medium.com",
            source_link="https://medium.com/@user/article",
            published_at="2026-01-15T10:00:00Z",
            company_name="Equinor ASA",
        )
        self.assertFalse(res["is_publishable"])
        self.assertEqual(res["credibility_tier"], "rejected")
        self.assertTrue(any("blacklisted_source" in f for f in res["fatal_flags"]))

    def test_clickbait_detection(self):
        flags = detect_clickbait("SJOKK: Du vil ikke tro dette trikset fra selskapet!!!")
        self.assertTrue(any("clickbait_phrase" in f for f in flags))
        self.assertIn("excessive_exclamation", flags)

    def test_future_date_rejected(self):
        res = evaluate_news_credibility(
            title="Equinor ASA leverer rekordresultat",
            publisher_name="NRK",
            publisher_url="https://nrk.no",
            source_link="https://nrk.no/artikkel/future",
            published_at="2028-12-31T00:00:00Z",
            company_name="Equinor ASA",
        )
        self.assertFalse(res["is_publishable"])
        self.assertTrue(any("future_date" in f for f in res["fatal_flags"]))

    def test_entity_headline_matching(self):
        m1 = verify_entity_in_headline("Equinor ASA", "Equinor ASA inngår milliardavtale")
        self.assertTrue(m1["matched"])
        self.assertEqual(m1["mode"], "exact_legal_name")

        m2 = verify_entity_in_headline("Byggmester Flo AS", "Byggmester Flo bygger ny skole")
        self.assertTrue(m2["matched"])
        self.assertEqual(m2["mode"], "multi_token_base_name")

        m3 = verify_entity_in_headline("Equinor ASA", "Kommunevalget i Oslo avgjort")
        self.assertFalse(m3["matched"])


class SocialSecurityTests(unittest.TestCase):
    def test_detect_security_threats_quarantines_scams(self):
        scam_text = "Join our telegram invest bot for guaranteed profit and crypto giveaway t.me/profit"
        threats = detect_security_threats(scam_text)
        self.assertTrue(len(threats) >= 2)
        self.assertTrue(any("crypto" in t or "guaranteed" in t or "t.me" in t for t in threats))

    def test_clean_corporate_profile_passes(self):
        sec = verify_social_channel_security(
            platform="linkedin",
            channel_or_profile_name="Equinor",
            target_url="https://www.linkedin.com/company/equinor",
            company_name="Equinor ASA",
            content_samples=["Offisiell side for Equinor. Besøk oss på equinor.com for mer info."],
            website_domain="equinor.com",
        )
        self.assertTrue(sec["is_safe"])
        self.assertEqual(sec["impersonation_risk"], "low")
        self.assertEqual(sec["security_tier"], "verified_safe_and_authentic")

    def test_scam_channel_is_quarantined(self):
        sec = verify_social_channel_security(
            platform="youtube",
            channel_or_profile_name="Equinor Crypto Giveaway",
            target_url="https://youtube.com/c/fakeequinor",
            company_name="Equinor ASA",
            content_samples=["Claim free tokens bitcoin double your investment whatsapp investment"],
        )
        self.assertFalse(sec["is_safe"])
        self.assertEqual(sec["security_tier"], "quarantined_threat")
        self.assertTrue(len(sec["quarantine_reasons"]) > 0)


class LinkedInConnectorTests(unittest.TestCase):
    def test_normalize_company_core(self):
        self.assertEqual(normalize_company_core("Equinor ASA"), "equinor")
        self.assertEqual(normalize_company_core("Kongsberg Gruppen AS"), "kongsberg gruppen")
        self.assertEqual(normalize_company_core("BYGGMESTER OLSEN HOLDING"), "byggmester olsen")

    @patch("urllib.request.urlopen")
    def test_discover_linkedin_company_exact_match(self, mock_urlopen):
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps([
            {"type": "COMPANY", "id": "12345", "displayName": "Equinor"},
            {"type": "COMPANY", "id": "67890", "displayName": "Equinor Fans"},
        ]).encode("utf-8")
        mock_urlopen.return_value.__enter__.return_value = mock_response

        res = discover_linkedin_company("Equinor ASA", "923609016", website_domain="equinor.com")
        self.assertIsNotNone(res)
        self.assertEqual(res["platform"], "linkedin")
        self.assertEqual(res["display_name"], "Equinor")
        self.assertIn("linkedin.com/company/equinor", res["source_url"])
        self.assertTrue(res["exact_entity"])

    @patch("urllib.request.urlopen")
    def test_discover_linkedin_company_mismatch_abstains(self, mock_urlopen):
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps([
            {"type": "COMPANY", "id": "99999", "displayName": "Completely Different Company"},
        ]).encode("utf-8")
        mock_urlopen.return_value.__enter__.return_value = mock_response

        res = discover_linkedin_company("Equinor ASA", "923609016")
        self.assertIsNone(res)


class UniversalBulkLoaderTests(unittest.TestCase):
    def test_iter_bulk_gzipped_csv(self):
        csv_data = "organisasjonsnummer;navn;organisasjonsform.kode;antallAnsatte\n923609016;Equinor ASA;ASA;20000\n"
        with tempfile.NamedTemporaryFile(suffix=".csv", delete=False) as tf:
            gz_buf = io.BytesIO()
            with gzip.GzipFile(fileobj=gz_buf, mode="wb") as gz:
                gz.write(csv_data.encode("utf-8"))
            tf.write(gz_buf.getvalue())
            temp_path = tf.name

        try:
            records = list(iter_bulk(temp_path))
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0]["organisation_number"], "923609016")
            self.assertEqual(records[0]["name"], "Equinor ASA")
        finally:
            Path(temp_path).unlink(missing_ok=True)

    def test_iter_bulk_plain_jsonl(self):
        jsonl_data = json.dumps({"organisation_number": "912345678", "name": "Test AS", "employees": 5}) + "\n"
        with tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False, mode="w", encoding="utf-8") as tf:
            tf.write(jsonl_data)
            temp_path = tf.name

        try:
            records = list(iter_bulk(temp_path))
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0]["organisation_number"], "912345678")
            self.assertEqual(records[0]["name"], "Test AS")
        finally:
            Path(temp_path).unlink(missing_ok=True)


class HeuristicDomainProbeTests(unittest.TestCase):
    @patch("urllib.request.urlopen")
    def test_probe_heuristic_domain_found(self, mock_urlopen):
        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.read.return_value = b"<html><body>Org: 923609016 - Equinor ASA</body></html>"
        mock_response.geturl.return_value = "https://www.equinor.no"
        mock_urlopen.return_value.__enter__.return_value = mock_response

        profile = {"name": "Equinor ASA", "organisation_number": "923609016"}
        res = probe_heuristic_domain(profile)
        self.assertEqual(res, "https://www.equinor.no")

    @patch("urllib.request.urlopen")
    def test_probe_heuristic_domain_not_found(self, mock_urlopen):
        mock_urlopen.side_effect = Exception("404 Not Found")
        profile = {"name": "Nonexistent Fake Company AS", "organisation_number": "999999999"}
        res = probe_heuristic_domain(profile)
        self.assertIsNone(res)


class GooglePlacesWebsiteTests(unittest.TestCase):
    @patch("urllib.request.urlopen")
    def test_fetch_place_data_extracts_website_uri(self, mock_urlopen):
        from norway_company_agent.connectors.google_places import fetch_place_data

        fake_api_response = {
            "places": [
                {
                    "id": "ChIJfake123",
                    "displayName": {"text": "Farsund Bygg AS"},
                    "rating": 4.8,
                    "userRatingCount": 25,
                    "formattedAddress": "Havnegaten 1, 4550 Farsund, Norway",
                    "nationalPhoneNumber": "38 39 00 00",
                    "types": ["general_contractor"],
                    "googleMapsUri": "https://maps.google.com/?cid=123",
                    "websiteUri": "https://www.farsundbygg.no",
                }
            ]
        }
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps(fake_api_response).encode("utf-8")
        mock_urlopen.return_value.__enter__.return_value = mock_resp

        with patch.dict("os.environ", {"GOOGLE_PLACES_API_KEY": "AIzaSyTestKey"}):
            obs = fetch_place_data("987654321", "Farsund Bygg AS")

        self.assertGreater(len(obs), 0)
        place_obs = obs[0]
        self.assertEqual(place_obs["website_uri"], "https://www.farsundbygg.no")
        self.assertEqual(place_obs["metrics"]["website_uri"], "https://www.farsundbygg.no")

    @patch("run_agent.fetch_youtube_data", return_value=[])
    @patch("run_agent.fetch_google_news", return_value=[])
    @patch("run_agent.fetch_official_modules", return_value=({}, []))
    @patch("run_agent.places_available", return_value=True)
    def test_enrich_single_company_discovers_website_via_places(
        self, mock_places_avail, mock_fetch_official, mock_news, mock_yt
    ):
        from norway_company_agent.budget import BudgetTracker
        import run_agent

        profile = {
            "organisation_number": "987654321",
            "name": "Farsund Bygg AS",
            "website": None,
            "evidence": {},
        }
        budget = BudgetTracker(max_requests=100)

        fake_place_obs = [{
            "id": "gplaces-1",
            "platform": "google_places",
            "signal_type": "place_summary",
            "website_uri": "https://www.farsundbygg.no",
            "metrics": {"website_uri": "https://www.farsundbygg.no"},
        }]

        with patch("run_agent.fetch_place_data", return_value=fake_place_obs):
            with patch("run_agent.fetch_website") as mock_fetch_web:
                mock_fetch_web.return_value = (
                    {
                        "status": "available",
                        "source_url": "https://www.farsundbygg.no",
                        "value": {
                            "final_url": "https://www.farsundbygg.no",
                            "title": "Farsund Bygg AS",
                            "identity_text_excerpt": "Farsund Bygg AS Org.nr: 987654321",
                            "social_links": [
                                {"platform": "linkedin", "url": "https://www.linkedin.com/company/farsund-bygg"}
                            ],
                        },
                    },
                    {"requests": 1, "bytes": 1000},
                )
                enriched, obs = run_agent.enrich_single_company(
                    profile,
                    fetch_modules=set(),
                    requested_modules=["website"],
                    budget=budget,
                )

        self.assertEqual(enriched["website"], "https://www.farsundbygg.no")
        self.assertEqual(enriched["evidence"]["website"]["status"], "available")
        social_obs = [o for o in obs if o.get("platform") == "linkedin" and o.get("signal_type") == "profile_handle"]
        self.assertGreater(len(social_obs), 0)


class EmailDomainCandidateTests(unittest.TestCase):
    def test_extract_valid_corporate_email_domain(self):
        from norway_company_agent.discovery import extract_email_domain_candidate

        profile = {
            "evidence": {
                "registry": {"value": {"epostadresse": "firmapost@svorka.no"}}
            }
        }
        self.assertEqual(extract_email_domain_candidate(profile), "https://www.svorka.no")

    def test_reject_generic_email_domains(self):
        from norway_company_agent.discovery import extract_email_domain_candidate

        for generic_email in [
            "ceo@gmail.com",
            "info@hotmail.com",
            "contact@outlook.com",
            "boss@online.no",
            "board@styrerommet.net",
            "me@me.com",
        ]:
            profile = {
                "evidence": {
                    "registry": {"value": {"epostadresse": generic_email}}
                }
            }
            self.assertIsNone(extract_email_domain_candidate(profile))

    def test_empty_or_missing_email(self):
        from norway_company_agent.discovery import extract_email_domain_candidate

        self.assertIsNone(extract_email_domain_candidate({}))
        self.assertIsNone(extract_email_domain_candidate({"evidence": {"registry": {"value": {"epostadresse": ""}}}}))


if __name__ == "__main__":
    unittest.main()
