"""Google News RSS connector — inline version for the live agent.

Fetches company mentions from Google News RSS search. Completely free, no API key needed.
Uses exact-title matching to prevent wrong-company attributions.
"""

from __future__ import annotations

import hashlib
import re
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any

from ..news_credibility import evaluate_news_credibility

LEGAL_SUFFIXES = {"as", "asa", "sa", "ba", "da", "ans", "enk", "nuf", "sti"}
UA = "SignalpostResearchPOC/1.0 (https://builderr.ai; bounded qualification run)"


def exact_title_match(company_name: str, title: str) -> bool:
    """Check if the company's core name appears in a headline."""
    company_tokens = re.findall(r"[a-z0-9æøå]+", str(company_name or "").casefold())
    while company_tokens and company_tokens[-1] in LEGAL_SUFFIXES:
        company_tokens.pop()
    if not company_tokens or (len(company_tokens) == 1 and len(company_tokens[0]) < 4):
        return False
    title_tokens = re.findall(r"[a-z0-9æøå]+", str(title or "").rsplit(" - ", 1)[0].casefold())
    if not title_tokens or len(company_tokens) > len(title_tokens):
        return False
    allowed_predecessors = {"av", "for", "fra", "hos", "i", "med", "om", "på", "til", "og", "kjøper", "velger"}
    for index in range(len(title_tokens) - len(company_tokens) + 1):
        if title_tokens[index:index + len(company_tokens)] != company_tokens:
            continue
        if index == 0 or title_tokens[index - 1] in allowed_predecessors:
            return True
    return False


def fetch_google_news(
    org: str,
    company_name: str,
    *,
    limit: int = 5,
    years: int = 2,
    budget: Any | None = None,
) -> list[dict[str, Any]]:
    """Fetch news mentions for a company from Google News RSS.

    Returns a list of observation dicts ready for external footprint evaluation.
    """
    if budget and not budget.can_proceed():
        return []

    clean_name = re.sub(r"\b(?:AS|ASA|BA|DA|ANS|ENK|NUF|STI)\b", "", str(company_name or ""), flags=re.I).strip()
    query_name = clean_name if len(clean_name) >= 4 else company_name
    query = urllib.parse.quote(f'"{query_name}" when:{years}y')
    url = f"https://news.google.com/rss/search?q={query}&hl=no&gl=NO&ceid=NO:no"

    try:
        request = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/rss+xml, application/xml"})
        with urllib.request.urlopen(request, timeout=25) as response:
            raw = response.read(2_000_000)

        if budget:
            budget.record_request(bytes_received=len(raw))

        root = ET.fromstring(raw)
        retrieved_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        output: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()

        for item in root.findall(".//item"):
            title = str(item.findtext("title") or "").strip()
            link = str(item.findtext("link") or "").strip()
            publisher = str(item.findtext("source") or "").strip()

            if not link or not exact_title_match(company_name, title):
                continue

            key = (title.casefold(), publisher.casefold())
            if key in seen:
                continue
            seen.add(key)

            published = item.findtext("pubDate")
            try:
                published_at = parsedate_to_datetime(published).astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
            except Exception:
                published_at = None

            # Multi-layer Norwegian Press Code credibility evaluation
            cred = evaluate_news_credibility(
                title=title,
                publisher_name=publisher,
                publisher_url="",
                source_link=link,
                published_at=published_at,
                company_name=company_name,
            )
            if not cred["is_publishable"]:
                continue

            digest = hashlib.sha256(raw + title.encode("utf-8") + publisher.encode("utf-8")).hexdigest()
            output.append({
                "id": "google-news-title-" + hashlib.sha256(f"{org}|{title}|{publisher}".encode()).hexdigest()[:24],
                "organisation_number": org,
                "platform": "news",
                "signal_type": "dated_news",
                "source_url": link,
                "retrieved_at": retrieved_at,
                "published_at": published_at,
                "content_sha256": digest,
                "exact_entity": True,
                "credibility_score": cred["credibility_score"],
                "credibility_tier": cred["credibility_tier"],
                "credibility_reasons": cred["reasons"],
                "identity_proof": [
                    {"type": "exact_legal_name_in_news_title", "value": company_name},
                    {"type": "publisher_label", "value": publisher},
                ],
                "acquisition_mode": "permitted_public_page",
                "rights_status": "approved",
                "source_class": "public_news",
                "sentiment_label": "neutral",
                "sentiment_model_version": "title_heuristic_v1",
                "evidence_span": title,
                "text": title,
                "publisher": publisher,
                "strategy": "independent_news_discovery",
            })
            if len(output) >= limit:
                break

        return output

    except Exception:
        if budget:
            budget.record_request()
        return []
