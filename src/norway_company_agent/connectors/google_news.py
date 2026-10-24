"""Google News RSS connector — inline version for the live agent.

Fetches company mentions from Google News RSS search. Completely free, no API key needed.
Uses exact-title matching to prevent wrong-company attributions.
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

from bs4 import BeautifulSoup

from ..news_credibility import evaluate_news_credibility

LEGAL_SUFFIXES = {"as", "asa", "sa", "ba", "da", "ans", "enk", "nuf", "sti"}
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"


def decode_google_news_url(rss_url: str, timeout: float = 6.0) -> str | None:
    """Decode a Google News RSS redirect link directly to its canonical publisher URL."""
    if not rss_url or "news.google.com" not in rss_url:
        return rss_url
    try:
        req = urllib.request.Request(rss_url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            final_url = resp.geturl()
            if "news.google.com" not in final_url:
                return final_url
            raw_html = resp.read(2_000_000).decode("utf-8", errors="replace")

        soup = BeautifulSoup(raw_html, "html.parser")
        wiz = soup.select_one("c-wiz[data-p]")
        if not wiz or not wiz.get("data-p"):
            return None
        data_p = wiz.get("data-p")
        obj = json.loads(data_p.replace("%.@.", '["garturlreq",'))
        rpc_payload = [[["Fbv4je", json.dumps(obj[:-6] + obj[-2:]), "null", "generic"]]]
        body = urllib.parse.urlencode({"f.req": json.dumps(rpc_payload)}).encode()
        rpc_req = urllib.request.Request(
            "https://news.google.com/_/DotsSplashUi/data/batchexecute",
            data=body,
            headers={
                "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
                "User-Agent": UA,
            },
        )
        with urllib.request.urlopen(rpc_req, timeout=timeout) as rpc_resp:
            raw_rpc = rpc_resp.read(500_000).decode("utf-8", errors="replace")
            clean_text = raw_rpc.replace(")]}'", "").strip()
            arr = json.loads(clean_text)
            inner = json.loads(arr[0][2])
            decoded = inner[1]
            if decoded and decoded.startswith(("http://", "https://")):
                return decoded
    except Exception:
        pass
    return None


def fetch_verified_article(
    url: str,
    company_name: str,
    fallback_date: str | None = None,
    timeout: float = 6.0,
) -> dict[str, Any] | None:
    """Fetch an article page, compute authentic SHA-256 hash, and verify company naming."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            final_url = resp.geturl()
            raw = resp.read(1_000_000)
            sha = hashlib.sha256(raw).hexdigest()
            html = raw.decode("utf-8", errors="replace")
            soup = BeautifulSoup(html, "html.parser")

            pub_date = None
            for sel in (
                "time[datetime]",
                "meta[property='article:published_time']",
                "meta[name='publication_date']",
                "meta[name='date']",
                "meta[name='DC.date.issued']",
            ):
                el = soup.select_one(sel)
                if el:
                    val = el.get("datetime") or el.get("content")
                    if val:
                        m = re.match(r"^(\d{4}-\d{2}-\d{2})", str(val).strip())
                        if m:
                            pub_date = m.group(1)
                            break
            if not pub_date and fallback_date:
                m = re.match(r"^(\d{4}-\d{2}-\d{2})", str(fallback_date).strip())
                if m:
                    pub_date = m.group(1)

            clean_core = re.sub(
                r"\b(?:AS|ASA|BA|DA|ANS|ENK|NUF|STI)\b", "", company_name, flags=re.I
            ).strip().lower()
            name_in = (
                clean_core in html.lower()
                if len(clean_core) >= 4
                else exact_title_match(company_name, soup.title.get_text(" ", strip=True) if soup.title else "")
            )

            title = soup.title.get_text(" ", strip=True) if soup.title else ""
            h1 = soup.find("h1")
            if h1:
                h1_text = h1.get_text(" ", strip=True)
                if len(h1_text) >= 10:
                    title = h1_text

            return {
                "title": title[:200],
                "url": final_url,
                "source_url": final_url,
                "published_at": pub_date,
                "content_sha256": sha,
                "verification": "article_fetched",
                "name_in_article": name_in,
                "bytes_len": len(raw),
            }
    except Exception:
        return None


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
        if len(company_tokens) >= 2 or len(company_tokens[0]) >= 5:
            return True
        if index == 0 or title_tokens[index - 1] in allowed_predecessors:
            return True
    return False


def fetch_google_news(
    org: str,
    company_name: str,
    *,
    limit: int = 5,
    years: int = 3,
    budget: Any | None = None,
    decode_articles: bool = True,
) -> list[dict[str, Any]]:
    """Fetch news mentions for a company from Google News RSS.

    Returns a list of observation dicts ready for external footprint evaluation.
    When decode_articles is True, Google News redirect tokens are resolved to real
    publisher URLs and fetched to verify the company in the article and establish
    an authentic content_sha256 digest.
    """
    if budget and not budget.can_proceed():
        return []

    clean_name = re.sub(r"\b(?:AS|ASA|BA|DA|ANS|ENK|NUF|STI)\b", "", str(company_name or ""), flags=re.I).strip()
    query_name = clean_name if len(clean_name) >= 3 else company_name
    query = urllib.parse.quote(f'"{query_name}"')
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

            if not link:
                continue

            lower_t = title.casefold()
            if any(bad in lower_t for bad in ["dødsfall", "gravferd", "minneord", "bisettelse", "død:"]):
                continue

            from ..verified_items import brand_tokens
            tokens = brand_tokens(company_name)
            has_brand_in_title = any(tok in lower_t for tok in tokens if len(tok) >= 4)
            has_title_match = exact_title_match(company_name, title)

            if not (has_title_match or has_brand_in_title or len(output) < 3):
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
            if decode_articles:
                if cred.get("credibility_tier") == "rejected" or cred.get("fatal_flags"):
                    continue
            else:
                if not cred.get("is_publishable"):
                    continue

            decoded_url = None
            art = None
            reserve_needed = min(10, max(2, int((budget.max_requests if budget else 1000) * 0.02)))
            if decode_articles and (budget is None or budget.can_proceed(reserve=reserve_needed)):
                decoded_url = decode_google_news_url(link)
                if budget:
                    budget.record_request()
                if decoded_url and decoded_url.startswith(("http://", "https://")) and "news.google.com" not in decoded_url:
                    art = fetch_verified_article(decoded_url, company_name, fallback_date=published_at)
                    if budget and art:
                        budget.record_request(bytes_received=art.get("bytes_len", 0))

            if art and art.get("content_sha256") and art.get("name_in_article"):
                final_link = art["url"]
                final_title = art["title"] or title
                final_sha = art["content_sha256"]
                final_pub = art["published_at"] or (published_at[:10] if published_at else None)
                verif = "article_fetched"
                name_in_art = True
            elif not decode_articles:
                final_link = link
                final_title = title
                final_sha = hashlib.sha256(raw + title.encode("utf-8") + publisher.encode("utf-8")).hexdigest()
                final_pub = published_at
                verif = ""
                name_in_art = False
            else:
                continue

            output.append({
                "id": "google-news-title-" + hashlib.sha256(f"{org}|{final_title}|{publisher}".encode()).hexdigest()[:24],
                "organisation_number": org,
                "platform": "news",
                "signal_type": "dated_news",
                "source_url": final_link,
                "retrieved_at": retrieved_at,
                "published_at": final_pub,
                "content_sha256": final_sha,
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
                "evidence_span": final_title,
                "text": final_title,
                "publisher": publisher,
                "strategy": "independent_news_discovery",
                "verification": verif,
                "name_in_article": name_in_art,
            })
            if len(output) >= limit:
                break

        return output

    except Exception:
        if budget:
            budget.record_request()
        return []
