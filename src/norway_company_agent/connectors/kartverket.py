"""Kartverket & Matrikkelen (Norwegian Mapping Authority & Cadastre) Connector.

Queries official Norwegian national address and cadastre registers via Geonorge (ws.geonorge.no)
to resolve official Gnr/Bnr/Snr cadastral identifiers, verified poststed, and exact WGS84
geographic coordinates (lat/long) for company operating locations.
"""
from __future__ import annotations

import hashlib
import json
import time
import urllib.parse
import urllib.request
from typing import Any

GEONORGE_ADRESSER_ENDPOINT = "https://ws.geonorge.no/adresser/v1/sok"
USER_AGENT = "SignalPostAgent/1.0 (+https://builderr.ai/signalpost)"


def geocode_address_kartverket(
    address_str: str,
    municipality: str = "",
    postcode: str = "",
    *,
    budget: Any | None = None,
    timeout: float = 5.0,
) -> dict[str, Any] | None:
    """Geocode a Norwegian address via Kartverket's official national Geonorge API."""
    if not address_str or len(address_str.strip()) < 3:
        return None

    if budget and not budget.can_proceed():
        return None

    clean_addr = address_str.strip()
    query_parts = [clean_addr]
    if postcode and postcode not in clean_addr:
        query_parts.append(postcode)
    if municipality and municipality not in clean_addr:
        query_parts.append(municipality)

    query_str = " ".join(query_parts)
    params = {
        "sok": query_str,
        "fuzzy": "true",
        "utkoordsys": "4258",  # ETRS89 / WGS84 decimal degrees
        "treffPerSide": "1",
    }
    url = f"{GEONORGE_ADRESSER_ENDPOINT}?{urllib.parse.urlencode(params)}"
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
            raw = resp.read(200_000)
            bytes_received = len(raw)
            data = json.loads(raw.decode("utf-8", errors="replace"))

        elapsed_ms = int((time.perf_counter() - start_time) * 1000)
        if budget:
            budget.record_request(cost_usd=0.0, bytes_received=bytes_received, elapsed_ms=elapsed_ms)

        adresser = data.get("adresser") or []
        if not adresser:
            return None

        hit = adresser[0]
        coords = hit.get("representasjonspunkt") or {}
        lat = coords.get("lat")
        lon = coords.get("lon")

        gnr = hit.get("gardsnummer")
        bnr = hit.get("bruksnummer")
        fnr = hit.get("festenummer")
        snr = hit.get("seksjonsnummer")

        cadastral_id = f"Gnr {gnr}, Bnr {bnr}" if (gnr and bnr) else ""
        if snr:
            cadastral_id += f", Snr {snr}"

        return {
            "formatted_address": hit.get("adressetekst") or clean_addr,
            "postnummer": hit.get("postnummer") or postcode,
            "poststed": hit.get("poststed") or municipality,
            "kommunenummer": hit.get("kommunenummer"),
            "kommunenavn": hit.get("kommunenavn") or municipality,
            "latitude": lat,
            "longitude": lon,
            "gardsnummer": gnr,
            "bruksnummer": bnr,
            "seksjonsnummer": snr,
            "cadastral_id": cadastral_id,
            "source": "Kartverket Geonorge",
        }

    except Exception:
        elapsed_ms = int((time.perf_counter() - start_time) * 1000)
        if budget:
            budget.record_request(cost_usd=0.0, bytes_received=bytes_received, elapsed_ms=elapsed_ms)
        return None
