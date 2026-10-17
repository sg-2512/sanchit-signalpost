#!/usr/bin/env python3
"""Empirical Adversarial Stress Harness for Identity Gating & Thread-Safe Budget Tracking.

Author: Challenger 1 (Identity & Budget Stress Challenger)
Date: 2026-10-17

Adversarial Verification Scope:
1. Identity Gating:
   - Parked domain markers (18+ distinct variations, for-sale placeholders, Sedo, Dan, Domeneshop)
   - Norwegian character normalization mismatch bug in parked marker detection
   - Login walls, error query parameters, authentication traps
   - Partial name overlaps & generic industry/geographic token confusion
   - Foreign branch vs parent name collisions (NUF vs foreign parent)
   - Sovereign registry / BBL co-op portal gating (valid vs minside portal)
   - Social link identity gating (generic endpoints, parent handle vs subsidiary)
   - Strict 0.90 publication threshold & 100% quarantine verification
2. Budget Enforcement:
   - Multi-threaded concurrency contention on BudgetTracker (32 threads, 3,200 operations)
   - Thread-safe atomic lock acquisition across requests, costs, bytes, and latencies
   - Strict quota exhaustion (requests, costs, wall-clock time)
   - Statutory Brreg bypass protection under exhaustion vs non-Brreg immediate blocking
   - Mode-dependent Brreg behavior (production max_requests > 50 vs test mode <= 50)
   - Time-of-Check to Time-of-Use (TOCTOU) concurrency race window in fetch_json
"""
from __future__ import annotations

import concurrent.futures
import math
import sys
import time
import unicodedata
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from norway_company_agent.identity import (
    apply_website_identity_gate,
    assess_social_identity,
    assess_website_identity,
    _tokens,
)
from norway_company_agent.budget import BudgetTracker, BudgetExhausted
from norway_company_agent.http import FetchResult


class EmpiricalIdentityGatingStressTests(unittest.TestCase):
    """Area 1: Adversarial Stress Testing of Identity Gating and Zero Wrong-Company Rules."""

    def test_ascii_parked_domain_markers_rejected_and_quarantined(self) -> None:
        """Every ASCII-only parked, for-sale, and placeholder marker must be rejected with score 0.1."""
        ascii_parked_phrases = [
            "domain is for sale",
            "domain for sale",
            "hugedomains",
            "parked at",
            "miss hosting",
            "her flytter snart en ny gjest",
            "has been informing visitors",
            "find the best information and most relevant links on all topics related to",
            "buy this domain",
            "domenet er til salgs",
            "this domain is parked",
            "under construction / sedo",
            "sedo domain parking",
            "domeneshop parkering",
            "parkert hos domeneshop",
            "dan.com",
            "the sponsored listings displayed above",
            "related searches",
            "is parked free",
            "siden er under konstruksjon",
        ]

        for phrase in ascii_parked_phrases:
            profile = {
                "organisation_number": "912345678",
                "name": "Nordic Innovate AS",
                "evidence": {
                    "website": {
                        "status": "available",
                        "value": {
                            "final_url": "https://nordicinnovate.no",
                            "title": f"Nordic Innovate - {phrase}",
                            "main_text_excerpt": f"Welcome. {phrase} on our server.",
                        },
                    }
                },
            }
            res = assess_website_identity(profile)
            self.assertFalse(res["publishable"], f"Parked marker '{phrase}' was marked publishable!")
            self.assertEqual(res["score"], 0.1, f"Parked marker '{phrase}' did not get score 0.1")
            self.assertEqual(res["status"], "related_or_uncertain")
            self.assertTrue(any("parked" in r or "placeholder" in r for r in res["reasons"]))

    def test_vulnerability_norwegian_char_parked_marker_encoding_flaw(self) -> None:
        """EMPIRICAL FINDING: Norwegian characters ('ø', 'å') in parked_markers fail to match normalized_raw.
        
        In identity.py:80, candidate_text is normalized via:
          normalized_raw = unicodedata.normalize('NFKD', candidate_text).encode('ascii', 'ignore').decode().casefold()
        This strips non-ASCII chars ('kjøp' -> 'kjp', 'vårt' -> 'vart').
        However, parked_markers contains raw UTF-8 strings ('kjøp dette domenet', 'velkommen til vårt nye nettsted').
        Consequently, 'marker in normalized_raw' evaluates to False!
        If a parked domain page contains the company name, it bypasses the parked filter and gets
        published with score 0.95 (exact identity)!
        """
        parked_with_norwegian_chars = {
            "organisation_number": "912345678",
            "name": "Nordic Innovate AS",
            "evidence": {
                "website": {
                    "status": "available",
                    "value": {
                        "final_url": "https://nordicinnovate.no",
                        "title": "Nordic Innovate - Kjøp dette domenet",
                        "main_text_excerpt": "Domenet for selskapet. Kjøp dette domenet på Sedo.",
                    },
                }
            },
        }
        res = assess_website_identity(parked_with_norwegian_chars)

        # Empirically verify that the bug causes this parked domain to be published!
        self.assertTrue(res["publishable"], "Demonstrates that parked domain bypassed filter due to character stripping")
        self.assertEqual(res["score"], 0.95)
        self.assertEqual(res["status"], "exact")

    def test_login_walls_and_error_redirects_rejected(self) -> None:
        """URLs redirecting to login walls, auth traps, or error pages must be quarantined with score 0.2."""
        auth_urls = [
            "https://portal.company.no/login",
            "https://portal.company.no/logg-inn",
            "https://identity.company.no/auth?login_required=true",
            "https://company.no/index.html?error=true",
            "https://company.no/app/login?next=/dashboard",
        ]

        for url in auth_urls:
            profile = {
                "organisation_number": "923456789",
                "name": "Cloud Security AS",
                "evidence": {
                    "website": {
                        "status": "available",
                        "value": {
                            "final_url": url,
                            "title": "Cloud Security AS - Sign In",
                            "main_text_excerpt": "Cloud Security AS portal login page",
                        },
                    }
                },
            }
            res = assess_website_identity(profile)
            self.assertFalse(res["publishable"], f"Login URL '{url}' was marked publishable!")
            self.assertEqual(res["score"], 0.2, f"Login URL '{url}' did not get score 0.2")
            self.assertEqual(res["status"], "related_or_uncertain")
            self.assertTrue(any("login wall" in r or "error" in r for r in res["reasons"]))

    def test_partial_name_overlap_strictly_quarantined_under_0_90(self) -> None:
        """Partial name overlap (ratio 0.75-0.89) must get status 'review' and publishable=False."""
        # 3 out of 4 tokens match: "Clean Energy Solutions Norway AS" (core: clean, energy, solutions, norway)
        # Website has: "Clean Energy Technology Norway" (clean, energy, norway) -> 3/4 = 0.75
        profile = {
            "organisation_number": "934567890",
            "name": "Clean Energy Solutions Norway AS",
            "evidence": {
                "website": {
                    "status": "available",
                    "value": {
                        "final_url": "https://cleanenergytech.no",
                        "title": "Clean Energy Technology Norway",
                        "main_text_excerpt": "We deliver clean energy systems in Norway with modern solutions.",
                    },
                }
            },
        }
        res = assess_website_identity(profile)
        # Even with partial overlap, score is 0.85 (review) and publishable MUST BE False!
        self.assertFalse(res["publishable"])
        self.assertEqual(res["score"], 0.85)
        self.assertEqual(res["status"], "review")
        self.assertTrue(any("exact identity is incomplete" in r for r in res["reasons"]))

    def test_foreign_branch_vs_parent_name_collision(self) -> None:
        """A Norwegian NUF branch must not publish parent entity sites without branch identity or matching orgnr."""
        # Foreign branch registered in Brreg as NUF: "Siemens Healthineers NUF" (core: siemens, healthineers)
        # Candidate homepage is Global parent: "Siemens AG" with German headquarters or general info
        nuf_profile = {
            "organisation_number": "991234567",
            "name": "Siemens Healthineers NUF",
            "legal_form": "NUF",
            "evidence": {
                "website": {
                    "status": "available",
                    "value": {
                        "final_url": "https://www.siemens.com/global/en.html",
                        "title": "Siemens Global - Technology for industry",
                        "main_text_excerpt": "Siemens AG is a German multinational technology conglomerate.",
                    },
                }
            },
        }
        res = assess_website_identity(nuf_profile)
        # Only 'siemens' matches (1/2 tokens = 0.50 ratio), 'healthineers' missing
        self.assertFalse(res["publishable"])
        self.assertLessEqual(res["score"], 0.65)
        self.assertEqual(res["status"], "related_or_uncertain")

    def test_bbl_co_op_portal_gating_minside_rejected(self) -> None:
        """BBL housing co-op portals are verified for root portals, but private tenant login (minside) is blocked."""
        base_coop = {
            "organisation_number": "945678901",
            "name": "Solheimen Borettslag",
            "legal_form": "BRL",
            "website": "https://www.bori.no",
            "evidence": {
                "registry": {
                    "value": {
                        "organisasjonsform": {"kode": "BRL"},
                        "hjemmeside": "https://www.bori.no",
                    }
                },
                "website": {
                    "status": "available",
                    "value": {
                        "final_url": "https://www.bori.no",
                        "title": "BORI Boligbyggelag - Offisiell forvalterportal",
                        "main_text_excerpt": "BORI er boligbyggelaget som forvalter borettslag og sameier over hele Romerike.",
                    },
                },
            },
        }
        # Official declared portal passes with 0.91
        res_portal = assess_website_identity(base_coop)
        self.assertTrue(res_portal["publishable"])
        self.assertEqual(res_portal["score"], 0.91)

        # But minside subdomain MUST be rejected
        minside_coop = dict(base_coop)
        minside_coop["evidence"] = {
            "registry": base_coop["evidence"]["registry"],
            "website": {
                "status": "available",
                "value": {
                    "final_url": "https://minside.bori.no/login",
                    "title": "Min Side BORI Login",
                    "main_text_excerpt": "Logg inn på min side for beboere i borettslag og sameier.",
                },
            },
        }
        res_minside = assess_website_identity(minside_coop)
        self.assertFalse(res_minside["publishable"])
        self.assertLessEqual(res_minside["score"], 0.3)

    def test_quarantine_enforcement_on_unverified_website(self) -> None:
        """When a website is in 'review' (0.85) or 'related_or_uncertain' (0.65), 100% of social links MUST be quarantined."""
        profile = {
            "organisation_number": "956789012",
            "name": "Bergen Shipping & Logistics AS",
            "evidence": {},
        }
        # Website has partial match
        candidate_site = {
            "status": "available",
            "value": {
                "final_url": "https://bergenshipping.no",
                "title": "Bergen Shipping AS",
                "main_text_excerpt": "Bergen Shipping fleet management and global marine services.",
                "social_links": [
                    {"platform": "linkedin", "url": "https://linkedin.com/company/bergenshipping"},
                    {"platform": "facebook", "url": "https://facebook.com/bergenshipping"},
                ],
            },
        }
        gate_res = apply_website_identity_gate(profile, candidate_site)

        # Website assessment is not publishable
        self.assertFalse(gate_res["assessment"]["publishable"])
        self.assertIn(gate_res["assessment"]["status"], {"review", "related_or_uncertain"})

        # Quarantined social links MUST BE EXACTLY 2, and published social_links MUST BE EMPTY!
        self.assertEqual(gate_res["quarantined_social_links"], 2)
        self.assertEqual(gate_res["website"]["value"]["social_links"], [])
        self.assertEqual(len(gate_res["website"]["value"]["discovered_social_links"]), 2)

    def test_social_identity_rejects_generic_platform_pages_and_share_dialogs(self) -> None:
        """Generic platform URLs (/share, /sharer, /intent/tweet, /login) must be rejected."""
        generic_links = [
            {"platform": "facebook", "url": "https://www.facebook.com/sharer/sharer.php?u=https://acme.no"},
            {"platform": "twitter", "url": "https://twitter.com/intent/tweet?text=Check+this+out"},
            {"platform": "linkedin", "url": "https://www.linkedin.com/login"},
            {"platform": "instagram", "url": "https://www.instagram.com/explore"},
        ]
        profile = {"name": "Acme Industrial Technologies AS"}
        for link in generic_links:
            res = assess_social_identity(profile, link)
            self.assertFalse(res["publishable"])
            self.assertEqual(res["identity_score"], 0.1)
            self.assertIn("generic platform page", res["reason"])

    def test_social_identity_rejects_parent_handle_for_subsidiary(self) -> None:
        """Parent company social handle (e.g. linkedin.com/company/dnb) must NOT publish for DNB Næringsmegling AS."""
        profile = {"name": "DNB Næringsmegling AS"}
        parent_link = {"platform": "linkedin", "url": "https://linkedin.com/company/dnb"}
        res = assess_social_identity(profile, parent_link)
        # 'dnb' is 1 of 2 core tokens (50%), not exact match
        self.assertFalse(res["publishable"])
        self.assertLessEqual(res["identity_score"], 0.3)


class EmpiricalBudgetEnforcementStressTests(unittest.TestCase):
    """Area 2: Adversarial Multi-Threaded Concurrency & Quota Enforcement on BudgetTracker."""

    def test_high_contention_32_threads_atomic_record_request(self) -> None:
        """32 concurrent threads executing 100 record_request calls must produce zero lost updates or race conditions."""
        tracker = BudgetTracker(max_requests=10000, max_cost_usd=500.0)
        num_threads = 32
        ops_per_thread = 100
        cost_per_op = 0.002
        bytes_per_op = 1024
        latency_ms = 45

        def worker(thread_id: int) -> None:
            for _ in range(ops_per_thread):
                tracker.record_request(
                    cost_usd=cost_per_op,
                    bytes_received=bytes_per_op,
                    latency_ms=latency_ms,
                    count=1,
                )

        with concurrent.futures.ThreadPoolExecutor(max_workers=num_threads) as executor:
            futures = [executor.submit(worker, t) for t in range(num_threads)]
            for f in futures:
                f.result()

        total_ops = num_threads * ops_per_thread
        expected_cost = round(total_ops * cost_per_op, 4)
        expected_bytes = total_ops * bytes_per_op

        # Verify atomic consistency
        rep = tracker.report()
        self.assertEqual(rep["total_requests"], total_ops, f"Expected {total_ops} requests, got {rep['total_requests']}")
        self.assertAlmostEqual(rep["total_cost_usd"], expected_cost, places=3)
        self.assertEqual(rep["total_bytes"], expected_bytes)
        self.assertEqual(len(tracker.latencies_ms), total_ops)
        self.assertEqual(rep["remaining_requests"], 10000 - total_ops)

    def test_concurrent_can_proceed_exhaustion_boundary(self) -> None:
        """Once request or cost quota is reached, can_proceed() must return False across all concurrent threads."""
        tracker = BudgetTracker(max_requests=50, max_cost_usd=1.0)
        # Advance requests to 50
        tracker.record_request(cost_usd=0.5, count=50)

        # 20 concurrent threads check can_proceed()
        def check_proceed(thread_id: int) -> bool:
            return tracker.can_proceed()

        with concurrent.futures.ThreadPoolExecutor(max_workers=20) as executor:
            results = list(executor.map(check_proceed, range(20)))

        self.assertTrue(all(r is False for r in results), "can_proceed() returned True after quota exhaustion!")
        self.assertEqual(tracker.remaining_requests(), 0)

    def test_cost_exhaustion_blocks_can_proceed(self) -> None:
        """When cost exceeds max_cost_usd, can_proceed() must return False even if requests remain."""
        tracker = BudgetTracker(max_requests=1000, max_cost_usd=5.0)
        tracker.record_request(cost_usd=5.01, count=10)

        self.assertFalse(tracker.can_proceed())
        self.assertEqual(tracker.remaining_cost(), 0.0)

    def test_wall_clock_time_exhaustion_blocks_can_proceed(self) -> None:
        """When elapsed time exceeds max_time_seconds, can_proceed() must return False."""
        tracker = BudgetTracker(max_requests=1000, max_cost_usd=10.0, max_time_seconds=0.05)
        time.sleep(0.08)  # Wait for timeout to expire
        self.assertFalse(tracker.can_proceed())

    def test_statutory_brreg_calls_protected_under_exhaustion(self) -> None:
        """In production mode (max_requests > 50), statutory Brreg calls bypass exhaustion, while non-Brreg are blocked."""
        tracker = BudgetTracker(max_requests=100, max_cost_usd=2.0)
        # Exhaust the budget completely
        tracker.record_request(cost_usd=2.5, count=100)
        self.assertFalse(tracker.can_proceed())

        # Mock _raw_fetch_json to verify whether network fetch is reached
        fake_brreg_result = FetchResult(
            url="https://data.brreg.no/enhetsregisteret/api/enheter/985589003",
            status=200,
            elapsed_ms=30,
            bytes_received=2500,
            body={"organisasjonsnummer": "985589003", "navn": "VIKØREN BYGG AS"},
        )

        with patch("norway_company_agent.budget._raw_fetch_json", return_value=fake_brreg_result) as mock_fetch:
            # 1. Non-Brreg request (e.g. Brave Search or NAV or external site)
            external_url = "https://api.search.brave.com/res/v1/web/search?q=test"
            ext_res = tracker.fetch_json(external_url)

            # Must be BLOCKED immediately with error='budget_exhausted' and NOT invoke raw fetch
            self.assertEqual(ext_res.error, "budget_exhausted")
            self.assertEqual(ext_res.status, 0)
            mock_fetch.assert_not_called()

            # 2. Statutory Brreg request
            brreg_url = "https://data.brreg.no/enhetsregisteret/api/enheter/985589003"
            brreg_res = tracker.fetch_json(brreg_url)

            # Statutory call MUST succeed and invoke raw fetch
            mock_fetch.assert_called_once_with(brreg_url, timeout=20.0, attempts=3)
            self.assertEqual(brreg_res.status, 200)
            self.assertIsNone(brreg_res.error)

    def test_test_mode_brreg_calls_not_protected_when_max_requests_le_50(self) -> None:
        """In unit test mode (max_requests <= 50), Brreg calls are NOT protected and honor hard limits."""
        tracker = BudgetTracker(max_requests=10, max_cost_usd=1.0)
        tracker.record_request(count=10)
        self.assertFalse(tracker.can_proceed())

        brreg_url = "https://data.brreg.no/enhetsregisteret/api/enheter/985589003"
        with patch("norway_company_agent.budget._raw_fetch_json") as mock_fetch:
            res = tracker.fetch_json(brreg_url)
            self.assertEqual(res.error, "budget_exhausted")
            mock_fetch.assert_not_called()

    def test_vulnerability_toctou_concurrency_race_condition_in_fetch_json(self) -> None:
        """EMPIRICAL FINDING: TOCTOU race in fetch_json allows concurrent workers to breach max_requests quota.
        
        can_proceed() checks the budget before the network request is initiated, but does not reserve a slot.
        Concurrent threads all pass can_proceed() simultaneously while requests are in flight,
        causing outbound network calls to double or triple max_requests.
        """
        tracker = BudgetTracker(max_requests=10, max_cost_usd=1.0)

        def slow_network_fetch(url: str, timeout: float = 20.0, attempts: int = 3) -> FetchResult:
            time.sleep(0.04)  # Simulate 40ms network I/O
            return FetchResult(url, 200, 40, 512, body={"ok": True})

        with patch("norway_company_agent.budget._raw_fetch_json", side_effect=slow_network_fetch):
            def request_task(idx: int) -> FetchResult:
                return tracker.fetch_json(f"https://external-api.com/req/{idx}")

            with concurrent.futures.ThreadPoolExecutor(max_workers=20) as executor:
                results = list(executor.map(request_task, range(20)))

            successful_calls = sum(1 for r in results if r.error is None)
            blocked_calls = sum(1 for r in results if r.error == "budget_exhausted")

            # Empirically proves that 20 calls succeeded against a budget of 10!
            self.assertGreater(successful_calls, tracker.max_requests, "Demonstrates TOCTOU budget overrun")
            self.assertEqual(tracker.requests, 20)
            self.assertEqual(blocked_calls, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
