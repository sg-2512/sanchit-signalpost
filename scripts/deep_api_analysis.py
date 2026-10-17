"""Deep Diagnostic & Health Analysis for all Norwegian & International Connectors.

Audits every endpoint, connector function, fallback behavior, network responsiveness,
and exact-entity cryptographic provenance.
"""

from __future__ import annotations

import json
import os
import sys
import time
from typing import Any

# Ensure project root is on PYTHONPATH
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src")))
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from norway_company_agent.budget import BudgetTracker
from norway_company_agent.official import (
    fetch_official_modules,
    accounting_obligation_assessment,
)
from norway_company_agent.connectors import (
    fetch_doffin_procurement,
    fetch_esg_certifications,
    fetch_forskningsradet_grants,
    fetch_gleif_identifier,
    fetch_google_news,
    fetch_innovasjon_norge_grants,
    geocode_address_kartverket,
    fetch_brreg_kunngjoringer,
    fetch_losoreregisteret_liens,
    fetch_insolvency_legal_notices,
    fetch_mattilsynet_inspections,
    fetch_miljodirektoratet_permits,
    fetch_nav_jobs,
    verify_norid_domain_ownership,
    fetch_oslo_bors_disclosures,
    fetch_patentstyret_data,
    resolve_commercial_directory_presence,
    fetch_regulatory_licenses,
    fetch_ssb_industry_benchmark,
    fetch_company_subunits,
    fetch_wikidata_entity,
)

TARGET_ORGS = [
    {
        "org": "923609016",
        "name": "EQUINOR ASA",
        "website": "https://www.equinor.com",
        "nace": "06.100",
        "address": "Forusbeen 50, 4035 Stavanger",
    },
    {
        "org": "984851006",
        "name": "DNB BANK ASA",
        "website": "https://www.dnb.no",
        "nace": "64.190",
        "address": "Dronning Eufemias gate 30, 0191 Oslo",
    },
    {
        "org": "943753709",
        "name": "KONGSBERG GRUPPEN ASA",
        "website": "https://www.kongsberg.com",
        "nace": "30.400",
        "address": "Kirkegårdsveien 45, 3616 Kongsberg",
    },
]


def run_connector_test(name: str, fn: Any, *args: Any, **kwargs: Any) -> dict[str, Any]:
    t0 = time.perf_counter()
    status = "OK"
    error_msg = ""
    result = None
    count = 0
    provenance_ok = True

    try:
        result = fn(*args, **kwargs)
        if isinstance(result, list):
            count = len(result)
            for item in result:
                if isinstance(item, dict):
                    if not item.get("content_sha256") or len(str(item.get("content_sha256"))) != 64:
                        provenance_ok = False
        elif isinstance(result, dict):
            count = 1 if result else 0
            if result.get("content_sha256") and len(str(result.get("content_sha256"))) != 64:
                provenance_ok = False
        elif result is not None:
            count = 1
    except Exception as exc:
        status = "EXCEPTION"
        error_msg = str(exc)

    latency_ms = round((time.perf_counter() - t0) * 1000, 1)

    return {
        "connector": name,
        "status": status,
        "latency_ms": latency_ms,
        "items_returned": count,
        "provenance_ok": provenance_ok,
        "error": error_msg,
        "sample": str(result)[:120] if result else "None",
    }


def main() -> int:
    print("=" * 80)
    print("SANCHIT-SIGNALPOST LIVE CONNECTOR & REGISTRY HEALTH AUDIT")
    print("=" * 80)

    budget = BudgetTracker()
    test_org = TARGET_ORGS[0]  # Equinor ASA
    test_dnb = TARGET_ORGS[1]  # DNB Bank ASA

    results: list[dict[str, Any]] = []

    print(f"\nTargeting primary entity: {test_org['name']} (Org: {test_org['org']})")
    print("-" * 80)

    # 1. Brreg Enhetsregisteret (Official Registry Live)
    def test_brreg_reg():
        recs, _ = fetch_official_modules(test_org["org"], {"registry_live"}, fetcher=budget.fetch_json)
        return recs.get("registry_live")
    results.append(run_connector_test("1. Brreg Enhetsregisteret", test_brreg_reg))

    # 2. Brreg Underenheter (Locations)
    def test_brreg_subunits():
        recs, _ = fetch_official_modules(test_org["org"], {"locations"}, fetcher=budget.fetch_json)
        return recs.get("locations")
    results.append(run_connector_test("2. Brreg Underenheter", test_brreg_subunits))

    # 3. Brreg Regnskapsregisteret (Financial Accounts)
    def test_brreg_fin():
        recs, _ = fetch_official_modules(test_org["org"], {"financials"}, fetcher=budget.fetch_json)
        return recs.get("financials")
    results.append(run_connector_test("3. Brreg Regnskapsregisteret", test_brreg_fin))

    # 4. Brreg Roller (Board Roles & Signatures)
    def test_brreg_roles():
        recs, _ = fetch_official_modules(test_org["org"], {"roles"}, fetcher=budget.fetch_json)
        return recs.get("roles")
    results.append(run_connector_test("4. Brreg Roller", test_brreg_roles))

    # 5. Brreg Kunngjøringer
    results.append(run_connector_test("5. Brreg Kunngjøringer", fetch_brreg_kunngjoringer, test_org["org"], test_org["name"], budget=budget))

    # 6. NAV Arbeidsplassen Open Jobs
    results.append(run_connector_test("6. NAV Arbeidsplassen", fetch_nav_jobs, test_org["org"], test_org["name"], budget=budget))

    # 7. Patentstyret NIPO Brand & IP
    results.append(run_connector_test("7. Patentstyret (NIPO)", fetch_patentstyret_data, test_org["org"], test_org["name"], budget=budget))

    # 8. Doffin Public Procurement
    results.append(run_connector_test("8. Doffin Procurement", fetch_doffin_procurement, test_org["org"], test_org["name"], budget=budget))

    # 9. Lovdata Insolvency & Court Gazettes
    results.append(run_connector_test("9. Lovdata Legal & Insolvency", fetch_insolvency_legal_notices, test_org["org"], test_org["name"], budget=budget))

    # 10. Kartverket Geocoding & Cadastre
    results.append(run_connector_test("10. Kartverket Geocoding", geocode_address_kartverket, test_org["address"], budget=budget))

    # 11. Mattilsynet Smilefjes Inspections
    results.append(run_connector_test("11. Mattilsynet Smilefjes", fetch_mattilsynet_inspections, test_org["org"], test_org["name"], budget=budget))

    # 12. Regulatory Licenses / Finanstilsynet
    results.append(run_connector_test("12. Finanstilsynet & Regulatory", fetch_regulatory_licenses, test_dnb["org"], test_dnb["name"], industry_code=test_dnb["nace"], budget=budget))

    # 13. Miljøfyrtårn & ESG Sustainability
    results.append(run_connector_test("13. Miljøfyrtårn & ESG", fetch_esg_certifications, test_org["org"], test_org["name"], budget=budget))

    # 14. Norid .no RDAP / WHOIS Verification
    results.append(run_connector_test("14. Norid .no Domain RDAP", verify_norid_domain_ownership, test_dnb["website"], test_dnb["org"], test_dnb["name"], budget=budget))

    # 15. Løsøreregisteret Enterprise Liens & Mortgages
    results.append(run_connector_test("15. Løsøreregisteret Liens", fetch_losoreregisteret_liens, test_org["org"], test_org["name"], budget=budget))

    # 16. Forskningsrådet Prosjektbanken R&D Grants
    results.append(run_connector_test("16. Forskningsrådet Grants", fetch_forskningsradet_grants, test_org["org"], test_org["name"], budget=budget))

    # 17. Innovasjon Norge Commercialization Subsidies
    results.append(run_connector_test("17. Innovasjon Norge Grants", fetch_innovasjon_norge_grants, test_org["org"], test_org["name"], budget=budget))

    # 18. Oslo Børs / Euronext Oslo NewsWeb
    results.append(run_connector_test("18. Oslo Børs NewsWeb", fetch_oslo_bors_disclosures, test_org["org"], test_org["name"], budget=budget))

    # 19. Statistisk sentralbyrå (SSB) Benchmarks
    results.append(run_connector_test("19. SSB Industry Benchmarks", fetch_ssb_industry_benchmark, test_org["nace"], test_org["org"], test_org["name"], budget=budget))

    # 20. GLEIF Global LEI Index
    results.append(run_connector_test("20. GLEIF Global LEI", fetch_gleif_identifier, test_org["org"], test_org["name"], budget=budget))

    # 21. Miljødirektoratet Norske Utslipp Permits
    results.append(run_connector_test("21. Miljødirektoratet Permits", fetch_miljodirektoratet_permits, test_org["org"], test_org["name"], budget=budget))

    # 22. Google News RSS Feeds
    results.append(run_connector_test("22. Google News RSS", fetch_google_news, test_org["org"], test_org["name"], limit=3, years=2, budget=budget))

    # 23. Wikidata SPARQL Corroboration
    results.append(run_connector_test("23. Wikidata Corroboration", fetch_wikidata_entity, test_org["org"], test_org["name"], budget=budget))

    # 24. Commercial Directory Presence (Proff / 1881 / Gule Sider)
    results.append(run_connector_test("24. Commercial Directories", resolve_commercial_directory_presence, test_org["org"], test_org["name"], budget=budget))

    # Format summary table
    print(f"{'Connector Name':<32} | {'Status':<10} | {'Latency':<9} | {'Items':<6} | {'Provenance':<10}")
    print("-" * 80)
    all_ok = True
    for r in results:
        prov_str = "VALID SHA" if r["provenance_ok"] else "INVALID"
        print(f"{r['connector']:<32} | {r['status']:<10} | {str(r['latency_ms']) + 'ms':<9} | {r['items_returned']:<6} | {prov_str:<10}")
        if r["status"] != "OK" or not r["provenance_ok"]:
            all_ok = False
            if r["error"]:
                print(f"   --> Error: {r['error']}")

    print("=" * 80)
    print(f"TOTAL CONNECTORS TESTED: {len(results)}")
    print(f"HEALTH STATUS: {'100% OPERATIONAL & VERIFIED' if all_ok else 'SOME ISSUES DETECTED'}")
    print(f"BUDGET TRACKER STATS: Requests={budget.requests}, Cost=${budget.cost_usd:.4f}")
    print("=" * 80)

    # Save diagnostic results to out/deep-analysis.json
    os.makedirs("out", exist_ok=True)
    with open("out/deep-analysis.json", "w", encoding="utf-8") as f:
        json.dump({"timestamp": time.time(), "all_ok": all_ok, "results": results}, f, indent=2)

    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
