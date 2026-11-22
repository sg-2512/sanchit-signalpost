"""Verify candidate Norwegian customer pages and company-issued press releases."""
from __future__ import annotations

import re
import unicodedata
from urllib.parse import urlsplit

from ..evidence import utc_now
from ..verified_items import cision_release_channel, name_in_text, validate_news_item
from ..website import fetch_source_page, norwegian_presence_path


def _company_name_match(name: str, text: str) -> re.Match | None:
    tokens = re.findall(r"[^\W_]+", unicodedata.normalize("NFKC", name))
    if len(tokens) < 2:
        return None
    return re.search(r"(?<!\w)" + r"[\W_]+".join(map(re.escape, tokens)) + r"(?!\w)", text, re.I)


def fetch_norwegian_presence(url: str, *, org: str, company: str, budget) -> dict | None:
    if not norwegian_presence_path(url) or not re.fullmatch(r"[0-9]{9}", org):
        return None
    page, error = fetch_source_page(url, budget=budget)
    if not page:
        return {"status": "blocked" if error and any(x in error for x in ("403", "robots_disallowed", "robots_access_denied")) else "source_error",
                "source_url": url, "note": error}
    if urlsplit(page["url"]).hostname != urlsplit(url).hostname:
        return None
    return assess_norwegian_presence(page, org=org, company=company)


def assess_norwegian_presence(page: dict, *, org: str, company: str) -> dict | None:
    scope = norwegian_presence_path(str(page.get("url") or ""))
    if not scope or not re.fullmatch(r"[0-9]{9}", org) or not re.fullmatch(r"[0-9a-f]{64}", str(page.get("content_sha256") or "")):
        return None
    text = unicodedata.normalize("NFKC", str(page.get("visible_text_excerpt") or page.get("main_text_excerpt") or ""))
    org_label = r"\b(?:organisasjons(?:nummer|nr\.?)|org\.?\s*nr\.?|reg\.?\s*nr\.?|trade register number)\s*[:.]?\s*"
    org_pattern = org_label + r"((?:\d[\s.\u00a0-]*){8}\d)(?!\d)"
    for match in re.finditer(org_pattern, text, re.I):
        if re.sub(r"\D", "", match[1]) != org:
            continue
        excerpt = text[max(0, match.start() - 240):match.end() + 240]
        company_match = _company_name_match(company, excerpt)
        if company_match:
            offset = max(0, match.start() - 240)
            between = text[min(offset + company_match.end(), match.end()):max(offset + company_match.start(), match.start())]
            if re.search(org_pattern, between, re.I):
                continue
            return {"status": "available", "source_url": page["url"], "content_sha256": page["content_sha256"],
                    "source_class": "company_site", "retrieved_at": page.get("retrieved_at") or utc_now(),
                    "literal_excerpt": excerpt[:500],
                    "newsroom_links": page.get("newsroom_links") or [],
                    "value": {"url": scope, "relationship": "norwegian_company_presence", "organisation_number": org}}
    return {"status": "ambiguous", "source_url": page["url"], "note": "Exact local legal name and number not captured together"}


def fetch_scoped_newsroom(url: str, *, scope: str, org: str, company: str, budget) -> list[dict]:
    def in_scope(candidate: str) -> bool:
        parsed = urlsplit(candidate)
        anchor = urlsplit(scope)
        return parsed.scheme == "https" and parsed.netloc == anchor.netloc and parsed.path.startswith(anchor.path)

    if not in_scope(url):
        return []
    hub, _ = fetch_source_page(url, budget=budget)
    if not hub or not in_scope(hub["url"]):
        return []
    observations = []
    checked = set()
    ordered = sorted(hub.get("news_items") or [], key=lambda item: bool(
        _company_name_match(company, str(item.get("excerpt") or item.get("title") or ""))), reverse=True)
    for item in ordered:
        link = str(item.get("url") or "")
        if link in checked or link.rstrip("/") == hub["url"].rstrip("/") or not in_scope(link):
            continue
        if len(checked) >= 3:
            break
        checked.add(link)
        page, _ = fetch_source_page(link, budget=budget)
        if not page or not in_scope(page["url"]):
            continue
        body = str(page.get("main_text_excerpt") or "")
        exact = _company_name_match(company, body)
        title = str(page.get("heading") or page.get("title") or "")
        digest = page.get("content_sha256")
        published = page.get("article_publication_date")
        article = {"url": page["url"], "title": title, "published_at": published, "content_sha256": digest,
                   "verification": "article_fetched", "name_in_article": bool(exact)}
        if not exact or not validate_news_item(article, company_name=company, site_url=scope)[0]:
            continue
        excerpt = body[max(0, exact.start() - 100):exact.end() + 180]
        observations.append({
            "id": f"scoped-news-{org}-{digest[:16]}", "organisation_number": org,
            "platform": "news", "signal_type": "dated_news", "source_url": page["url"], "article_url": page["url"],
            "retrieved_at": utc_now(), "published_at": published, "content_sha256": digest,
            "exact_entity": True, "identity_proof": [{"type": "exact_legal_name_in_first_party_article", "name": exact[0]}],
            "name_in_article": True, "verification": "article_fetched", "rights_status": "approved",
            "acquisition_mode": "permitted_public_page", "source_class": "company_site",
            "strategy": "scoped_first_party_news", "text": title, "literal_excerpt": excerpt, "evidence_span": excerpt,
            "metrics": {"title": title, "url": page["url"]},
        })
    return observations


def fetch_company_press_release(url: str, *, org: str, company: str, budget) -> dict | None:
    channel = cision_release_channel(url)
    channel_tokens = channel.split("-") if channel else []
    company_tokens = re.findall(r"[^\W_]+", company.casefold())
    if len(channel_tokens) < 2 or not set(channel_tokens[:2]).issubset(company_tokens):
        return None
    page, _ = fetch_source_page(url, budget=budget)
    if not page or cision_release_channel(page["url"]) != channel:
        return None
    body = unicodedata.normalize("NFKC", str(page.get("main_text_excerpt") or ""))
    visible = unicodedata.normalize("NFKC", str(page.get("visible_text_excerpt") or ""))
    exact_name = _company_name_match(company, body) or _company_name_match(company, visible)
    if not exact_name or not name_in_text(company, body[:1500]):
        return None
    canonical = page["url"].rstrip("/")
    editorial = next((item for item in (page.get("news_items") or [])
                      if item.get("published_at") and str(item.get("source_url") or "").rstrip("/") == canonical), None)
    if not editorial:
        return None
    published = str(editorial["published_at"])[:10]
    digest = page["content_sha256"]
    title = editorial["title"]
    excerpt = body[:280]
    item = {"url": canonical, "source_url": canonical, "title": title, "published_at": published,
            "content_sha256": digest, "verification": "article_fetched", "name_in_article": True}
    if not validate_news_item(item, company_name=company)[0]:
        return None
    return {"id": f"press-cision-{org}-{canonical.rsplit(',c', 1)[-1]}", "organisation_number": org,
            "platform": "news", "signal_type": "dated_news", "source_url": canonical, "article_url": canonical,
            "retrieved_at": utc_now(), "published_at": published, "content_sha256": digest,
            "exact_entity": True, "identity_proof": [{"type": "company_issued_release", "name": exact_name.group(),
                "publisher_channel": channel}], "name_in_article": True, "verification": "article_fetched",
            "rights_status": "approved", "acquisition_mode": "permitted_public_page",
            "source_class": "company_press_release", "strategy": "cision_company_press_release",
            "text": title, "evidence_span": excerpt, "literal_excerpt": excerpt,
            "metrics": {"title": title, "url": canonical, "content_class": "press_release_distribution"}}
