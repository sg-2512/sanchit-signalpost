"""Miljødirektoratet (Norwegian Environment Agency / Norske Utslipp) Connector.

Queries official Norwegian Environmental Agency records (Norske Utslipp) to uncover
statutory industrial emission permits (utslippstillatelser), carbon quota compliance,
and hazardous waste authorizations for Norwegian industrial and manufacturing enterprises.
"""
from __future__ import annotations

import hashlib
import json
import time
import urllib.parse
import urllib.request
from typing import Any

NORSKE_UTSLIPP_API = "https://www.norskeutslipp.no/api/virksomheter"
USER_AGENT = "SignalPostAgent/1.0 (+https://builderr.ai/signalpost)"


def fetch_miljodirektoratet_permits(
    org_number: str,
    company_name: str,
    *,
    budget: Any | None = None,
    timeout: float = 6.0,
    max_records: int = 5,
) -> list[dict[str, Any]]:
    """Fetch official environmental permits and emissions records from Miljødirektoratet."""
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

    params = {
        "orgnr": clean_org,
        "size": str(max_records),
    }
    url = f"{NORSKE_UTSLIPP_API}?{urllib.parse.urlencode(params)}"

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

        permits = data.get("tillatelser") or data.get("items") or data.get("anlegg") or []
        for p in permits[:max_records]:
            permit_id = str(p.get("tillatelseId") or p.get("id") or "")
            anlegg_navn = p.get("anleggsnavn") or p.get("navn") or clean_name
            type_tillatelse = p.get("tillatelsestype") or p.get("type") or "Utslippstillatelse etter forurensningsloven"
            status = p.get("status") or "Gjeldende"
            vedtaksdato = p.get("vedtaksdato") or p.get("dato") or retrieved_at[:10]

            h_digest = hashlib.sha256(f"{clean_org}|miljodir|{permit_id}|{type_tillatelse}".encode()).hexdigest()
            obs_id = f"permit-env-{clean_org}-{h_digest[:16]}"
            case_url = f"https://www.norskeutslipp.no/virksomhet/{clean_org}"

            observations.append({
                "id": obs_id,
                "organisation_number": clean_org,
                "platform": "miljodirektoratet",
                "signal_type": "environmental_permit_record",
                "source_url": case_url,
                "retrieved_at": retrieved_at,
                "published_at": vedtaksdato[:10],
                "content_sha256": h_digest,
                "exact_entity": True,
                "identity_proof": [
                    {
                        "type": "official_environmental_permit",
                        "organisation_number": clean_org,
                        "company_name": clean_name,
                        "permit_id": permit_id,
                        "authority": "Miljødirektoratet",
                    }
                ],
                "acquisition_mode": "official_api",
                "rights_status": "approved",
                "source_class": "environmental_authority",
                "evidence_span": f"Miljødirektoratet {type_tillatelse} for {clean_name} ({anlegg_navn}): Status {status} (Vedtatt {vedtaksdato[:10]})",
                "metrics": {
                    "permit_id": permit_id,
                    "facility_name": anlegg_navn,
                    "permit_type": type_tillatelse,
                    "status": status,
                    "date": vedtaksdato[:10],
                    "authority": "Miljødirektoratet",
                },
            })

    except Exception:
        elapsed_ms = int((time.perf_counter() - start_time) * 1000)
        if budget:
            budget.record_request(cost_usd=0.0, bytes_received=bytes_received, elapsed_ms=elapsed_ms)

    return observations
