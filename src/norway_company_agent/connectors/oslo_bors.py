"""Oslo Børs / Euronext Oslo (NewsWeb & Listed Securities) Connector for Signalpost.

Queries official Oslo Børs and Euronext market data and the NewsWeb statutory disclosure
portal to detect whether an enterprise is publicly listed (Oslo Børs Hovedliste, Euronext Growth,
or Euronext Expand), retrieve its ticker and ISIN, and index official regulatory stock exchange
announcements (børsmeldinger).
"""
from __future__ import annotations

import hashlib
import json
import time
import urllib.parse
import urllib.request
from typing import Any

NEWSWEB_API_ENDPOINT = "https://newsweb.oslobors.no/api/search"
USER_AGENT = "SignalPostAgent/1.0 (+https://builderr.ai/signalpost)"


def fetch_oslo_bors_disclosures(
    org_number: str,
    company_name: str,
    *,
    budget: Any | None = None,
    timeout: float = 6.0,
    max_records: int = 5,
) -> list[dict[str, Any]]:
    """Fetch official stock exchange disclosures and ticker data from Oslo Børs NewsWeb."""
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
        "issuerOrgNo": clean_org,
        "size": str(max_records),
    }
    url = f"{NEWSWEB_API_ENDPOINT}?{urllib.parse.urlencode(params)}"

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

        messages = data.get("data") or data.get("messages") or data.get("items") or []
        for msg in messages[:max_records]:
            msg_id = str(msg.get("messageId") or msg.get("id") or "")
            title = msg.get("title") or msg.get("header") or f"Børsmelding: {clean_name}"
            issuer_ticker = msg.get("issuerSign") or msg.get("ticker") or ""
            market = msg.get("market") or msg.get("segment") or "Oslo Børs"
            pub_date = msg.get("publishedTime") or msg.get("date") or retrieved_at[:10]
            category = msg.get("categoryName") or msg.get("category") or "Informasjonspliktig pressemelding"

            h_digest = hashlib.sha256(f"{clean_org}|oslobors|{msg_id}|{title}".encode()).hexdigest()
            obs_id = f"bors-{clean_org}-{h_digest[:16]}"
            case_url = f"https://newsweb.oslobors.no/message/{msg_id}" if msg_id else "https://newsweb.oslobors.no"

            observations.append({
                "id": obs_id,
                "organisation_number": clean_org,
                "platform": "oslo_bors",
                "signal_type": "stock_exchange_disclosure",
                "source_url": case_url,
                "retrieved_at": retrieved_at,
                "published_at": pub_date[:10],
                "content_sha256": h_digest,
                "exact_entity": True,
                "identity_proof": [
                    {
                        "type": "official_stock_exchange_disclosure",
                        "organisation_number": clean_org,
                        "company_name": clean_name,
                        "ticker": issuer_ticker,
                        "market": market,
                        "message_id": msg_id,
                    }
                ],
                "acquisition_mode": "official_api",
                "rights_status": "approved",
                "source_class": "stock_exchange",
                "evidence_span": f"Oslo Børs ({market}) børsmelding for {clean_name} [{issuer_ticker}]: '{title}' ({pub_date[:10]})",
                "metrics": {
                    "message_id": msg_id,
                    "title": title,
                    "ticker": issuer_ticker,
                    "market": market,
                    "category": category,
                    "published_at": pub_date,
                },
            })

    except Exception:
        elapsed_ms = int((time.perf_counter() - start_time) * 1000)
        if budget:
            budget.record_request(cost_usd=0.0, bytes_received=bytes_received, elapsed_ms=elapsed_ms)

    return observations
