"""Verify candidate Norwegian customer pages and company-issued press releases."""
from __future__ import annotations

import re
import unicodedata
from urllib.parse import urlsplit

from ..evidence import utc_now
from ..verified_items import cision_release_channel, name_in_text, validate_news_item
from ..website import fetch_source_page


def _company_name_match(name: str, text: str) -> re.Match | None:
    tokens = re.findall(r"[^\W_]+", unicodedata.normalize("NFKC", name))
    if len(tokens) < 2:
        return None
    return re.search(r"(?<!\w)" + r"[\W_]+".join(map(re.escape, tokens)) + r"(?!\w)", text, re.I)


def _norwegian_presence_path(url: str) -> str | None:
    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.hostname or parts.username or parts.password:
        return None
    path = parts.path.strip("/").split("/")
    if len(path) < 2 or path[0] not in {"no", "nb-no", "en-no"}:
        return None
    if not any(re.match(r"(?:contact|kontakt|legal|business-information|privacy)(?:[-.]|$)", part, re.I) for part in path[1:]):
        return None
    return f"{parts.scheme}://{parts.netloc}/{path[0]}/"


def fetch_norwegian_presence(url: str, *, org: str, company: str, budget) -> dict | None:
    if not _norwegian_presence_path(url) or not re.fullmatch(r"[0-9]{9}", org):
        return None
    page, error = fetch_source_page(url, budget=budget)
    if not page:
        return {"status": "blocked" if error and any(x in error for x in ("403", "robots_disallowed", "robots_access_denied")) else "source_error",
                "source_url": url, "note": error}
    scope = _norwegian_presence_path(page["url"])
    if not scope or urlsplit(page["url"]).hostname != urlsplit(url).hostname:
        return None
    text = unicodedata.normalize("NFKC", str(page.get("visible_text_excerpt") or page.get("main_text_excerpt") or ""))
    org_pattern = r"\b(?:organisasjonsnummer|org\.?\s*nr\.?|trade register number)\s*[:.]?\s*" + r"\s*".join(org) + r"(?!\d)"
    for match in re.finditer(org_pattern, text, re.I):
        excerpt = text[max(0, match.start() - 240):match.end() + 240]
        if _company_name_match(company, excerpt):
            return {"status": "available", "source_url": page["url"], "content_sha256": page["content_sha256"],
                    "source_class": "company_site", "retrieved_at": utc_now(), "evidence_span": excerpt[:500],
                    "value": {"url": scope, "relationship": "norwegian_company_presence", "organisation_number": org}}
    return {"status": "ambiguous", "source_url": page["url"], "note": "Exact local legal name and number not captured together"}


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
            "text": title, "evidence_span": excerpt, "metrics": {"title": title, "url": canonical}}
