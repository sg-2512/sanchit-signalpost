"""Press Wire connector for NTB Kommunikasjon, Mynewsdesk, and Cision Norway.

Discovers authentic dated corporate press releases for Norwegian companies from:
- NTB Kommunikasjon (kommunikasjon.ntb.no) - Norway's national press agency wire
- Mynewsdesk Norway (mynewsdesk.com/no) - Major Scandinavian corporate newsrooms
- Cision Norway (news.cision.com/no) - Regulatory and corporate announcement wire

Uses Google News RSS with site constraints to discover items, then decodes them
to direct canonical publisher URLs and verifies company naming in the article text.
"""
from __future__ import annotations

import hashlib
import json
import re
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any

from .google_news import decode_google_news_url, fetch_verified_article, exact_title_match

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
WIRE_DOMAINS = ("kommunikasjon.ntb.no", "mynewsdesk.com", "news.cision.com")


def fetch_press_wire_news(
    org: str,
    company_name: str,
    *,
    limit: int = 5,
    budget: Any | None = None,
) -> list[dict[str, Any]]:
    """Fetch dated corporate press releases for a company from Norwegian wire services."""
    if not company_name:
        return []
    if budget and not budget.can_proceed(reserve=5):
        return []

    clean_name = re.sub(r"\b(?:AS|ASA|BA|DA|ANS|ENK|NUF|STI)\b", "", str(company_name or ""), flags=re.I).strip()
    query_name = clean_name if len(clean_name) >= 3 else company_name
    query_str = f'"{query_name}" (site:kommunikasjon.ntb.no OR site:mynewsdesk.com/no OR site:news.cision.com)'
    encoded_q = urllib.parse.quote(query_str)
    url = f"https://news.google.com/rss/search?q={encoded_q}&hl=no&gl=NO&ceid=NO:no"

    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/rss+xml, application/xml"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read(1_500_000)

        if budget:
            budget.record_request(bytes_received=len(raw))

        root = ET.fromstring(raw)
        retrieved_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        observations: list[dict[str, Any]] = []
        seen_urls: set[str] = set()

        for item in root.findall(".//item"):
            title = str(item.findtext("title") or "").strip()
            link = str(item.findtext("link") or "").strip()
            publisher = str(item.findtext("source") or "").strip()

            if not link:
                continue

            pub_raw = item.findtext("pubDate")
            try:
                published_at = parsedate_to_datetime(pub_raw).astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
            except Exception:
                published_at = None

            decoded_url = decode_google_news_url(link)
            if budget:
                budget.record_request()

            if not decoded_url or not decoded_url.startswith(("http://", "https://")) or "news.google.com" in decoded_url:
                continue

            if not any(wire in decoded_url.lower() for wire in WIRE_DOMAINS):
                continue

            if decoded_url.lower() in seen_urls:
                continue
            seen_urls.add(decoded_url.lower())

            art = fetch_verified_article(decoded_url, company_name, fallback_date=published_at)
            if budget and art:
                budget.record_request(bytes_received=art.get("bytes_len", 0))

            if not art or not art.get("content_sha256") or not art.get("name_in_article"):
                continue

            pub_iso = art.get("published_at") or (published_at[:10] if published_at else None)
            if not pub_iso:
                continue

            final_title = art.get("title") or title
            final_title = final_title.rsplit(" - ", 1)[0].strip()

            obs_id = "wire-news-" + hashlib.sha256(f"{org}|{decoded_url}".encode()).hexdigest()[:24]
            observations.append({
                "id": obs_id,
                "organisation_number": str(org),
                "platform": "press_wire",
                "signal_type": "dated_news",
                "source_url": decoded_url,
                "retrieved_at": retrieved_at,
                "published_at": pub_iso,
                "content_sha256": art["content_sha256"],
                "exact_entity": True,
                "verification": "article_fetched",
                "name_in_article": True,
                "identity_proof": [
                    {"type": "press_wire_named_release", "publisher": publisher, "query": query_name}
                ],
                "acquisition_mode": "permitted_public_page",
                "rights_status": "approved",
                "source_class": "editorial_press_wire",
                "evidence_span": f"Official press release: {final_title} (published {pub_iso}) via {publisher}",
                "text": final_title,
                "metrics": {
                    "title": final_title,
                    "url": decoded_url,
                    "published_at": pub_iso,
                    "wire_service": publisher or "Norwegian PR Wire",
                },
                "strategy": "press_wire_news_discovery",
            })

            if len(observations) >= limit:
                break

        return observations

    except Exception:
        return []
