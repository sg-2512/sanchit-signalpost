"""Kartverket & Matrikkelen (Norwegian Mapping Authority & Cadastre) Connector.

Queries official Norwegian national address and cadastre registers via Geonorge (ws.geonorge.no)
to resolve official Gnr/Bnr/Snr cadastral identifiers, verified poststed, and exact WGS84
geographic coordinates (lat/long) for registered addresses.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
import unicodedata
import urllib.parse
import urllib.request
from typing import Any

GEONORGE_ADRESSER_ENDPOINT = "https://ws.geonorge.no/adresser/v1/sok"
USER_AGENT = "SignalPostAgent/1.0 (+https://builderr.ai/signalpost)"


def _address_components(value: str) -> tuple[str, int, str] | None:
    text = " ".join(unicodedata.normalize("NFKC", value).casefold().split())
    match = re.fullmatch(r"(.+?)\s+(\d+)\s*([a-zæøå]?)", text)
    if not match:
        return None
    name = re.sub(r"\b(?:v|vn)\.$", "vei", match[1])
    name = re.sub(r"\bgt\.$", "gate", name)
    return re.sub(r"[^\w]", "", name), int(match[2]), match[3]


def matches_registered_address(registered: dict[str, Any], geocoded: dict[str, Any]) -> bool:
    """Reject fuzzy Geonorge hits that differ from the registry address."""
    lines = registered.get("adresse") or []
    street = str(lines[-1] if isinstance(lines, list) and lines else lines or "")
    postcode = str(registered.get("postnummer") or "")
    if not street or not re.fullmatch(r"\d{4}", postcode):
        return False
    if postcode != str(geocoded.get("postnummer") or ""):
        return False
    reg_municipality = str(registered.get("kommunenummer") or "")
    geo_municipality = str(geocoded.get("kommunenummer") or "")
    if reg_municipality and reg_municipality != geo_municipality:
        return False

    registered_parts = _address_components(street)
    if not registered_parts:
        return False
    name = geocoded.get("adressenavn")
    number = geocoded.get("nummer")
    address = f"{name} {number}{geocoded.get('bokstav') or ''}" if name and number is not None else str(geocoded.get("formatted_address") or "")
    return registered_parts == _address_components(address)


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
    if budget and not budget.try_start_request():
        return None
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

        digest = hashlib.sha256(raw).hexdigest()

        return {
            "formatted_address": hit.get("adressetekst"),
            "adressenavn": hit.get("adressenavn"),
            "nummer": hit.get("nummer"),
            "bokstav": hit.get("bokstav"),
            "postnummer": hit.get("postnummer"),
            "poststed": hit.get("poststed"),
            "kommunenummer": hit.get("kommunenummer"),
            "kommunenavn": hit.get("kommunenavn"),
            "latitude": lat,
            "longitude": lon,
            "gardsnummer": gnr,
            "bruksnummer": bnr,
            "seksjonsnummer": snr,
            "cadastral_id": cadastral_id,
            "source": "Kartverket Geonorge",
            "source_url": url,
            "content_sha256": digest,
        }

    except (OSError, ValueError, TypeError, AttributeError):
        return None
    finally:
        if budget:
            budget.record_request(count=0, bytes_received=bytes_received,
                                  elapsed_ms=int((time.perf_counter() - start_time) * 1000))
