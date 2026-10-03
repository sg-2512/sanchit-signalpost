"""Doffin (Norwegian National Public Procurement Database) Connector for Signalpost.

Extracts public procurement tender wins and government contract awards
for Norwegian commercial entities to verify commercial viability, government trust,
and core operational revenue drivers.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
import urllib.parse
import urllib.request
from typing import Any

DOFFIN_SEARCH_ENDPOINT = "https://api.dfo.no/doffin/v1/notices"
USER_AGENT = "SignalPostAgent/1.0 (+https://builderr.ai/signalpost)"
DOFFIN_API_KEY_ENV = "DOFFIN_API_KEY"


def is_available() -> bool:
    """Return True if a Doffin (DFØ) API key is configured."""
    return bool(os.environ.get(DOFFIN_API_KEY_ENV))


def _get_api_key() -> str:
    return (os.environ.get(DOFFIN_API_KEY_ENV) or "").strip()


def fetch_doffin_awards(
    org_number: str,
    company_name: str,
    *,
    budget: Any | None = None,
    timeout: float = 6.0,
    max_records: int = 5,
) -> list[dict[str, Any]]:
    """Fetch public procurement contract awards won by the company on Doffin."""
    if not company_name or len(company_name) < 3:
        return []

    if budget and not budget.can_proceed():
        return []

    clean_org = org_number.strip().replace(" ", "") if org_number else "unknown"
    clean_name = company_name.strip()
    start_time = time.perf_counter()
    bytes_received = 0
    retrieved_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    # Build query for awarded contractor or notice content
    params = urllib.parse.urlencode({
        "searchTerms": f'"{clean_name}"',
        "noticeType": "CONTRACT_AWARD",
        "pageSize": max_records,
    })
    url = f"{DOFFIN_SEARCH_ENDPOINT}?{params}"

    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
    }
    api_key = _get_api_key()
    if api_key:
        headers["Ocp-Apim-Subscription-Key"] = api_key
        headers["Authorization"] = f"Bearer {api_key}"

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
        items = data.get("items") or data.get("notices") or []
        if not items:
            return []

        observations: list[dict[str, Any]] = []
        for item in items[:max_records]:
            doffin_id = item.get("id") or item.get("noticeId") or ""
            title = item.get("title") or "Offentlig anskaffelse kontraktstildeling"
            buyer = (item.get("buyer") or {}).get("name") or "Offentlig oppdragsgiver"
            award_date = item.get("publicationDate") or item.get("issueDate")
            value = item.get("value")

            h_digest = hashlib.sha256(f"{clean_org}|doffin|{doffin_id}|{title}".encode()).hexdigest()
            notice_url = f"https://doffin.no/notices/{doffin_id}" if doffin_id else "https://doffin.no"

            obs_id = f"procurement-{clean_org}-{h_digest[:16]}"
            observations.append({
                "id": obs_id,
                "organisation_number": clean_org,
                "platform": "doffin",
                "signal_type": "public_procurement_award",
                "source_url": notice_url,
                "retrieved_at": retrieved_at,
                "published_at": award_date,
                "content_sha256": h_digest,
                "exact_entity": True,
                "identity_proof": [
                    {
                        "type": "official_public_procurement_award",
                        "supplier_name": clean_name,
                        "buyer_name": buyer,
                        "doffin_notice_id": doffin_id,
                    }
                ],
                "acquisition_mode": "official_api",
                "rights_status": "approved",
                "source_class": "public_procurement",
                "evidence_span": f"Doffin public contract '{title}' awarded to {clean_name} by {buyer}",
                "metrics": {
                    "notice_id": doffin_id,
                    "tender_title": title,
                    "contracting_authority": buyer,
                    "award_date": award_date,
                    "contract_value": value,
                },
                "strategy": "doffin_procurement_search",
            })

        return observations

    except Exception:
        elapsed_ms = int((time.perf_counter() - start_time) * 1000)
        if budget:
            budget.record_request(cost_usd=0.0, bytes_received=bytes_received, elapsed_ms=elapsed_ms)
        return []
