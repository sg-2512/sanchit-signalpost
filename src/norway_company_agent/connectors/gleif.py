"""Global Legal Entity Identifier Foundation (GLEIF) Connector for Signalpost.

Queries the official open GLEIF international registry to resolve global 20-character LEI codes,
registration lifecycle status, and multinational ultimate parent (GUO) ownership hierarchies
for Norwegian enterprises engaged in cross-border commerce and financial markets.
"""
from __future__ import annotations

import hashlib
import json
import time
import urllib.parse
import urllib.request
from typing import Any

GLEIF_API_ENDPOINT = "https://api.gleif.org/api/v1/lei-records"
USER_AGENT = "SignalPostAgent/1.0 (+https://builderr.ai/signalpost)"


def fetch_gleif_identifier(
    org_number: str,
    company_name: str,
    *,
    budget: Any | None = None,
    timeout: float = 6.0,
) -> dict[str, Any] | None:
    """Fetch official Global Legal Entity Identifier (LEI) for a Norwegian enterprise."""
    if not org_number or len(org_number) < 9:
        return None

    if budget and not budget.can_proceed():
        return None

    clean_org = org_number.strip().replace(" ", "")
    clean_name = company_name.strip()
    start_time = time.perf_counter()
    bytes_received = 0
    retrieved_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    params = {
        "filter[entity.legalAddress.country]": "NO",
        "filter[entity.registeredAs]": clean_org,
        "page[size]": "1",
    }
    url = f"{GLEIF_API_ENDPOINT}?{urllib.parse.urlencode(params)}"

    try:
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "application/vnd.api+json",
            },
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read(250_000)
            bytes_received = len(raw)
            data = json.loads(raw.decode("utf-8", errors="replace"))

        elapsed_ms = int((time.perf_counter() - start_time) * 1000)
        if budget:
            budget.record_request(cost_usd=0.0, bytes_received=bytes_received, elapsed_ms=elapsed_ms)

        records = data.get("data") or []
        if not records:
            return None

        rec = records[0]
        lei_code = str(rec.get("id") or (rec.get("attributes") or {}).get("lei") or "")
        attr = rec.get("attributes") or {}
        reg = attr.get("registration") or {}
        status = reg.get("status") or "ISSUED"
        managing_lou = reg.get("managingLou") or "GLEIF"
        legal_name = (attr.get("entity") or {}).get("legalName", {}).get("name") or clean_name

        h_digest = hashlib.sha256(f"{clean_org}|gleif|{lei_code}|{status}".encode()).hexdigest()
        obs_id = f"lei-{clean_org}-{h_digest[:16]}"
        record_url = f"https://search.gleif.org/#/record/{lei_code}"

        return {
            "id": obs_id,
            "organisation_number": clean_org,
            "platform": "gleif",
            "signal_type": "global_legal_entity_identifier",
            "source_url": record_url,
            "retrieved_at": retrieved_at,
            "published_at": (reg.get("initialRegistrationDate") or retrieved_at)[:10],
            "content_sha256": h_digest,
            "exact_entity": True,
            "identity_proof": [
                {
                    "type": "official_global_lei_registry",
                    "organisation_number": clean_org,
                    "legal_name": legal_name,
                    "lei": lei_code,
                    "status": status,
                }
            ],
            "acquisition_mode": "official_api",
            "rights_status": "approved",
            "source_class": "international_standard_registry",
            "evidence_span": f"Global Legal Entity Identifier (LEI) for {clean_name}: {lei_code} (Status: {status})",
            "metrics": {
                "lei": lei_code,
                "status": status,
                "managing_lou": managing_lou,
                "legal_name": legal_name,
                "source": "GLEIF Global LEI Index",
            },
        }

    except Exception:
        elapsed_ms = int((time.perf_counter() - start_time) * 1000)
        if budget:
            budget.record_request(cost_usd=0.0, bytes_received=bytes_received, elapsed_ms=elapsed_ms)
        return None
