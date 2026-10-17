"""Forskningsrådet & SkatteFUNN (Research Council of Norway) Connector for Signalpost.

Queries official Norwegian research funding registers (Prosjektbanken) to uncover awarded
R&D grants, SkatteFUNN tax-incentivized research projects, and state-supported innovation
initiatives, providing high-conviction signals of technological depth and institutional backing.
"""
from __future__ import annotations

import hashlib
import json
import time
import urllib.parse
import urllib.request
from typing import Any

PROSJEKTBANKEN_API_ENDPOINT = "https://prosjektbanken.forskningsradet.no/api/projects"
USER_AGENT = "SignalPostAgent/1.0 (+https://builderr.ai/signalpost)"


def fetch_forskningsradet_grants(
    org_number: str,
    company_name: str,
    *,
    budget: Any | None = None,
    timeout: float = 6.0,
    max_records: int = 5,
) -> list[dict[str, Any]]:
    """Fetch official R&D project grants from Forskningsrådet Prosjektbanken."""
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
    url = f"{PROSJEKTBANKEN_API_ENDPOINT}?{urllib.parse.urlencode(params)}"

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

        projects = data.get("projects") or data.get("items") or data.get("results") or []
        for proj in projects[:max_records]:
            proj_id = str(proj.get("projectNumber") or proj.get("id") or "")
            title = proj.get("title") or proj.get("projectTitle") or f"Forskningsprosjekt: {clean_name}"
            program = proj.get("fundingScheme") or proj.get("program") or "Forskningsrådet"
            amount = proj.get("allocatedAmount") or proj.get("totalBudget")
            start_year = proj.get("startYear") or proj.get("startDate", "")[:4]
            end_year = proj.get("endYear") or proj.get("endDate", "")[:4]

            h_digest = hashlib.sha256(f"{clean_org}|forskningsradet|{proj_id}|{title}".encode()).hexdigest()
            obs_id = f"rnd-grant-{clean_org}-{h_digest[:16]}"
            proj_url = f"https://prosjektbanken.forskningsradet.no/project/FORISS/{proj_id}" if proj_id else "https://prosjektbanken.forskningsradet.no"

            amount_label = f"{amount:,.0f} NOK".replace(",", " ") if isinstance(amount, (int, float)) else "Tildelt støtte"

            observations.append({
                "id": obs_id,
                "organisation_number": clean_org,
                "platform": "forskningsradet",
                "signal_type": "public_rnd_grant",
                "source_url": proj_url,
                "retrieved_at": retrieved_at,
                "published_at": str(start_year) if start_year else retrieved_at[:10],
                "content_sha256": h_digest,
                "exact_entity": True,
                "identity_proof": [
                    {
                        "type": "official_research_council_project",
                        "organisation_number": clean_org,
                        "company_name": clean_name,
                        "project_number": proj_id,
                        "funding_scheme": program,
                    }
                ],
                "acquisition_mode": "official_api",
                "rights_status": "approved",
                "source_class": "research_funding_authority",
                "evidence_span": f"Norges forskningsråd tildeling ({program}) til {clean_name}: '{title}' ({amount_label}, {start_year}–{end_year})",
                "metrics": {
                    "project_number": proj_id,
                    "title": title,
                    "funding_scheme": program,
                    "amount_nok": amount,
                    "start_year": start_year,
                    "end_year": end_year,
                },
            })

    except Exception:
        elapsed_ms = int((time.perf_counter() - start_time) * 1000)
        if budget:
            budget.record_request(cost_usd=0.0, bytes_received=bytes_received, elapsed_ms=elapsed_ms)

    return observations
