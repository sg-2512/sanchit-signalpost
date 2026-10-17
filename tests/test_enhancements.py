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


class OperatingLocationPlaceTests(unittest.TestCase):
    def test_extract_place_from_subunits(self):
        import run_agent
        from norway_company_agent.external_footprint import publishable_observation

        profile = {
            "organisation_number": "912345678",
            "name": "TEST NORGE AS",
            "evidence": {
                "locations": {
                    "value": {
                        "locations": [
                            {
                                "organisation_number": "987654321",
                                "name": "TEST NORGE AVD OSLO",
                                "address": {"kommune": "OSLO", "poststed": "OSLO"},
                            }
                        ]
                    },
                    "retrieved_at": "2026-09-28T12:00:00Z",
                }
            },
        }
        obs = run_agent.extract_place_observations(profile)
        self.assertEqual(len(obs), 1)
        self.assertEqual(obs[0]["signal_type"], "place_summary")
        self.assertEqual(obs[0]["platform"], "brreg")
        self.assertTrue(publishable_observation(obs[0]))

    def test_extract_place_fallback_to_headquarters(self):
        import run_agent
        from norway_company_agent.external_footprint import publishable_observation

        profile = {
            "organisation_number": "912345678",
            "name": "TEST SINGLE LOCATION AS",
            "address": {"kommune": "BERGEN", "poststed": "BERGEN", "adresse": ["Strandgaten 1"]},
            "evidence": {
                "locations": {"value": {"locations": []}},
                "registry": {"retrieved_at": "2026-09-28T12:00:00Z"},
            },
        }
        obs = run_agent.extract_place_observations(profile)
        self.assertEqual(len(obs), 1)
        self.assertEqual(obs[0]["signal_type"], "place_summary")
        self.assertEqual(obs[0]["strategy"], "official_headquarters_place")
        self.assertEqual(obs[0]["metrics"]["municipality"], "BERGEN")
        self.assertTrue(publishable_observation(obs[0]))

    def test_read_organisation_inputs_norwegian_variants(self):
        from norway_company_agent.batch import read_organisation_inputs
        import tempfile
        content = (
            '{"organisasjonsnummer": "923609016"}\n'
            '{"orgnr": "912345678"}\n'
            '{"org_nr": "987654321"}\n'
        )
        with tempfile.NamedTemporaryFile(suffix=".jsonl", mode="w", encoding="utf-8", delete=False) as tf:
            tf.write(content)
            tf_path = tf.name
        try:
            records = read_organisation_inputs(tf_path)
            self.assertEqual(len(records), 3)
            self.assertEqual(records[0]["organisation_number"], "923609016")
            self.assertEqual(records[1]["organisation_number"], "912345678")
            self.assertEqual(records[2]["organisation_number"], "987654321")
        finally:
            Path(tf_path).unlink(missing_ok=True)

    def test_evidence_terminal_state_budget_exhausted(self):
        from norway_company_agent.batch import evidence_terminal_state
        rec_status = {"status": "budget_exhausted"}
        self.assertEqual(evidence_terminal_state(rec_status), "budget_exhausted")
        rec_note = {"status": "source_error", "note": "budget_exhausted before request"}
        self.assertEqual(evidence_terminal_state(rec_note), "budget_exhausted")

    def test_linkedin_company_publishable_schema(self):
        from norway_company_agent.connectors.linkedin import discover_linkedin_company
        from norway_company_agent.external_footprint import publishable_observation, validate_observation
        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_response = MagicMock()
            mock_response.read.return_value = json.dumps([
                {"type": "COMPANY", "id": "12345", "displayName": "Equinor"},
            ]).encode("utf-8")
            mock_urlopen.return_value.__enter__.return_value = mock_response

            res = discover_linkedin_company("Equinor ASA", "923609016", website_domain="equinor.com")
            self.assertIsNotNone(res)
            self.assertEqual(validate_observation(res), [])
            self.assertTrue(publishable_observation(res))


class BuilderrCanonicalStatesComplianceTests(unittest.TestCase):
    """Verify Builderr challenge requirements:

    1. 'Return a result for every company' (zero missing rows, zero silent drops).
    2. 'Each result carries one of these states: available, not_available, blocked,
       not_applicable, ambiguous, failed'.
    3. '"I found nothing" is a valid answer. A missing row is not.'
    """

    CANONICAL_STATES = {
        "available",
        "not_available",
        "blocked",
        "not_applicable",
        "ambiguous",
        "failed",
    }

    def test_found_company_has_available_state(self):
        from norway_company_agent.batch import terminal_envelope
        from norway_company_agent.evidence import evidence

        profile = {
            "organisation_number": "923609016",
            "name": "EQUINOR ASA",
            "evidence": {
                "registry": evidence("registry", "available", "official", "https://data.brreg.no", value={"navn": "EQUINOR ASA"}),
                "website": evidence("website", "available", "company_site", "https://www.equinor.com", value={"identity_assessment": {"publishable": True}}),
            },
        }
        env = terminal_envelope(profile, run_id="test", modules=["registry", "website"], started_at="2026-10-01T00:00:00Z", completed_at="2026-10-01T00:00:01Z")
        self.assertEqual(env["availability"], "available")
        self.assertIn(env["availability"], self.CANONICAL_STATES)
        claims_avail = {c["availability"] for c in env["claims"]}
        self.assertTrue(claims_avail.issubset(self.CANONICAL_STATES))

    def test_not_found_company_emits_envelope_with_not_available_state(self):
        from norway_company_agent.batch import terminal_envelope
        from norway_company_agent.evidence import evidence

        # 'I found nothing' is a valid answer. A missing row is not.
        profile = {
            "organisation_number": "999999999",
            "name": "Organisation 999999999",
            "evidence": {
                "registry": evidence("registry", "not_found", "official", "https://data.brreg.no", note="Not found"),
            },
        }
        env = terminal_envelope(profile, run_id="test", modules=["registry"], started_at="2026-10-01T00:00:00Z", completed_at="2026-10-01T00:00:01Z")
        self.assertEqual(env["availability"], "not_available")
        self.assertIn(env["availability"], self.CANONICAL_STATES)

    def test_quarantined_website_emits_ambiguous_state(self):
        from norway_company_agent.batch import terminal_envelope
        from norway_company_agent.evidence import evidence

        # 'ambiguous — you could not be sure it is the right company'
        profile = {
            "organisation_number": "912345678",
            "name": "TEST NORGE AS",
            "evidence": {
                "registry": evidence("registry", "available", "official", "https://data.brreg.no", value={"navn": "TEST NORGE AS"}),
                "website": evidence(
                    "website",
                    "available",
                    "company_site",
                    "https://parent-company.com",
                    value={"final_url": "https://parent-company.com", "identity_assessment": {"publishable": False, "status": "related_or_uncertain"}},
                ),
            },
        }
        env = terminal_envelope(profile, run_id="test", modules=["registry", "website"], started_at="2026-10-01T00:00:00Z", completed_at="2026-10-01T00:00:01Z")
        web_claim = next(c for c in env["claims"] if c["field"] == "official_website")
        self.assertEqual(web_claim["availability"], "ambiguous")
        self.assertIsNone(web_claim["value"])

    def test_blocked_source_emits_blocked_state(self):
        from norway_company_agent.batch import terminal_envelope
        from norway_company_agent.evidence import evidence

        # 'blocked — the source refused the request'
        profile = {
            "organisation_number": "912345678",
            "name": "TEST NORGE AS",
            "evidence": {
                "registry": evidence("registry", "available", "official", "https://data.brreg.no", value={"navn": "TEST NORGE AS"}),
                "website": evidence("website", "blocked", "company_site", "https://example.com", note="HTTP 403 Forbidden"),
            },
        }
        env = terminal_envelope(profile, run_id="test", modules=["registry", "website"], started_at="2026-10-01T00:00:00Z", completed_at="2026-10-01T00:00:01Z")
        web_claim = next(c for c in env["claims"] if c["field"] == "official_website")
        self.assertEqual(web_claim["availability"], "blocked")

    def test_failed_run_emits_failed_state(self):
        from norway_company_agent.batch import terminal_envelope
        from norway_company_agent.evidence import evidence

        # 'failed — the run broke on this one'
        profile = {
            "organisation_number": "912345678",
            "name": "TEST NORGE AS",
            "evidence": {
                "registry": evidence("registry", "source_error", "official", "https://data.brreg.no", note="Worker crash"),
            },
        }
        env = terminal_envelope(profile, run_id="test", modules=["registry"], started_at="2026-10-01T00:00:00Z", completed_at="2026-10-01T00:00:01Z")
        self.assertEqual(env["availability"], "failed")
        self.assertEqual(env["run"]["terminal_status"], "failed")


class AdvancedLearningsTests(unittest.TestCase):
    """Tests verifying the architectural advancements inspired by Nikita's repository review."""

    def test_sanitize_url_redacts_api_keys_and_tokens(self):
        from norway_company_agent.evidence import sanitize_url, evidence

        # Test direct URL sanitization
        places_url = "https://maps.googleapis.com/maps/api/place/textsearch/json?query=Test+AS&key=AIzaSySecretApiKey123"
        clean_url = sanitize_url(places_url)
        self.assertNotIn("AIzaSySecretApiKey123", clean_url)
        self.assertIn("key=REDACTED", clean_url)

        # Test evidence record creation automatically sanitizes URL
        ev = evidence("reviews", "available", "licensed_api", places_url)
        self.assertNotIn("AIzaSySecretApiKey123", ev["source_url"])
        self.assertIn("key=REDACTED", ev["source_url"])

    def test_expanded_parked_domain_markers_reject_placeholder_pages(self):
        from norway_company_agent.identity import assess_website_identity

        # Test domeneshop parking placeholder
        parked_domeneshop = {
            "organisation_number": "912345678",
            "name": "Nordic Tech AS",
            "evidence": {
                "website": {
                    "status": "available",
                    "value": {
                        "final_url": "https://nordictech.no",
                        "title": "Nordic Tech AS - Domeneshop parkering",
                        "main_text_excerpt": "Domenet er parkert hos Domeneshop.",
                    },
                },
            },
        }
        res = assess_website_identity(parked_domeneshop)
        self.assertFalse(res["publishable"])
        self.assertLessEqual(res["score"], 0.1)

        # Test dan.com for-sale placeholder
        parked_dan = {
            "organisation_number": "912345678",
            "name": "Acme Nordic AS",
            "evidence": {
                "website": {
                    "status": "available",
                    "value": {
                        "final_url": "https://acmenordic.no",
                        "title": "Acme Nordic AS",
                        "main_text_excerpt": "Buy this domain on dan.com. Domenet er til salgs.",
                    },
                },
            },
        }
        res_dan = assess_website_identity(parked_dan)
        self.assertFalse(res_dan["publishable"])

    def test_statutory_annual_accounts_workforce_fte_mining(self):
        from norway_company_agent.hiring import evaluate_company_hiring

        profile = {
            "organisation_number": "912345678",
            "name": "Byggmester Hansen AS",
            "employees": None,
            "evidence": {
                "financials": {
                    "status": "available",
                    "value": {
                        "notes": "Note 4 Lønnskostnader: Selskapet har sysselsatt 14 årsverk i regnskapsåret.",
                    },
                },
            },
        }
        hiring_block, obs = evaluate_company_hiring(profile)
        signals = hiring_block.get("signals", [])
        fte_signal = next((s for s in signals if s.get("type") == "statutory_workforce_fte"), None)
        self.assertIsNotNone(fte_signal)
        self.assertEqual(fte_signal["count"], 14)
        self.assertEqual(fte_signal["source"], "regnskapsregisteret_annual_accounts")

    def test_v4_social_profiles_and_synthesis_in_envelope(self):
        from norway_company_agent.batch import terminal_envelope
        from norway_company_agent.official import normalize_entity

        norm = normalize_entity({
            "organisasjonsnummer": "985589003",
            "navn": "ARKITEKTFIRMA JON VIKØREN AS",
            "epostadresse": "post@arkjv.no",
            "telefon": "57 69 89 50",
        })
        self.assertEqual(norm["epostadresse"], "post@arkjv.no")
        self.assertEqual(norm["telefon"], "57 69 89 50")

        profile = {
            "organisation_number": "985589003",
            "name": "ARKITEKTFIRMA JON VIKØREN AS",
            "legal_form": "AS",
            "municipality": "VIK",
            "social_profiles": [
                {
                    "platform": "linkedin",
                    "url": "https://www.linkedin.com/company/arkitektfirma-jon-vikoren-as",
                    "content_sha256": "a" * 64,
                    "evidence_span": "Verified LinkedIn company profile",
                }
            ],
            "evidence": {
                "registry": {
                    "field": "registry",
                    "status": "available",
                    "source_type": "official_registry_bulk",
                    "source_url": "https://data.brreg.no/enhetsregisteret/api/enheter/lastned/csv",
                    "retrieved_at": "2026-10-17T00:00:00Z",
                    "content_sha256": "b" * 64,
                    "value": {"organisasjonsnummer": "985589003", "navn": "ARKITEKTFIRMA JON VIKØREN AS"},
                },
                "website": {
                    "field": "website",
                    "status": "not_found",
                    "source_type": "registry_linked_company_website",
                    "source_url": "https://data.brreg.no/enhetsregisteret/api/enheter",
                    "retrieved_at": "2026-10-17T00:00:00Z",
                    "content_sha256": None,
                    "value": None,
                },
            },
        }
        env = terminal_envelope(
            profile,
            run_id="test-v4",
            modules=["registry", "website"],
            started_at="2026-10-17T00:00:00Z",
            completed_at="2026-10-17T00:00:01Z",
        )
        self.assertIn("synthesis", env)
        self.assertTrue(env["synthesis"]["summary"])
        claims_by_field = {c["field"]: c for c in env["claims"]}
        self.assertEqual(claims_by_field["social_profile"]["availability"], "available")
        self.assertEqual(
            claims_by_field["social_profile"]["value"],
            "https://www.linkedin.com/company/arkitektfirma-jon-vikoren-as",
        )
        self.assertEqual(claims_by_field["social_profiles"]["availability"], "available")


class NewNorwegianConnectorsTests(unittest.TestCase):
    def test_doffin_procurement_connector(self):
        from norway_company_agent.connectors.doffin import fetch_doffin_procurement

        mock_payload = {
            "items": [{
                "id": "2026-123456",
                "title": "IKT-driftstjenester rammeavtale",
                "publicationDate": "2026-03-01",
                "url": "https://doffin.no/Notice/Details/2026-123456",
            }]
        }
        with patch("urllib.request.urlopen") as mock_open:
            mock_resp = MagicMock()
            mock_resp.read.return_value = json.dumps(mock_payload).encode("utf-8")
            mock_resp.__enter__.return_value = mock_resp
            mock_open.return_value = mock_resp

            obs = fetch_doffin_procurement("985589003", "ARKITEKTFIRMA JON VIKØREN AS")
            self.assertEqual(len(obs), 1)
            self.assertEqual(obs[0]["platform"], "doffin")
            self.assertEqual(obs[0]["signal_type"], "public_procurement_award")
            self.assertTrue(obs[0]["exact_entity"])
            self.assertIn("IKT-driftstjenester", obs[0]["evidence_span"])

    def test_kartverket_cadastre_connector(self):
        from norway_company_agent.connectors.kartverket import geocode_address_kartverket

        mock_payload = {
            "adresser": [{
                "representasjonspunkt": {"lat": 61.215, "lon": 6.582},
                "adressetekst": "Sentrumsvegen 12",
                "postnummer": "6899",
                "poststed": "VIK I SOGN",
                "kommunenavn": "VIK",
                "gardsnummer": 14,
                "bruksnummer": 2,
                "seksjonsnummer": 1,
            }]
        }
        with patch("urllib.request.urlopen") as mock_open:
            mock_resp = MagicMock()
            mock_resp.read.return_value = json.dumps(mock_payload).encode("utf-8")
            mock_resp.__enter__.return_value = mock_resp
            mock_open.return_value = mock_resp

            res = geocode_address_kartverket("Sentrumsvegen 12", "VIK", "6899")
            self.assertIsNotNone(res)
            self.assertEqual(res["source"], "Kartverket Geonorge")
            self.assertEqual(res["gardsnummer"], 14)
            self.assertEqual(res["bruksnummer"], 2)
            self.assertEqual(res["latitude"], 61.215)
            self.assertIn("Gnr 14, Bnr 2", res["cadastral_id"])

    def test_mattilsynet_smilefjes_connector(self):
        from norway_company_agent.connectors.mattilsynet import fetch_mattilsynet_inspections

        mock_payload = {
            "entries": [{
                "tilsynid": "12345",
                "dato": "20260215",
                "total_karakter": "0",
                "navn": "Bakehuset AS",
            }]
        }
        with patch("urllib.request.urlopen") as mock_open:
            mock_resp = MagicMock()
            mock_resp.read.return_value = json.dumps(mock_payload).encode("utf-8")
            mock_resp.__enter__.return_value = mock_resp
            mock_open.return_value = mock_resp

            obs = fetch_mattilsynet_inspections("985589003", "Bakehuset AS")
            self.assertEqual(len(obs), 1)
            self.assertEqual(obs[0]["platform"], "mattilsynet")
            self.assertEqual(obs[0]["signal_type"], "food_safety_inspection")
            self.assertEqual(obs[0]["metrics"]["grade"], "0")

    def test_regulatory_licenses_connector(self):
        from norway_company_agent.connectors.regulatory_licenses import fetch_regulatory_licenses

        mock_payload = {
            "status": "Godkjent",
            "godkjenningsstatus": "Godkjent renholdsvirksomhet",
        }
        with patch("urllib.request.urlopen") as mock_open:
            mock_resp = MagicMock()
            mock_resp.read.return_value = json.dumps(mock_payload).encode("utf-8")
            mock_resp.__enter__.return_value = mock_resp
            mock_open.return_value = mock_resp

            obs = fetch_regulatory_licenses("985589003", "VIK RENHOLD AS", industry_code="81.210")
            self.assertEqual(len(obs), 1)
            self.assertEqual(obs[0]["platform"], "arbeidstilsynet")
            self.assertEqual(obs[0]["signal_type"], "regulatory_license_record")
            self.assertEqual(obs[0]["metrics"]["status"], "Godkjent")

    def test_esg_miljofyrtarn_connector(self):
        from norway_company_agent.connectors.esg_certifications import fetch_esg_certifications

        mock_esg_html = """
        <div class="certified-company">
          <h4>VIK ENERGI AS (985589003)</h4>
          <span class="cert-status">Gyldig sertifikat</span>
        </div>
        """
        with patch("urllib.request.urlopen") as mock_open:
            mock_resp = MagicMock()
            mock_resp.read.return_value = mock_esg_html.encode("utf-8")
            mock_resp.__enter__.return_value = mock_resp
            mock_open.return_value = mock_resp

            obs = fetch_esg_certifications("985589003", "VIK ENERGI AS")
            self.assertEqual(len(obs), 1)
            self.assertEqual(obs[0]["platform"], "miljofyrtarn")
            self.assertEqual(obs[0]["signal_type"], "esg_certification_record")
            self.assertIn("Sertifisert", obs[0]["metrics"]["status"])

    def test_proff_directory_connector(self):
        from norway_company_agent.connectors.proff_directory import resolve_commercial_directory_presence

        mock_proff_html = """
        <div class="company-details">
          <a class="official-url" href="https://www.arkjv.no">Besøk hjemmeside</a>
          <span class="phone-link">57 69 89 50</span>
        </div>
        """
        with patch("urllib.request.urlopen") as mock_open:
            mock_resp = MagicMock()
            mock_resp.read.return_value = mock_proff_html.encode("utf-8")
            mock_resp.__enter__.return_value = mock_resp
            mock_open.return_value = mock_resp

            site, obs = resolve_commercial_directory_presence("985589003", "ARKITEKTFIRMA JON VIKØREN AS")
            self.assertEqual(site, "https://www.arkjv.no")
            self.assertGreaterEqual(len(obs), 1)
            self.assertEqual(obs[0]["platform"], "proff")

    def test_lovdata_insolvency_connector(self):
        from norway_company_agent.connectors.lovdata import fetch_insolvency_legal_notices

        mock_gazette_html = """
        <div class="announcement">
          <span class="title">Kunngjøring om åpning av konkurs</span>
          <span class="date">12.01.2026</span>
          <span class="orgnr">985589003</span>
        </div>
        """
        with patch("urllib.request.urlopen") as mock_open:
            mock_resp = MagicMock()
            mock_resp.read.return_value = mock_gazette_html.encode("iso-8859-1")
            mock_resp.__enter__.return_value = mock_resp
            mock_open.return_value = mock_resp

            obs = fetch_insolvency_legal_notices("985589003", "VIK HANDEL AS")
            self.assertEqual(len(obs), 1)
            self.assertEqual(obs[0]["platform"], "lovdata_konkursregisteret")
            self.assertEqual(obs[0]["signal_type"], "legal_gazette_notice")

    def test_norid_domain_ownership(self):
        from norway_company_agent.connectors.norid import verify_norid_domain_ownership

        mock_rdap = {
            "entities": [{
                "roles": ["registrant"],
                "publicIds": [{"type": "orgno", "identifier": "985589003"}],
                "vcardArray": ["vcard", [["fn", {}, "text", "ARKITEKTFIRMA JON VIKØREN AS"]]],
            }]
        }
        with patch("urllib.request.urlopen") as mock_open:
            mock_resp = MagicMock()
            mock_resp.read.return_value = json.dumps(mock_rdap).encode("utf-8")
            mock_resp.__enter__.return_value = mock_resp
            mock_open.return_value = mock_resp

            obs = verify_norid_domain_ownership("arkjv.no", "985589003", "ARKITEKTFIRMA JON VIKØREN AS")
            self.assertIsNotNone(obs)
            self.assertEqual(obs["platform"], "norid")
            self.assertEqual(obs["signal_type"], "legal_domain_ownership")
            self.assertTrue(obs["exact_entity"])
            self.assertTrue(obs["metrics"]["verified_ownership"])

    def test_losoreregisteret_liens(self):
        from norway_company_agent.connectors.losoreregisteret import fetch_losoreregisteret_liens

        mock_payload = {
            "heftelser": [{
                "dokumentnummer": "2026123456",
                "pantetype": "Driftstilbehørpant",
                "belop": 5000000,
                "valuta": "NOK",
                "tinglystDato": "2026-01-20",
                "panthaver": "DNB Bank ASA",
            }]
        }
        with patch("urllib.request.urlopen") as mock_open:
            mock_resp = MagicMock()
            mock_resp.read.return_value = json.dumps(mock_payload).encode("utf-8")
            mock_resp.__enter__.return_value = mock_resp
            mock_open.return_value = mock_resp

            obs = fetch_losoreregisteret_liens("985589003", "ARKITEKTFIRMA JON VIKØREN AS")
            self.assertEqual(len(obs), 1)
            self.assertEqual(obs[0]["platform"], "losoreregisteret")
            self.assertEqual(obs[0]["signal_type"], "registered_mortgage_lien")
            self.assertEqual(obs[0]["metrics"]["amount"], 5000000)
            self.assertEqual(obs[0]["metrics"]["secured_party"], "DNB Bank ASA")

    def test_forskningsradet_grants(self):
        from norway_company_agent.connectors.forskningsradet import fetch_forskningsradet_grants

        mock_payload = {
            "projects": [{
                "projectNumber": "320145",
                "title": "Bærekraftig trearkitektur i fjordlandskap",
                "fundingScheme": "BIA",
                "allocatedAmount": 4200000,
                "startYear": "2025",
                "endYear": "2027",
            }]
        }
        with patch("urllib.request.urlopen") as mock_open:
            mock_resp = MagicMock()
            mock_resp.read.return_value = json.dumps(mock_payload).encode("utf-8")
            mock_resp.__enter__.return_value = mock_resp
            mock_open.return_value = mock_resp

            obs = fetch_forskningsradet_grants("985589003", "ARKITEKTFIRMA JON VIKØREN AS")
            self.assertEqual(len(obs), 1)
            self.assertEqual(obs[0]["platform"], "forskningsradet")
            self.assertEqual(obs[0]["signal_type"], "public_rnd_grant")
            self.assertEqual(obs[0]["metrics"]["amount_nok"], 4200000)

    def test_innovasjon_norge_grants(self):
        from norway_company_agent.connectors.innovasjon_norge import fetch_innovasjon_norge_grants

        mock_payload = {
            "tildelinger": [{
                "id": "IN-2025-9988",
                "prosjektnavn": "Eksportinitiativ Vestland",
                "ordning": "Miljøteknologiordningen",
                "tilsagn": 1500000,
                "ar": "2025",
                "fylke": "Vestland",
            }]
        }
        with patch("urllib.request.urlopen") as mock_open:
            mock_resp = MagicMock()
            mock_resp.read.return_value = json.dumps(mock_payload).encode("utf-8")
            mock_resp.__enter__.return_value = mock_resp
            mock_open.return_value = mock_resp

            obs = fetch_innovasjon_norge_grants("985589003", "ARKITEKTFIRMA JON VIKØREN AS")
            self.assertEqual(len(obs), 1)
            self.assertEqual(obs[0]["platform"], "innovasjon_norge")
            self.assertEqual(obs[0]["signal_type"], "innovation_grant_award")
            self.assertEqual(obs[0]["metrics"]["amount_nok"], 1500000)

    def test_oslo_bors_disclosures(self):
        from norway_company_agent.connectors.oslo_bors import fetch_oslo_bors_disclosures

        mock_payload = {
            "data": [{
                "messageId": "654321",
                "title": "Årsrapport 2025 godkjent av styret",
                "issuerSign": "VIK",
                "market": "Oslo Børs",
                "publishedTime": "2026-03-15T08:00:00Z",
                "categoryName": "Årsrapport",
            }]
        }
        with patch("urllib.request.urlopen") as mock_open:
            mock_resp = MagicMock()
            mock_resp.read.return_value = json.dumps(mock_payload).encode("utf-8")
            mock_resp.__enter__.return_value = mock_resp
            mock_open.return_value = mock_resp

            obs = fetch_oslo_bors_disclosures("985589003", "VIK ASA")
            self.assertEqual(len(obs), 1)
            self.assertEqual(obs[0]["platform"], "oslo_bors")
            self.assertEqual(obs[0]["signal_type"], "stock_exchange_disclosure")
            self.assertEqual(obs[0]["metrics"]["ticker"], "VIK")

    def test_ssb_industry_benchmark(self):
        from norway_company_agent.connectors.ssb_benchmarks import fetch_ssb_industry_benchmark

        obs = fetch_ssb_industry_benchmark("71.112", "985589003", "ARKITEKTFIRMA JON VIKØREN AS")
        self.assertIsNotNone(obs)
        self.assertEqual(obs["platform"], "ssb")
        self.assertEqual(obs["signal_type"], "industry_macro_benchmark")
        self.assertEqual(obs["metrics"]["avg_operating_margin_pct"], 7.6)
        self.assertEqual(obs["metrics"]["median_monthly_salary_nok"], 69100)

    def test_gleif_identifier(self):
        from norway_company_agent.connectors.gleif import fetch_gleif_identifier

        mock_payload = {
            "data": [{
                "id": "5493001KJTIIGC8Y1R12",
                "attributes": {
                    "lei": "5493001KJTIIGC8Y1R12",
                    "registration": {
                        "status": "ISSUED",
                        "managingLou": "Nordic Trustee",
                        "initialRegistrationDate": "2020-05-12T00:00:00Z",
                    },
                    "entity": {
                        "legalName": {"name": "ARKITEKTFIRMA JON VIKØREN AS"},
                    },
                },
            }]
        }
        with patch("urllib.request.urlopen") as mock_open:
            mock_resp = MagicMock()
            mock_resp.read.return_value = json.dumps(mock_payload).encode("utf-8")
            mock_resp.__enter__.return_value = mock_resp
            mock_open.return_value = mock_resp

            obs = fetch_gleif_identifier("985589003", "ARKITEKTFIRMA JON VIKØREN AS")
            self.assertIsNotNone(obs)
            self.assertEqual(obs["platform"], "gleif")
            self.assertEqual(obs["signal_type"], "global_legal_entity_identifier")
            self.assertEqual(obs["metrics"]["lei"], "5493001KJTIIGC8Y1R12")

    def test_miljodirektoratet_permits(self):
        from norway_company_agent.connectors.miljodirektoratet import fetch_miljodirektoratet_permits

        mock_payload = {
            "tillatelser": [{
                "tillatelseId": "MD-2024-554",
                "anleggsnavn": "Vik Pukkverk og Asfalt",
                "tillatelsestype": "Utslippstillatelse",
                "status": "Gjeldende",
                "vedtaksdato": "2024-08-10",
            }]
        }
        with patch("urllib.request.urlopen") as mock_open:
            mock_resp = MagicMock()
            mock_resp.read.return_value = json.dumps(mock_payload).encode("utf-8")
            mock_resp.__enter__.return_value = mock_resp
            mock_open.return_value = mock_resp

            obs = fetch_miljodirektoratet_permits("985589003", "VIK PUKK AS")
            self.assertEqual(len(obs), 1)
            self.assertEqual(obs[0]["platform"], "miljodirektoratet")
            self.assertEqual(obs[0]["signal_type"], "environmental_permit_record")
            self.assertEqual(obs[0]["metrics"]["status"], "Gjeldende")


if __name__ == "__main__":
    unittest.main()




