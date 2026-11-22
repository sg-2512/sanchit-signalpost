import unittest
from bs4 import BeautifulSoup

from norway_company_agent.official import normalize_financials, normalize_group, normalize_roles
from norway_company_agent.website import detect_tech_stack, extract_local_business_schema
from norway_company_agent.connectors.ats_discovery import _extract_ats_jobs
from norway_company_agent.connectors.regulatory_licenses import fetch_regulatory_licenses
from norway_company_agent.connectors.esg_certifications import fetch_esg_certifications
from norway_company_agent.synthesis import generate_company_synthesis


class RoadmapFeaturesTests(unittest.TestCase):
    def test_line_item_financial_extraction(self):
        body = [
            {
                "id": 1,
                "regnskapstype": "SELSKAP",
                "regnskapsperiode": {"fraDato": "2023-01-01", "tilDato": "2023-12-31"},
                "valuta": "NOK",
                "resultatregnskapResultat": {
                    "driftsresultat": {
                        "driftsinntekter": {"sumDriftsinntekter": 10000000},
                        "driftsresultat": 1500000,
                        "driftskostnad": {
                            "sumDriftskostnad": 8500000,
                            "loennskostnad": 4000000,
                        },
                    },
                    "ordinaertResultatFoerSkattekostnad": 1400000,
                    "aarsresultat": 1100000,
                },
                "eiendeler": {
                    "sumEiendeler": 12000000,
                    "omloepsmidler": {
                        "sumOmloepsmidler": 6000000,
                        "bankinnskuddKontanterOgLignende": 2500000,
                    },
                },
                "egenkapitalGjeld": {
                    "egenkapital": {
                        "sumEgenkapital": 7000000,
                        "opptjentEgenkapital": {
                            "sumOpptjentEgenkapital": 4500000,
                        },
                    },
                    "gjeldOversikt": {
                        "sumGjeld": 5000000,
                        "kortsiktigGjeld": {
                            "sumKortsiktigGjeld": 2000000,
                        },
                    },
                },
            }
        ]

        res = normalize_financials(body, employee_count=10)
        recs = res["records"]
        self.assertEqual(len(recs), 1)
        r0 = recs[0]

        # Verify deep line-item extractions
        self.assertEqual(r0["revenue"], 10000000)
        self.assertEqual(r0["operating_costs"], 8500000)
        self.assertEqual(r0["operating_result"], 1500000)
        self.assertEqual(r0["operating_margin"], 0.15)
        self.assertEqual(r0["wage_expenses"], 4000000)
        self.assertNotIn("wage_per_employee", r0)
        self.assertEqual(r0["cash_and_bank_deposits"], 2500000)
        self.assertEqual(r0["retained_earnings"], 4500000)
        self.assertEqual(r0["current_assets"], 6000000)
        self.assertEqual(r0["short_term_debt"], 2000000)
        self.assertEqual(r0["current_ratio"], 3.0)
        self.assertEqual(r0["debt_to_equity"], 0.7143)
        self.assertEqual(r0["solvency_status"], "Strong")
        self.assertEqual(r0["derived_assessment"]["solvency"]["formula"], "equity / assets")

        # Verify trends
        trends = res["financial_trends"]
        self.assertNotIn("wage_per_employee", trends)
        self.assertEqual(trends["current_ratio"], 3.0)
        self.assertEqual(trends["cash_and_bank_deposits"], 2500000)
        self.assertEqual(trends["operating_costs"], 8500000)
        self.assertEqual(trends["retained_earnings"], 4500000)

    def test_group_normalization(self):
        body = {
            "morselskap": {"organisasjonsnummer": "999888777", "navn": "Holding Nordic AS"},
            "datterselskaper": [
                {"organisasjonsnummer": "111222333", "navn": "Sub One AS", "eierandel": 100.0},
                {"organisasjonsnummer": "444555666", "navn": "Sub Two AS", "eierandel": 80.0},
            ],
        }
        res = normalize_group(body)
        self.assertIsNotNone(res["parent"])
        self.assertEqual(res["parent"]["navn"], "Holding Nordic AS")
        self.assertEqual(res["subsidiary_count"], 2)
        self.assertEqual(res["subsidiaries"][0]["name"], "Sub One AS")
        self.assertEqual(res["subsidiaries"][0]["ownership_percentage"], 100.0)

    def test_roles_governance_identification(self):
        body = {
            "rollegrupper": [
                {
                    "type": {"kode": "STYR", "beskrivelse": "Styre"},
                    "roller": [
                        {"type": {"kode": "LEDE", "beskrivelse": "Styrets leder"}, "person": {"navn": {"fornavn": "Kari", "etternavn": "Nordmann"}}},
                        {"type": {"kode": "MEDL", "beskrivelse": "Styremedlem"}, "person": {"navn": {"fornavn": "Per", "etternavn": "Hansen"}}},
                    ],
                },
                {
                    "type": {"kode": "LEDL", "beskrivelse": "Daglig leder"},
                    "roller": [
                        {"type": {"kode": "DAGL", "beskrivelse": "Daglig leder"}, "person": {"navn": {"fornavn": "Ola", "etternavn": "Nordmann"}}},
                    ],
                },
            ]
        }
        res = normalize_roles(body)
        self.assertEqual(res["chair"], "Kari Nordmann")
        self.assertEqual(res["ceo"], "Ola Nordmann")
        self.assertIn("Per Hansen", res["board_members"])

    def test_tech_stack_detection(self):
        html = """
        <html>
        <head><title>Online Butikk</title></head>
        <body>
            <div data-reactroot="">Velkommen</div>
            <script src="https://cdn.shopify.com/s/files/1/shop.js"></script>
            <script src="https://static.vipps.no/checkout.js"></script>
            <script src="https://klarna.com/sdk.js"></script>
        </body>
        </html>
        """
        soup = BeautifulSoup(html, "html.parser")
        tech = detect_tech_stack(html, {"server": "cloudflare"}, soup)
        self.assertIn("Shopify", tech)
        self.assertIn("Vipps", tech)
        self.assertIn("Klarna", tech)
        self.assertIn("React", tech)
        self.assertIn("Cloudflare", tech)

    def test_local_business_schema_extraction(self):
        structured = {
            "json-ld": [
                {
                    "@type": "LocalBusiness",
                    "telephone": "+47 22 00 00 00",
                    "email": "kontakt@butikk.no",
                    "address": {
                        "streetAddress": "Storgata 1",
                        "postalCode": "0182",
                        "addressLocality": "Oslo",
                        "addressCountry": "NO",
                    },
                    "openingHours": "Mo-Fr 08:00-16:00",
                    "priceRange": "$$",
                }
            ]
        }
        res = extract_local_business_schema(structured)
        self.assertEqual(res["telephone"], "+47 22 00 00 00")
        self.assertEqual(res["email"], "kontakt@butikk.no")
        self.assertEqual(res["address"]["locality"], "Oslo")
        self.assertEqual(res["opening_hours"], "Mo-Fr 08:00-16:00")

    def test_ats_schema_org_job_posting_parser(self):
        raw = b"""
        <html>
        <head>
            <script type="application/ld+json">
            {
                "@context": "https://schema.org",
                "@type": "JobPosting",
                "title": "Senior Cloud Software Engineer",
                "url": "https://acme.teamtailor.com/jobs/12345-senior-cloud-engineer",
                "datePosted": "2026-10-15",
                "employmentType": "FULL_TIME",
                "validThrough": "2026-11-30"
            }
            </script>
        </head>
        <body>
            <h1>Careers at Acme</h1>
            <a href="/jobs/12345-senior-cloud-engineer">Senior Cloud Software Engineer</a>
        </body>
        </html>
        """
        jobs = _extract_ats_jobs(raw, "https://acme.teamtailor.com")
        self.assertTrue(len(jobs) >= 1)
        j0 = jobs[0]
        self.assertEqual(j0["title"], "Senior Cloud Software Engineer")
        self.assertEqual(j0["url"], "https://acme.teamtailor.com/jobs/12345-senior-cloud-engineer")
        self.assertEqual(j0.get("date_posted"), "2026-10-15")

    def test_synthesis_with_regulatory_and_esg(self):
        profile = {
            "name": "Nordic Solutions AS",
            "organisation_number": "912345678",
            "legal_form": "AS",
            "municipality": "Oslo",
            "industry_label": "Software Development",
            "employees": 25,
            "website": "https://nordicsolutions.no",
        }
        observations = [
            {
                "signal_type": "regulatory_license_record",
                "metrics": {"authority": "Finanstilsynet", "license_type": "Konsesjon", "status": "Aktiv"},
            },
            {
                "signal_type": "esg_certification_record",
                "metrics": {"standard": "Miljøfyrtårn", "status": "Certified"},
            },
        ]
        res = generate_company_synthesis(profile, observations)
        self.assertIn("Finanstilsynet", res["summary"])
        self.assertIn("Miljøfyrtårn", res["summary"])
        self.assertIn("Nordic Solutions AS", res["summary"])


if __name__ == "__main__":
    unittest.main()
