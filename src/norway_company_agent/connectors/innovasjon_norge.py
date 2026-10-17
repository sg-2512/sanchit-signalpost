"""Innovasjon Norge (Innovation Norway Grants & Commercialization Subsidies) Connector.

Queries official Innovation Norway grant databases to identify public innovation grants,
environmental technology schemes (Miljøteknologiordningen), startup loans, and regional
business development funding awarded to Norwegian commercial enterprises.
"""
from __future__ import annotations

import hashlib
import json
import time
import urllib.parse
import urllib.request
from typing import Any

IN_TILDELING_ENDPOINT = "https://tildeling.innovasjonnorge.no/api/tildelinger"
USER_AGENT = "SignalPostAgent/1.0 (+https://builderr.ai/signalpost)"


def fetch_innovasjon_norge_grants(
    org_number: str,
    company_name: str,
    *,
    budget: Any | None = None,
    timeout: float = 6.0,
    max_records: int = 5,
) -> list[dict[str, Any]]:
    """Fetch official grants and loans awarded by Innovation Norway."""
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
        "take": str(max_records),
    }
    url = f"{IN_TILDELING_ENDPOINT}?{urllib.parse.urlencode(params)}"

    try:
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "application/json",
            },
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read(400_000)
            bytes_received = len(raw)
            data = json.loads(raw.decode("utf-8", errors="replace"))

        elapsed_ms = int((time.perf_counter() - start_time) * 1000)
        if budget:
            budget.record_request(cost_usd=0.0, bytes_received=bytes_received, elapsed_ms=elapsed_ms)

        grants = data.get("tildelinger") or data.get("items") or data.get("results") or []
        for g in grants[:max_records]:
            tildeling_id = str(g.get("id") or g.get("tildelingId") or "")
            project_name = g.get("prosjektnavn") or g.get("title") or f"Innovasjonsprosjekt: {clean_name}"
            scheme = g.get("ordning") or g.get("program") or "Innovasjon Norge Tilskudd"
            belop = g.get("tilsagn") or g.get("belop")
            year = g.get("ar") or g.get("year") or retrieved_at[:4]
            fylke = g.get("fylke") or g.get("kommune") or ""

            h_digest = hashlib.sha256(f"{clean_org}|in|{tildeling_id}|{project_name}|{belop}".encode()).hexdigest()
            obs_id = f"in-grant-{clean_org}-{h_digest[:16]}"
            case_url = f"https://tildeling.innovasjonnorge.no/tildelinger/{tildeling_id}" if tildeling_id else "https://www.innovasjonnorge.no"

            amount_label = f"{belop:,.0f} NOK".replace(",", " ") if isinstance(belop, (int, float)) else "Innvilget tilskudd"

            observations.append({
                "id": obs_id,
                "organisation_number": clean_org,
                "platform": "innovasjon_norge",
                "signal_type": "innovation_grant_award",
                "source_url": case_url,
                "retrieved_at": retrieved_at,
                "published_at": str(year),
                "content_sha256": h_digest,
                "exact_entity": True,
                "identity_proof": [
                    {
                        "type": "official_innovation_norway_grant",
                        "organisation_number": clean_org,
                        "company_name": clean_name,
                        "grant_id": tildeling_id,
                        "scheme": scheme,
                    }
                ],
                "acquisition_mode": "official_api",
                "rights_status": "approved",
                "source_class": "innovation_agency",
                "evidence_span": f"Innovasjon Norge tildeling ({scheme}) til {clean_name}: '{project_name}' ({amount_label}, år {year})",
                "metrics": {
                    "grant_id": tildeling_id,
                    "project_name": project_name,
                    "scheme": scheme,
                    "amount_nok": belop,
                    "year": year,
                    "region": fylke,
                },
            })

    except Exception:
        elapsed_ms = int((time.perf_counter() - start_time) * 1000)
        if budget:
            budget.record_request(cost_usd=0.0, bytes_received=bytes_received, elapsed_ms=elapsed_ms)

    return observations
