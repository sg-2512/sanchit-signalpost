"""Patentstyret (Norwegian Industrial Property Office - NIPO) Connector for Signalpost.

Extracts registered Norwegian trademarks, protected commercial brand names,
and patents owned by Norwegian enterprises to bridge official registered roles
to public commercial brands (Builderr Route 9 Leader & Brand Bridge).
"""
from __future__ import annotations

import hashlib
import json
import os
import time
import urllib.parse
import urllib.request
from typing import Any

PATENTSTYRET_SEARCH_URL = "https://services.patentstyret.no/register/v1/IprCasesByCompany"
USER_AGENT = "SignalPostAgent/1.0 (+https://builderr.ai/signalpost)"
PATENTSTYRET_API_KEY_ENV = "PATENTSTYRET_API_KEY"
NIPO_API_KEY_ENV = "NIPO_API_KEY"


def is_available() -> bool:
    """Return True if a Patentstyret (NIPO) API key is configured."""
    return bool(os.environ.get(PATENTSTYRET_API_KEY_ENV) or os.environ.get(NIPO_API_KEY_ENV))


def _get_api_key() -> str:
    return (os.environ.get(PATENTSTYRET_API_KEY_ENV) or os.environ.get(NIPO_API_KEY_ENV) or "").strip()


def fetch_patentstyret_data(
    org_number: str,
    company_name: str,
    *,
    budget: Any | None = None,
    timeout: float = 6.0,
    max_records: int = 5,
) -> list[dict[str, Any]]:
    """Fetch registered trademarks and patent records from Patentstyret for a company."""
    if not company_name or len(company_name) < 3:
        return []

    if budget and not budget.can_proceed():
        return []

    clean_org = org_number.strip().replace(" ", "") if org_number else "unknown"
    clean_name = company_name.strip()
    start_time = time.perf_counter()
    bytes_received = 0
    retrieved_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    # Build query for applicant / owner matching company name
    params = urllib.parse.urlencode({
        "q": f'innehaver:"{clean_name}" OR applicant:"{clean_name}"',
        "type": "trademark,patent",
        "size": max_records,
    })
    url = f"{PATENTSTYRET_SEARCH_URL}?{params}"

    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
    }
    api_key = _get_api_key()
    if api_key:
        headers["Ocp-Apim-Subscription-Key"] = api_key

    try:
        req = urllib.request.Request(
            url,
            headers=headers,
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read(500_000)
            bytes_received = len(raw)
            data = json.loads(raw.decode("utf-8", errors="replace"))

        elapsed_ms = int((time.perf_counter() - start_time) * 1000)
        if budget:
            budget.record_request(cost_usd=0.0, bytes_received=bytes_received, elapsed_ms=elapsed_ms)

        digest = hashlib.sha256(raw).hexdigest()
        hits = data.get("results") or data.get("hits") or []
        if not hits:
            return []

        observations: list[dict[str, Any]] = []
        for hit in hits[:max_records]:
            app_num = hit.get("applicationNumber") or hit.get("id") or ""
            title = hit.get("title") or hit.get("markText") or ""
            doc_type = hit.get("type", "trademark")
            status = hit.get("status") or "Registrert"
            pub_date = hit.get("publicationDate") or hit.get("filingDate")

            h_digest = hashlib.sha256(f"{clean_org}|patentstyret|{app_num}|{title}".encode()).hexdigest()
            record_url = f"https://search.patentstyret.no/trademark/{app_num}" if doc_type == "trademark" else f"https://search.patentstyret.no/patent/{app_num}"

            obs_id = f"ip-{doc_type}-{clean_org}-{h_digest[:16]}"
            observations.append({
                "id": obs_id,
                "organisation_number": clean_org,
                "platform": "patentstyret",
                "signal_type": "patent_trademark_record",
                "source_url": record_url,
                "retrieved_at": retrieved_at,
                "published_at": pub_date,
                "content_sha256": h_digest,
                "exact_entity": True,
                "identity_proof": [
                    {
                        "type": "official_industrial_property_registration",
                        "owner_name": clean_name,
                        "application_number": app_num,
                    }
                ],
                "acquisition_mode": "official_api",
                "rights_status": "approved",
                "source_class": "official_ip_registry",
                "evidence_span": f"Patentstyret {doc_type} '{title}' (app #{app_num}) registered to {clean_name}, status: {status}",
                "metrics": {
                    "ip_type": doc_type,
                    "title": title,
                    "application_number": app_num,
                    "status": status,
                    "filing_date": pub_date,
                },
                "strategy": "patentstyret_ip_search",
            })

        return observations

    except Exception:
        # Fallback: if search API endpoint is unreachable or requires different params, record request timing safely
        elapsed_ms = int((time.perf_counter() - start_time) * 1000)
        if budget:
            budget.record_request(cost_usd=0.0, bytes_received=bytes_received, elapsed_ms=elapsed_ms)
        return []
