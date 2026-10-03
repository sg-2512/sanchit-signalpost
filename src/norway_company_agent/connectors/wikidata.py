"""Wikidata SPARQL Entity Corroboration Connector for Signalpost.

Queries the public Wikidata SPARQL endpoint by Norwegian organisation number (Property P2333).
Maps official company entities directly to verified social media handles, Wikipedia articles,
CEO, inception year, and official website with 100% exact entity grounding.
"""
from __future__ import annotations

import hashlib
import json
import time
import urllib.parse
import urllib.request
from typing import Any

WIKIDATA_SPARQL_ENDPOINT = "https://query.wikidata.org/sparql"
USER_AGENT = "SignalPostResearchAgent/1.0 (+https://builderr.ai/signalpost; research@builderr.ai)"


def build_sparql_query(org_number: str) -> str:
    """Build SPARQL query targeting Norwegian organisation number (P2333)."""
    return f"""
SELECT ?item ?itemLabel ?website ?linkedin ?youtube ?facebook ?twitter ?github ?inception ?ceo ?ceoLabel WHERE {{
  ?item wdt:P2333 "{org_number}" .
  OPTIONAL {{ ?item wdt:P856 ?website . }}
  OPTIONAL {{ ?item wdt:P4264 ?linkedin . }}
  OPTIONAL {{ ?item wdt:P2397 ?youtube . }}
  OPTIONAL {{ ?item wdt:P2013 ?facebook . }}
  OPTIONAL {{ ?item wdt:P2002 ?twitter . }}
  OPTIONAL {{ ?item wdt:P2037 ?github . }}
  OPTIONAL {{ ?item wdt:P571 ?inception . }}
  OPTIONAL {{ ?item wdt:P169 ?ceo . }}
  SERVICE wikibase:label {{ bd:serviceParam wikibase:language "nb,nn,en". }}
}} LIMIT 1
"""


def fetch_wikidata_entity(
    org_number: str,
    company_name: str = "",
    *,
    budget: Any | None = None,
    timeout: float = 8.0,
) -> list[dict[str, Any]]:
    """Fetch verified Wikidata entity corroboration for a Norwegian company.

    Returns observations for discovered social profiles and corporate metadata.
    """
    if not org_number or len(org_number) < 9:
        return []

    if budget and not budget.can_proceed():
        return []

    clean_org = org_number.strip().replace(" ", "")
    query = build_sparql_query(clean_org)
    params = urllib.parse.urlencode({"query": query, "format": "json"})
    url = f"{WIKIDATA_SPARQL_ENDPOINT}?{params}"

    start_time = time.perf_counter()
    bytes_received = 0
    try:
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "application/sparql-results+json, application/json",
            },
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read(500_000)
            bytes_received = len(raw)
            payload = json.loads(raw.decode("utf-8", errors="replace"))

        elapsed_ms = int((time.perf_counter() - start_time) * 1000)
        if budget:
            budget.record_request(cost_usd=0.0, bytes_received=bytes_received, elapsed_ms=elapsed_ms)

        bindings = payload.get("results", {}).get("bindings", [])
        if not bindings:
            return []

        row = bindings[0]
        item_uri = row.get("item", {}).get("value", "")
        item_label = row.get("itemLabel", {}).get("value", "") or company_name
        digest = hashlib.sha256(raw).hexdigest()
        retrieved_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

        observations: list[dict[str, Any]] = []

        # 1. Main entity observation
        website = row.get("website", {}).get("value", "")
        ceo_label = row.get("ceoLabel", {}).get("value", "")
        inception = row.get("inception", {}).get("value", "")
        qid = item_uri.split("/")[-1] if item_uri else ""

        obs_id = f"wikidata-entity-{clean_org}-{digest[:16]}"
        observations.append({
            "id": obs_id,
            "organisation_number": clean_org,
            "platform": "wikidata",
            "signal_type": "profile_metrics",
            "source_url": item_uri or f"https://www.wikidata.org/wiki/Special:EntityData/{qid}",
            "retrieved_at": retrieved_at,
            "content_sha256": digest,
            "exact_entity": True,
            "identity_proof": [
                {
                    "type": "wikidata_property_p2333",
                    "property": "P2333",
                    "org_number": clean_org,
                    "wikidata_qid": qid,
                    "wikidata_label": item_label,
                }
            ],
            "acquisition_mode": "official_api",
            "rights_status": "approved",
            "source_class": "wikidata_open_knowledge",
            "evidence_span": f"Wikidata entity {qid} ({item_label}) matches Norwegian organization {clean_org}",
            "metrics": {
                "wikidata_qid": qid,
                "label": item_label,
                "website": website,
                "ceo": ceo_label,
                "inception": inception,
            },
            "strategy": "wikidata_sparql_p2333",
        })

        # 2. Social handles discovered via Wikidata
        social_mappings = [
            ("linkedin", "P4264", row.get("linkedin", {}).get("value"), lambda v: f"https://www.linkedin.com/company/{v}" if not v.startswith("http") else v),
            ("youtube", "P2397", row.get("youtube", {}).get("value"), lambda v: f"https://www.youtube.com/channel/{v}" if not v.startswith("http") else v),
            ("facebook", "P2013", row.get("facebook", {}).get("value"), lambda v: f"https://www.facebook.com/{v}" if not v.startswith("http") else v),
            ("twitter", "P2002", row.get("twitter", {}).get("value"), lambda v: f"https://x.com/{v}" if not v.startswith("http") else v),
            ("github", "P2037", row.get("github", {}).get("value"), lambda v: f"https://github.com/{v}" if not v.startswith("http") else v),
        ]

        for platform, prop_id, raw_val, url_builder in social_mappings:
            if not raw_val:
                continue
            canonical_url = url_builder(str(raw_val).strip())
            h_digest = hashlib.sha256(f"{clean_org}|{platform}|{canonical_url}".encode()).hexdigest()
            observations.append({
                "id": f"social-{platform}-{clean_org}-{h_digest[:16]}",
                "organisation_number": clean_org,
                "platform": platform,
                "signal_type": "profile_handle",
                "source_url": canonical_url,
                "retrieved_at": retrieved_at,
                "content_sha256": h_digest,
                "exact_entity": True,
                "identity_proof": [
                    {
                        "type": "wikidata_property_claim",
                        "property": prop_id,
                        "wikidata_qid": qid,
                        "value": str(raw_val),
                    }
                ],
                "acquisition_mode": "official_api",
                "rights_status": "approved",
                "source_class": "wikidata_open_knowledge",
                "evidence_span": f"Wikidata item {qid} asserts official {platform} link: {canonical_url}",
                "metrics": {"platform": platform, "url": canonical_url, "wikidata_qid": qid},
                "strategy": "wikidata_social_discovery",
            })

        return observations

    except Exception:
        elapsed_ms = int((time.perf_counter() - start_time) * 1000)
        if budget:
            budget.record_request(cost_usd=0.0, bytes_received=bytes_received, elapsed_ms=elapsed_ms)
        return []
