"""Enhetsregisteret Subunits (Underenheter) Regional Footprint Connector.

Fetches and normalizes all registered operating workplaces (branches, regional offices,
retail locations, facilities) for a Norwegian enterprise via Enhetsregisteret's official API.
"""
from __future__ import annotations

import hashlib
import json
import time
import urllib.parse
import urllib.request
from typing import Any

SUBUNITS_ENDPOINT = "https://data.brreg.no/enhetsregisteret/api/underenheter"
USER_AGENT = "SignalPostAgent/1.0 (+https://builderr.ai/signalpost)"


def fetch_company_subunits(
    org_number: str,
    company_name: str,
    *,
    budget: Any | None = None,
    timeout: float = 8.0,
    max_subunits: int = 10,
) -> list[dict[str, Any]]:
    """Fetch operating subunit locations and workplace employee distributions for a company."""
    if not org_number or len(org_number) < 9:
        return []

    if budget and not budget.can_proceed():
        return []

    clean_org = org_number.strip().replace(" ", "")
    url = f"{SUBUNITS_ENDPOINT}?overordnetEnhet={clean_org}&size=100"

    start_time = time.perf_counter()
    bytes_received = 0
    try:
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "application/json",
            },
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read(1_000_000)
            bytes_received = len(raw)
            data = json.loads(raw.decode("utf-8", errors="replace"))

        elapsed_ms = int((time.perf_counter() - start_time) * 1000)
        if budget:
            budget.record_request(cost_usd=0.0, bytes_received=bytes_received, elapsed_ms=elapsed_ms)

        digest = hashlib.sha256(raw).hexdigest()
        retrieved_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

        subunits = (data.get("_embedded") or {}).get("underenheter", [])
        if not subunits:
            return []

        observations: list[dict[str, Any]] = []

        for unit in subunits[:max_subunits]:
            sub_org = str(unit.get("organisasjonsnummer") or "")
            sub_name = str(unit.get("navn") or company_name)
            addr = unit.get("beliggenhetsadresse") or unit.get("postadresse") or {}
            street = ", ".join(addr.get("adresse", [])) if isinstance(addr.get("adresse"), list) else str(addr.get("adresse") or "")
            poststed = str(addr.get("poststed") or "")
            kommune = str(addr.get("kommune") or "")
            postnummer = str(addr.get("postnummer") or "")
            employees = unit.get("antallAnsatte")

            sub_digest = hashlib.sha256(f"{clean_org}|subunit|{sub_org}|{kommune}".encode()).hexdigest()

            full_addr_parts = [p for p in (street, f"{postnummer} {poststed}".strip(), kommune) if p]
            loc_str = ", ".join(full_addr_parts) if full_addr_parts else "Norway"

            # 1. Place observation for operational workplace
            obs_id = f"subunit-place-{clean_org}-{sub_digest[:16]}"
            observations.append({
                "id": obs_id,
                "organisation_number": clean_org,
                "platform": "brreg",
                "signal_type": "place_summary",
                "source_url": f"https://data.brreg.no/enhetsregisteret/api/underenheter/{sub_org}",
                "retrieved_at": retrieved_at,
                "content_sha256": sub_digest,
                "exact_entity": True,
                "identity_proof": [
                    {
                        "type": "official_brreg_subunit_record",
                        "parent_org": clean_org,
                        "subunit_org": sub_org,
                        "name": sub_name,
                    }
                ],
                "acquisition_mode": "official_api",
                "rights_status": "approved",
                "source_class": "official_subunits",
                "evidence_span": f"Official registered workplace: {sub_name} (subunit {sub_org}) located at {loc_str}",
                "metrics": {
                    "place_name": sub_name,
                    "subunit_organisation_number": sub_org,
                    "address": street,
                    "postnummer": postnummer,
                    "poststed": poststed,
                    "municipality": kommune,
                    "employees": employees,
                },
                "strategy": "official_enhetsregisteret_subunits",
            })

            # 2. Workforce distribution observation if branch employee count exists
            if employees is not None and employees > 0:
                wf_digest = hashlib.sha256(f"{clean_org}|subunit-wf|{sub_org}|{employees}".encode()).hexdigest()
                observations.append({
                    "id": f"subunit-wf-{clean_org}-{wf_digest[:16]}",
                    "organisation_number": clean_org,
                    "platform": "brreg",
                    "signal_type": "workforce_snapshot",
                    "source_url": f"https://data.brreg.no/enhetsregisteret/api/underenheter/{sub_org}",
                    "retrieved_at": retrieved_at,
                    "content_sha256": wf_digest,
                    "exact_entity": True,
                    "identity_proof": [
                        {
                            "type": "official_subunit_workforce_record",
                            "parent_org": clean_org,
                            "subunit_org": sub_org,
                            "employees": employees,
                        }
                    ],
                    "acquisition_mode": "official_api",
                    "rights_status": "approved",
                    "source_class": "official_subunits",
                    "evidence_span": f"Official workplace {sub_name} (subunit {sub_org}) employs {employees} registered employee(s)",
                    "metrics": {
                        "subunit_organisation_number": sub_org,
                        "workforce_value": employees,
                        "measure": "employees",
                        "municipality": kommune,
                    },
                    "strategy": "official_subunit_workforce",
                })

        return observations

    except Exception:
        elapsed_ms = int((time.perf_counter() - start_time) * 1000)
        if budget:
            budget.record_request(cost_usd=0.0, bytes_received=bytes_received, elapsed_ms=elapsed_ms)
        return []
