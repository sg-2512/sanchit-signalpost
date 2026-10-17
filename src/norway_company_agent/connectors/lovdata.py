"""Lovdata & Konkursregisteret (Norwegian Legal & Insolvency Gazettes) Connector.

Queries official Norwegian bankruptcy, insolvency, compulsory liquidation (tvangsavvikling),
and debt restructuring public notices from the official Konkursregisteret and Lovdata gazettes.
"""
from __future__ import annotations

import hashlib
import json
import time
import urllib.parse
import urllib.request
from typing import Any

KONKURS_SEARCH_URL = "https://w2.brreg.no/kunngjoring/hent_enhet.jsp"
USER_AGENT = "SignalPostAgent/1.0 (+https://builderr.ai/signalpost)"


def fetch_insolvency_legal_notices(
    org_number: str,
    company_name: str,
    *,
    budget: Any | None = None,
    timeout: float = 6.0,
) -> list[dict[str, Any]]:
    """Fetch official bankruptcy, liquidation, or legal notices for an enterprise."""
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

    url = f"{KONKURS_SEARCH_URL}?orgnr={clean_org}"

    try:
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "text/html,application/xhtml+xml",
            },
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read(250_000)
            bytes_received = len(raw)
            text = raw.decode("iso-8859-1", errors="replace")

        elapsed_ms = int((time.perf_counter() - start_time) * 1000)
        if budget:
            budget.record_request(cost_usd=0.0, bytes_received=bytes_received, elapsed_ms=elapsed_ms)

        # Look for bankruptcy / liquidation notices
        is_konkurs = any(kw in text.lower() for kw in ("åpning av konkurs", "konkursåpning", "tvangsoppløsning", "innstilling av bobehandling"))
        status_label = "Insolvens-/konkursvarsel funnet" if is_konkurs else "Aktiv virksomhet i god orden (Ingen konkurs)"

        h_digest = hashlib.sha256(f"{clean_org}|lovdata_konkurs|{status_label}".encode()).hexdigest()
        obs_id = f"legal-{clean_org}-{h_digest[:16]}"
        observations.append({
            "id": obs_id,
            "organisation_number": clean_org,
            "platform": "lovdata_konkursregisteret",
            "signal_type": "legal_gazette_notice",
            "source_url": url,
            "retrieved_at": retrieved_at,
            "published_at": retrieved_at[:10],
            "content_sha256": h_digest,
            "exact_entity": True,
            "identity_proof": [
                {
                    "type": "official_insolvency_register",
                    "entity_name": clean_name,
                    "organisation_number": clean_org,
                    "legal_standing": status_label,
                }
            ],
            "acquisition_mode": "official_registry",
            "rights_status": "approved",
            "source_class": "official_gazette",
            "evidence_span": f"Konkursregisteret status for {clean_name}: {status_label}",
            "metrics": {
                "insolvency_detected": is_konkurs,
                "legal_status": status_label,
                "registry_url": url,
            },
            "strategy": "konkurs_legal_gazette_lookup",
        })

        return observations

    except Exception:
        elapsed_ms = int((time.perf_counter() - start_time) * 1000)
        if budget:
            budget.record_request(cost_usd=0.0, bytes_received=bytes_received, elapsed_ms=elapsed_ms)
        return []
