"""Mattilsynet (Norwegian Food Safety Authority - Smilefjes) Connector for Signalpost.

Extracts official food safety inspection reports, audit compliance history,
and inspection smileys (Smilefjes) for restaurants, cafes, hotels, catering,
and food production enterprises across Norway.
"""
from __future__ import annotations

import hashlib
import json
import time
import urllib.parse
import urllib.request
from typing import Any

MATTILSYNET_API_ENDPOINT = "https://hotell.difi.no/api/json/mattilsynet/smilefjes/tilsyn"
USER_AGENT = "SignalPostAgent/1.0 (+https://builderr.ai/signalpost)"


def fetch_mattilsynet_inspections(
    org_number: str,
    company_name: str,
    *,
    budget: Any | None = None,
    timeout: float = 6.0,
    max_records: int = 5,
) -> list[dict[str, Any]]:
    """Fetch official food safety inspection grades (Smilefjes) from Mattilsynet."""
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

    # Query Mattilsynet open data API by organization number
    url = f"{MATTILSYNET_API_ENDPOINT}?orgnr={clean_org}"

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

        entries = data.get("entries") or []
        for entry in entries[:max_records]:
            tilsyn_id = str(entry.get("tilsynid") or entry.get("id") or "")
            dato = str(entry.get("dato") or "")
            karakter = str(entry.get("total_karakter") or "0")
            obj_navn = entry.get("navn") or clean_name
            adr = entry.get("adr") or ""
            poststed = entry.get("poststed") or ""

            # Grade interpretation: 0 = Smilefjes (Meget bra/Ingen avvik), 1 = Strekmunn (Mindre avvik), 2/3 = Sur munn (Alvorlige avvik)
            grade_label = {
                "0": "Smil (Ingen avvik funnet - Meget bra)",
                "1": "Strek (Mindre avvik - Oppfølging kreves)",
                "2": "Sur munn (Betydelige avvik)",
                "3": "Sur munn (Alvorlige avvik / Vedtak fattet)",
            }.get(karakter, f"Karakter {karakter}")

            # Normalize date YYYYMMDD -> YYYY-MM-DD if needed
            pub_date = f"{dato[:4]}-{dato[4:6]}-{dato[6:8]}" if (len(dato) == 8 and dato.isdigit()) else (dato or retrieved_at[:10])

            h_digest = hashlib.sha256(f"{clean_org}|mattilsynet|{tilsyn_id}|{dato}|{karakter}".encode()).hexdigest()
            record_url = f"https://smilefjes.mattilsynet.no/tilsyn/{clean_org}"
            obs_id = f"foodsafety-{clean_org}-{h_digest[:16]}"

            observations.append({
                "id": obs_id,
                "organisation_number": clean_org,
                "platform": "mattilsynet",
                "signal_type": "food_safety_inspection",
                "source_url": record_url,
                "retrieved_at": retrieved_at,
                "published_at": pub_date,
                "content_sha256": h_digest,
                "exact_entity": True,
                "identity_proof": [
                    {
                        "type": "official_food_safety_inspection",
                        "operating_name": obj_navn,
                        "organisation_number": clean_org,
                        "tilsyn_id": tilsyn_id,
                    }
                ],
                "acquisition_mode": "official_api",
                "rights_status": "approved",
                "source_class": "official_inspection_authority",
                "evidence_span": f"Mattilsynet Smilefjes-tilsyn for {clean_name}: {grade_label} (dato {pub_date})",
                "metrics": {
                    "tilsyn_id": tilsyn_id,
                    "date": pub_date,
                    "grade": karakter,
                    "grade_label": grade_label,
                    "location_name": obj_navn,
                    "address": f"{adr}, {poststed}".strip(", "),
                },
                "strategy": "mattilsynet_smilefjes_lookup",
            })

        return observations

    except Exception:
        elapsed_ms = int((time.perf_counter() - start_time) * 1000)
        if budget:
            budget.record_request(cost_usd=0.0, bytes_received=bytes_received, elapsed_ms=elapsed_ms)
        return []
