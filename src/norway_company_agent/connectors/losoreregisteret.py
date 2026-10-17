"""Løsøreregisteret (Register of Mortgages and Liens on Movable Property) Connector.

Queries official Brønnøysund Løsøreregisteret records to discover active enterprise mortgages
(driftstilbehørspant), inventory/factoring liens (varelagerpant, factoringpant), and distraint
notices (utleggsforretning), providing vital commercial solvency and debt collateral intelligence.
"""
from __future__ import annotations

import hashlib
import json
import time
import urllib.parse
import urllib.request
from typing import Any

LOSORE_API_ENDPOINT = "https://data.brreg.no/losore/api/pant/enhet"
USER_AGENT = "SignalPostAgent/1.0 (+https://builderr.ai/signalpost)"


def fetch_losoreregisteret_liens(
    org_number: str,
    company_name: str,
    *,
    budget: Any | None = None,
    timeout: float = 6.0,
    max_records: int = 5,
) -> list[dict[str, Any]]:
    """Fetch official mortgage and lien records from Brønnøysund Løsøreregisteret."""
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

    url = f"{LOSORE_API_ENDPOINT}/{clean_org}"

    try:
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "application/json",
            },
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read(300_000)
            bytes_received = len(raw)
            data = json.loads(raw.decode("utf-8", errors="replace"))

        elapsed_ms = int((time.perf_counter() - start_time) * 1000)
        if budget:
            budget.record_request(cost_usd=0.0, bytes_received=bytes_received, elapsed_ms=elapsed_ms)

        heftelser = data.get("heftelser") or data.get("tinglysteHeftelser") or data.get("items") or []
        for heft in heftelser[:max_records]:
            doc_id = str(heft.get("dokumentnummer") or heft.get("id") or "")
            pant_type = heft.get("pantetype") or heft.get("type") or "Virksomhetspant"
            belop = heft.get("belop") or heft.get("rammebelop") or heft.get("kravbelop")
            valuta = heft.get("valuta") or "NOK"
            tinglyst_dato = heft.get("tinglystDato") or heft.get("dato") or retrieved_at[:10]
            panthaver = heft.get("panthaver") or "Kredittinstitusjon"

            h_digest = hashlib.sha256(f"{clean_org}|losore|{doc_id}|{pant_type}|{belop}".encode()).hexdigest()
            obs_id = f"lien-{clean_org}-{h_digest[:16]}"

            amount_str = f"{belop:,.0f} {valuta}".replace(",", " ") if isinstance(belop, (int, float)) else "Uoppgitt beløp"

            observations.append({
                "id": obs_id,
                "organisation_number": clean_org,
                "platform": "losoreregisteret",
                "signal_type": "registered_mortgage_lien",
                "source_url": f"https://data.brreg.no/losore/api/pant/enhet/{clean_org}",
                "retrieved_at": retrieved_at,
                "published_at": tinglyst_dato,
                "content_sha256": h_digest,
                "exact_entity": True,
                "identity_proof": [
                    {
                        "type": "official_brreg_losoreregisteret",
                        "organisation_number": clean_org,
                        "company_name": clean_name,
                        "document_id": doc_id,
                        "mortgage_type": pant_type,
                    }
                ],
                "acquisition_mode": "official_api",
                "rights_status": "approved",
                "source_class": "official_mortgage_registry",
                "evidence_span": f"Tinglyst {pant_type} på {clean_name} ({clean_org}) hos Løsøreregisteret: {amount_str} til {panthaver}",
                "metrics": {
                    "document_id": doc_id,
                    "mortgage_type": pant_type,
                    "amount": belop,
                    "currency": valuta,
                    "secured_party": panthaver,
                    "registered_date": tinglyst_dato,
                },
            })

    except Exception:
        elapsed_ms = int((time.perf_counter() - start_time) * 1000)
        if budget:
            budget.record_request(cost_usd=0.0, bytes_received=bytes_received, elapsed_ms=elapsed_ms)

    return observations
