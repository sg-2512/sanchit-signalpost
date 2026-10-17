"""Doffin (Norwegian Public Procurement Database) Connector for Signalpost.

Queries official Norwegian public tender and contract award notices from Doffin (doffin.no)
for contracts won or published by Norwegian enterprises, providing authoritative B2G/B2B
commercial performance signals.
"""
from __future__ import annotations

import hashlib
import json
import time
import urllib.parse
import urllib.request
from typing import Any

DOFFIN_API_ENDPOINT = "https://api.doffin.no/api/v1/notices"
DOFFIN_PUBLIC_SEARCH = "https://doffin.no/Notice"
USER_AGENT = "SignalPostAgent/1.0 (+https://builderr.ai/signalpost)"


def fetch_doffin_procurement(
    org_number: str,
    company_name: str,
    *,
    budget: Any | None = None,
    timeout: float = 6.0,
    max_records: int = 5,
) -> list[dict[str, Any]]:
    """Fetch official public procurement contract awards and tender notices from Doffin."""
    if not org_number or len(org_number) < 9:
        return []

    if budget and not budget.can_proceed():
        return []

    clean_org = org_number.strip().replace(" ", "")
    clean_name = company_name.strip()
    start_time = time.perf_counter()
    bytes_received = 0
    retrieved_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    observations: list[dict[str, Any]] = []

    # Query Doffin by supplier or buyer organization number
    query_params = {
        "searchQuery": clean_org,
        "page": "1",
        "size": str(max_records),
    }
    url = f"{DOFFIN_API_ENDPOINT}?{urllib.parse.urlencode(query_params)}"

    try:
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "application/json",
            },
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read(500_000)
            bytes_received = len(raw)
            data = json.loads(raw.decode("utf-8", errors="replace"))

        elapsed_ms = int((time.perf_counter() - start_time) * 1000)
        if budget:
            budget.record_request(cost_usd=0.0, bytes_received=bytes_received, elapsed_ms=elapsed_ms)

        hits = data.get("items") or data.get("notices") or data.get("results") or []
        for hit in hits[:max_records]:
            notice_id = str(hit.get("id") or hit.get("noticeId") or hit.get("reference") or "")
            title = hit.get("title") or hit.get("heading") or f"Offentlig kontrakt: {clean_name}"
            notice_type = hit.get("noticeType") or hit.get("type") or "Kontraktstildeling"
            pub_date = hit.get("publicationDate") or hit.get("date") or retrieved_at[:10]
            case_url = hit.get("url") or f"{DOFFIN_PUBLIC_SEARCH}/Details/{notice_id}" if notice_id else DOFFIN_PUBLIC_SEARCH
            val_amount = hit.get("estimatedValue") or hit.get("value")

            h_digest = hashlib.sha256(f"{clean_org}|doffin|{notice_id}|{title}".encode()).hexdigest()
            obs_id = f"procurement-{clean_org}-{h_digest[:16]}"

            observations.append({
                "id": obs_id,
                "organisation_number": clean_org,
                "platform": "doffin",
                "signal_type": "public_procurement_award",
                "source_url": case_url,
                "retrieved_at": retrieved_at,
                "published_at": pub_date,
                "content_sha256": h_digest,
                "exact_entity": True,
                "identity_proof": [
                    {
                        "type": "official_public_procurement_registry",
                        "supplier_name": clean_name,
                        "organisation_number": clean_org,
                        "notice_id": notice_id,
                    }
                ],
                "acquisition_mode": "official_api",
                "rights_status": "approved",
                "source_class": "official_procurement_registry",
                "evidence_span": f"Doffin {notice_type} '{title}' (Notice #{notice_id}) linked to {clean_name} ({clean_org})",
                "metrics": {
                    "notice_id": notice_id,
                    "title": title,
                    "notice_type": notice_type,
                    "publication_date": pub_date,
                    "estimated_value": val_amount,
                },
                "strategy": "doffin_procurement_search",
            })

        return observations

    except Exception:
        elapsed_ms = int((time.perf_counter() - start_time) * 1000)
        if budget:
            budget.record_request(cost_usd=0.0, bytes_received=bytes_received, elapsed_ms=elapsed_ms)
        return []
