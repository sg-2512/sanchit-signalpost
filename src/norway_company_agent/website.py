from __future__ import annotations

import hashlib
import ipaddress
import json
import re
import socket
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser
from dataclasses import dataclass
from typing import Any
import xml.etree.ElementTree as ET
from datetime import timezone
from email.utils import parsedate_to_datetime

from bs4 import BeautifulSoup
import extruct
import tldextract
import trafilatura

from .evidence import evidence
from .verified_items import is_hub_url

USER_AGENT = "builderr-signalpost-poc/0.1 (+https://builderr.ai)"
SOCIAL_HOSTS = {
    "linkedin.com": "linkedin",
    "facebook.com": "facebook",
    "instagram.com": "instagram",
    "x.com": "x",
    "twitter.com": "x",
    "youtube.com": "youtube",
    "youtu.be": "youtube",
    "tiktok.com": "tiktok",
}
PRIORITY_TERMS = (
    "om-oss", "om_oss", "about", "kontakt", "contact", "ledelse", "management",
    "team", "people", "locations", "lokasjoner", "avdelinger", "butikker",
    "news", "press", "aktuelt", "nyheter", "media", "presse", "artikler", "siste-nytt", "blogg",
    "karriere", "jobb", "careers", "vacancies", "stillinger", "ledige-stillinger", "work-with-us",
    "bli-med-pa-laget", "open-positions", "rekruttering", "jobbe-hos-oss",
)


def assert_public_url(url: str) -> None:
    parsed = urllib.parse.urlparse(url)
    host = (parsed.hostname or "").lower().rstrip(".")
    if parsed.scheme not in {"http", "https"} or not host:
        raise ValueError("Only public HTTP(S) URLs are allowed")
    if host == "localhost" or host.endswith(".localhost") or host.endswith(".local"):
        raise ValueError("Local hosts are blocked")
    try:
        addresses = {item[4][0] for item in socket.getaddrinfo(host, parsed.port or (443 if parsed.scheme == "https" else 80), type=socket.SOCK_STREAM)}
    except socket.gaierror as exc:
        raise ValueError("Hostname did not resolve") from exc
    for address in addresses:
        ip = ipaddress.ip_address(address)
        if not ip.is_global:
            raise ValueError("Private, loopback, link-local, multicast, and reserved addresses are blocked")


class SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> Any:
        assert_public_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


SAFE_OPENER = urllib.request.build_opener(SafeRedirectHandler())


def _build_safe_ssl_opener() -> urllib.request.OpenerDirector:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return urllib.request.build_opener(SafeRedirectHandler(), urllib.request.HTTPSHandler(context=ctx))


SAFE_SSL_FALLBACK_OPENER = _build_safe_ssl_opener()


def _safe_urlopen(request: urllib.request.Request, timeout: float):
    try:
        return SAFE_OPENER.open(request, timeout=timeout)
    except urllib.error.URLError as exc:
        exc_str = str(exc).lower()
        if "ssl" in exc_str or "certificate" in exc_str or "cert" in exc_str:
            return SAFE_SSL_FALLBACK_OPENER.open(request, timeout=timeout)
        raise


def normalize_homepage(value: str | None) -> str | None:
    value = str(value or "").strip()
    if not value:
        return None
    if not re.match(r"^https?://", value, re.I):
        value = "https://" + value
    parsed = urllib.parse.urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return None
    return urllib.parse.urlunparse((parsed.scheme, parsed.netloc, parsed.path or "/", "", "", ""))


_TLD_EXTRACTOR = tldextract.TLDExtract(cache_dir=None, suffix_list_urls=())


def _registered_domain(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    host = (parsed.hostname or "").strip().lower()
    if not host:
        return ""
    try:
        ext = _TLD_EXTRACTOR(host)
        if ext.top_domain_under_public_suffix:
            return ext.top_domain_under_public_suffix
    except Exception:
        pass
    parts = [p for p in host.removeprefix("www.").split(".") if p]
    return ".".join(parts[-2:]) if len(parts) >= 2 else host


_ROBOTS_CACHE: dict[str, tuple[urllib.robotparser.RobotFileParser | None, float]] = {}


def _robots_allowed(url: str, timeout: float) -> bool:
    assert_public_url(url)
    parsed = urllib.parse.urlparse(url)
    robots_url = urllib.parse.urlunparse((parsed.scheme, parsed.netloc, "/robots.txt", "", "", ""))
    now = time.monotonic()
    if robots_url in _ROBOTS_CACHE:
        cached_parser, _ = _ROBOTS_CACHE[robots_url]
        if cached_parser is None:
            return True
        return cached_parser.can_fetch(USER_AGENT, url)

    parser = urllib.robotparser.RobotFileParser()
    parser.set_url(robots_url)
    try:
        request = urllib.request.Request(robots_url, headers={"User-Agent": USER_AGENT})
        with _safe_urlopen(request, timeout=min(timeout, 4.0)) as response:
            parser.parse(response.read().decode("utf-8", errors="replace").splitlines())
        _ROBOTS_CACHE[robots_url] = (parser, now)
        return parser.can_fetch(USER_AGENT, url)
    except Exception:
        _ROBOTS_CACHE[robots_url] = (None, now)
        return True


def _social_links(base_url: str, soup: BeautifulSoup) -> list[dict[str, str]]:
    found: dict[tuple[str, str], dict[str, str]] = {}
    candidates = [str(node.get("href") or "") for node in soup.select("a[href]")]
    candidates.extend(str(node.get("data-href") or "") for node in soup.select("[data-href]"))
    candidates.extend(str(node.get("src") or "") for node in soup.select("iframe[src]"))
    for candidate in candidates:
        url = urllib.parse.urljoin(base_url, candidate)
        parsed_candidate = urllib.parse.urlparse(url)
        if (parsed_candidate.hostname or "").casefold().removeprefix("www.") == "facebook.com" and parsed_candidate.path.startswith("/plugins/"):
            embedded = urllib.parse.parse_qs(parsed_candidate.query).get("href", [])
            if embedded:
                url = embedded[0]
        normalized = normalize_social_url(url)
        if not normalized:
            continue
        found[(normalized["platform"], normalized["url"])] = normalized
    return sorted(found.values(), key=lambda item: (item["platform"], item["url"]))


def structured_social_links(value: Any) -> list[dict[str, str]]:
    found: dict[tuple[str, str], dict[str, str]] = {}

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            same_as = node.get("sameAs")
            urls = same_as if isinstance(same_as, list) else [same_as]
            for raw in urls:
                if not isinstance(raw, str):
                    continue
                normalized = normalize_social_url(raw.strip())
                if normalized:
                    found[(normalized["platform"], normalized["url"])] = normalized
            for child in node.values():
                walk(child)
        elif isinstance(node, list):
            for child in node:
                walk(child)

    walk(value)
    return sorted(found.values(), key=lambda item: (item["platform"], item["url"]))


def normalize_social_url(url: str) -> dict[str, str] | None:
    try:
        parsed = urllib.parse.urlparse(url)
    except ValueError:
        return None
    host = (parsed.hostname or "").lower().removeprefix("www.")
    platform = next((label for domain, label in SOCIAL_HOSTS.items() if host == domain or host.endswith("." + domain)), None)
    if not platform:
        return None
    parts = [part.strip() for part in parsed.path.split("/") if part.strip()]
    lowered = [part.casefold() for part in parts]
    rejected_first = {
        "facebook": {"sharer", "sharer.php", "share.php", "dialog", "policy.php", "privacy", "events", "groups", "plugins"},
        "instagram": {"p", "reel", "reels", "stories", "explore"},
        "x": {"intent", "share", "home", "search", "i"},
    }
    if not parts or lowered[0] in rejected_first.get(platform, set()):
        return None
    if platform == "facebook" and lowered[0] == "profile.php":
        return None
    if platform == "linkedin" and (lowered[0] != "company" or len(parts) < 2):
        return None
    if platform == "youtube" and lowered[0] not in {"channel", "user", "c"} and not parts[0].startswith("@"):
        return None
    if host == "youtu.be":
        return None
    if platform == "tiktok" and not parts[0].startswith("@"):
        return None
    if platform == "x" and len(parts) != 1:
        return None
    canonical_host = {
        "linkedin": "linkedin.com",
        "facebook": "facebook.com",
        "instagram": "instagram.com",
        "x": "x.com",
        "youtube": "youtube.com",
        "tiktok": "tiktok.com",
    }[platform]
    if platform == "linkedin":
        parts = parts[:2]
    elif platform == "youtube":
        parts = parts[:1] if parts[0].startswith("@") else parts[:2]
    return {"platform": platform, "url": f"https://{canonical_host}/{'/'.join(parts)}"}


NORWEGIAN_MONTHS = {
    "januar": "01", "februar": "02", "mars": "03", "april": "04", "mai": "05", "juni": "06",
    "juli": "07", "august": "08", "september": "09", "oktober": "10", "november": "11", "desember": "12",
    "jan": "01", "feb": "02", "mar": "03", "apr": "04", "jun": "06", "jul": "07", "aug": "08",
    "sep": "09", "okt": "10", "nov": "11", "des": "12",
}


def extract_date_from_text(text: str) -> str | None:
    if not text:
        return None
    m = re.search(r"\b(202[0-6])[-/](\d{1,2})[-/](\d{1,2})(?:\b|T)", text)
    if m:
        return f"{m.group(1)}-{m.group(2).zfill(2)}-{m.group(3).zfill(2)}"
    m = re.search(r"\b(\d{1,2})\.(\d{1,2})\.(202[0-6])\b", text)
    if m:
        return f"{m.group(3)}-{m.group(2).zfill(2)}-{m.group(1).zfill(2)}"
    m = re.search(r"\b(\d{1,2})\.?\s+([a-zA-ZæøåÆØÅ]+)\s+(202[0-6])\b", text)
    if m:
        month_str = m.group(2).lower()
        if month_str in NORWEGIAN_MONTHS:
            return f"{m.group(3)}-{NORWEGIAN_MONTHS[month_str]}-{m.group(1).zfill(2)}"
    m = re.search(r"/(202[0-6])/(\d{2})/", text)
    if m:
        return f"{m.group(1)}-{m.group(2)}-01"
    return None


def extract_feed_news(feed_bytes: bytes, feed_url: str, base_url: str) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    feed_sha = hashlib.sha256(feed_bytes).hexdigest()
    try:
        root = ET.fromstring(feed_bytes)
    except Exception:
        return []

    # RSS 2.0 (<channel><item>)
    for item in root.findall(".//item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        pub = (item.findtext("pubDate") or "").strip()
        if not title or not link or len(title) < 8:
            continue
        abs_link = urllib.parse.urljoin(base_url, link)
        norm_date = None
        if pub:
            try:
                norm_date = parsedate_to_datetime(pub).astimezone(timezone.utc).date().isoformat()
            except Exception:
                pass
        if not norm_date:
            norm_date = extract_date_from_text(abs_link) or extract_date_from_text(pub)
        if not norm_date:
            continue

        items.append({
            "title": title[:200],
            "url": abs_link,
            "verification": "site_listing",
            "article_url": abs_link,
            "source_url": feed_url,
            "published_at": norm_date,
            "excerpt": (item.findtext("description") or title)[:300],
            "content_sha256": feed_sha,
        })

    # Atom (<feed><entry>)
    atom_entries = root.findall(".//{http://www.w3.org/2005/Atom}entry") or root.findall(".//entry")
    for entry in atom_entries:
        title = (
            entry.findtext("{http://www.w3.org/2005/Atom}title")
            or entry.findtext("title")
            or ""
        ).strip()
        link_el = entry.find("{http://www.w3.org/2005/Atom}link")
        if link_el is None:
            link_el = entry.find("link")
        link = link_el.get("href") if link_el is not None else ""
        pub = (
            entry.findtext("{http://www.w3.org/2005/Atom}published")
            or entry.findtext("published")
            or entry.findtext("{http://www.w3.org/2005/Atom}updated")
            or entry.findtext("updated")
            or ""
        ).strip()
        if not title or not link or len(title) < 8:
            continue
        abs_link = urllib.parse.urljoin(base_url, link)
        norm_date = None
        if pub:
            m = re.match(r"^(\d{4}-\d{2}-\d{2})", pub)
            if m:
                norm_date = m.group(1)
        if not norm_date:
            norm_date = extract_date_from_text(abs_link) or extract_date_from_text(pub)
        if not norm_date:
            continue

        items.append({
            "title": title[:200],
            "url": abs_link,
            "verification": "site_listing",
            "article_url": abs_link,
            "source_url": feed_url,
            "published_at": norm_date,
            "excerpt": title[:300],
            "content_sha256": feed_sha,
        })

    return items


def extract_website_news(page_url: str, soup: BeautifulSoup, raw_html: str, page_sha256: str | None = None) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    seen_titles: set[str] = set()
    lower_url = page_url.lower()
    if any(k in lower_url for k in ["/career", "/karriere", "/jobb", "/stilling", "/vacanc", "/work-with-us", "/ledige-stillinger"]):
        return []
    is_news_page = any(k in lower_url for k in ["aktuelt", "nyhet", "news", "press", "pressemelding", "innsikt", "blogg", "media", "artikkel", "siste-nytt"])
    real_page_sha = page_sha256 or hashlib.sha256(raw_html.encode("utf-8", errors="replace")).hexdigest()

    page_meta_date = None
    for meta_sel in (
        'meta[property="article:published_time"]',
        'meta[property="article:modified_time"]',
        'meta[name="publish-date"]',
        'meta[name="date"]',
        'meta[name="DC.date.issued"]',
        'meta[property="og:updated_time"]',
    ):
        m_node = soup.select_one(meta_sel)
        if m_node and m_node.get("content"):
            c_val = str(m_node.get("content")).strip()
            page_meta_date = extract_date_from_text(c_val) or (c_val[:10] if re.match(r"^\d{4}-\d{2}-\d{2}", c_val) else None)
            if page_meta_date:
                break

    selectors = [
        "article", ".article", ".post", ".news-item", ".post-excerpt",
        "[class*='news']", "[class*='aktuelt']", "[class*='nyhet']",
        ".card", ".entry",
    ]
    candidate_elements = soup.select(", ".join(selectors)) if is_news_page else soup.find_all("article")

    for container in candidate_elements:
        h = container.find(["h1", "h2", "h3", "h4"])
        if not h:
            continue
        title = h.get_text(" ", strip=True)
        if len(title) < 8 or len(title) > 200 or title.casefold() in seen_titles:
            continue
        if any(skip in title.casefold() for skip in ["meny", "kontakt", "om oss", "les mer", "vis alle", "søk", "filter", "del på"]):
            continue
        a = container.find("a", href=True) or h.find("a", href=True) or h.find_parent("a", href=True)
        link = urllib.parse.urljoin(page_url, a["href"]) if a else page_url
        if link.startswith(("tel:", "mailto:", "javascript:", "#")):
            continue
        if any(k in link.lower() for k in ["/career", "/karriere", "/jobb", "/stilling", "/vacanc"]):
            continue
        if _registered_domain(link) != _registered_domain(page_url):
            link = page_url

        time_tag = container.find("time")
        pub_date = None
        if time_tag:
            pub_date = time_tag.get("datetime") or extract_date_from_text(time_tag.get_text(" ", strip=True))
        if not pub_date:
            pub_date = extract_date_from_text(container.get_text(" ", strip=True))
        if not pub_date:
            # Only a date carried by the item itself (or its URL) counts; a hub page's
            # modified/published meta tag is not the article's publication date.
            pub_date = extract_date_from_text(link)

        if not pub_date or link.rstrip("/") == page_url.rstrip("/"):
            continue

        item_url = link
        seen_titles.add(title.casefold())
        items.append({
            "title": title,
            "url": item_url,
            "verification": "site_listing",
            "article_url": link,
            "source_url": page_url.rstrip("/"),
            "published_at": pub_date,
            "excerpt": container.get_text(" ", strip=True)[:300],
            "content_sha256": real_page_sha,
        })
        if len(items) >= 10:
            break

    # Fallback for news hub pages (/aktuelt, /nyheter) where headings are direct h2/h3 links
    if not items and is_news_page:
        fallback_date = None
        for meta_prop in ("article:published_time", "article:modified_time", "og:updated_time"):
            m_tag = soup.find("meta", attrs={"property": meta_prop})
            if m_tag and m_tag.get("content"):
                fallback_date = extract_date_from_text(str(m_tag["content"]))
                if fallback_date:
                    break
        if not fallback_date:
            for meta_name in ("date", "publish-date", "pubdate", "DC.date.issued"):
                m_tag = soup.find("meta", attrs={"name": meta_name})
                if m_tag and m_tag.get("content"):
                    fallback_date = extract_date_from_text(str(m_tag["content"]))
                    if fallback_date:
                        break
        for h in soup.find_all(["h2", "h3"]):
            title = h.get_text(" ", strip=True)
            if len(title) < 8 or len(title) > 200 or title.casefold() in seen_titles:
                continue
            if any(skip in title.casefold() for skip in ["meny", "kontakt", "om oss", "les mer", "vis alle", "søk", "filter", "del på", "cookie", "personvern"]):
                continue
            a = h.find("a", href=True) or h.find_parent("a", href=True)
            if not a:
                parent_box = h.find_parent(["div", "li", "section"])
                a = parent_box.find("a", href=True) if parent_box else None
            if not a:
                continue
            link = urllib.parse.urljoin(page_url, a["href"])
            if link.startswith(("tel:", "mailto:", "javascript:", "#")) or _registered_domain(link) != _registered_domain(page_url):
                continue
            if any(k in link.lower() for k in ["/career", "/karriere", "/jobb", "/stilling", "/vacanc"]):
                continue
            if link.rstrip("/") == page_url.rstrip("/") or is_hub_url(link):
                continue
            card_box = h.find_parent(["a", "div", "li", "article", "section"])
            card_date = (
                extract_date_from_text(link)
                or (extract_date_from_text(card_box.get_text(" ", strip=True)) if card_box else None)
                or fallback_date
            )
            if not card_date:
                continue
            seen_titles.add(title.casefold())
            items.append({
                "title": title,
                "url": link,
                "verification": "site_listing",
                "article_url": link,
                "source_url": page_url.rstrip("/"),
                "published_at": card_date,
                "excerpt": title,
                "content_sha256": real_page_sha,
            })
            if len(items) >= 6:
                break

    if not items and is_news_page:
        h1 = soup.find("h1")
        raw_title = h1.get_text(" ", strip=True) if h1 else (soup.title.get_text(" ", strip=True) if soup.title else "")
        if len(raw_title) >= 5 and not any(skip in raw_title.casefold() for skip in ["om oss", "kontakt", "cookie", "personvern", "meny", "forside"]):
            title = raw_title.split(" - ")[0].split(" | ")[0].strip()
            pub_date = page_meta_date or extract_date_from_text(raw_html[:3000])
            if pub_date and title.casefold() not in {"nyheter", "aktuelt", "news", "presse", "pressemeldinger", "siste nytt"}:
                items.append({
                    "title": title[:200],
                    "url": page_url,
                    "source_url": page_url,
                    "published_at": pub_date,
                    "verification": "site_listing",
                    "excerpt": (soup.find("main") or soup.find("article") or soup).get_text(" ", strip=True)[:300],
                    "content_sha256": real_page_sha,
                })

    return items


def extract_website_jobs(page_url: str, soup: BeautifulSoup, raw_html: str, page_sha256: str | None = None) -> list[dict[str, Any]]:
    jobs: list[dict[str, Any]] = []
    seen_titles: set[str] = set()
    is_career_page = any(k in page_url.lower() for k in ["stilling", "karriere", "jobb", "career", "vacanc", "work-with-us", "bli-med", "open-position", "rekruttering", "jobbe-hos-oss"])
    real_page_sha = page_sha256 or hashlib.sha256(raw_html.encode("utf-8", errors="replace")).hexdigest()

    selectors = [
        "[class*='job']", "[class*='stilling']", "[class*='vacancy']",
        ".position", ".opening",
    ]
    candidate_elements = soup.select(", ".join(selectors)) if is_career_page else []
    generic_job_titles = {
        "vacancies", "offene stellen", "jobb hos oss", "jobbe hos oss", "ledige stillinger",
        "karriere", "careers", "careers in", "work with us", "join our team", "our purpose",
        "contact us about careers", "meet the summer interns", "discover the stories",
        "åpen søknad", "open application",
    }

    for container in candidate_elements:
        h = container.find(["h2", "h3", "h4", "h5", "a"])
        if not h:
            continue
        title = h.get_text(" ", strip=True)
        if len(title) < 5 or len(title) > 150 or title.casefold() in seen_titles:
            continue
        lower_t = title.casefold()
        if lower_t in generic_job_titles or any(
            skip in lower_t
            for skip in [
                "meny", "kontakt", "om oss", "cookie", "personvern", "vis alle",
                "hopp til", "skip to", "hovedinnhold", "our purpose", "contact us",
                "discover the stories", "about us",
            ]
        ):
            continue
        a = container.find("a", href=True) or (h if h.name == "a" and h.get("href") else None)
        link = urllib.parse.urljoin(page_url, a["href"]) if a else page_url
        if link.startswith(("tel:", "mailto:", "javascript:", "#")):
            continue
        if any(bad in link.lower() for bad in ["/about-us", "/magazine", "/contact", "/kontakt", "/om-oss"]):
            continue
        seen_titles.add(lower_t)
        jobs.append({
            "title": title,
            "url": link,
            "source_url": page_url,
            "published_at": extract_date_from_text(container.get_text(" ", strip=True)) or None,
            "excerpt": container.get_text(" ", strip=True)[:300],
            "content_sha256": real_page_sha,
            "kind": "listing_card",
        })
        if len(jobs) >= 8:
            break

    # Detect Norwegian and international ATS portals (Webcruiter, Jobbnorge, Teamtailor, Recman, Easycruit, Finn, ReachMee, Talentech, etc.)
    ats_domains = (
        "webcruiter.no", "webcruiter.com", "jobbnorge.no", "teamtailor.com",
        "recman.no", "recman.io", "career.recman.no", "easycruit.com", "reachmee.com",
        "finn.no/jobb", "finn.no/job/", "finn.no/stilling", "finnkode=", "workbuster.com", "workbuster.se",
        "varbi.com", "jobylon.com", "homerun.co", "talentech.com", "talentech.io",
        "karriere.no", "personio.de", "personio.com", "bamboohr.com", "lever.co",
        "greenhouse.io", "workable.com", "smartrecruiters.com", "arbeidsplassen.nav.no/stillinger/stilling/",
    )
    candidate_elements = soup.select("a[href]") + soup.select("iframe[src]")
    for anchor in candidate_elements:
        href = str(anchor.get("href") or anchor.get("src") or "").strip()
        if any(ats in href.lower() for ats in ats_domains) and not href.startswith(("tel:", "mailto:", "javascript:", "#")):
            clean_href = urllib.parse.urljoin(page_url, href)
            parsed_ats = urllib.parse.urlparse(clean_href)
            a_text = anchor.get_text(" ", strip=True) or anchor.get("title") or ""
            
            # Intelligent title decoding from URL slug if anchor text is generic
            generic_texts = {"les mer", "søk her", "søk stilling", "apply here", "apply", "klikk her", "se stillinger", "open positions", "ledige stillinger"}
            if len(a_text) < 4 or a_text.casefold() in generic_texts:
                slug = parsed_ats.path.strip("/").split("/")[-1]
                slug_clean = re.sub(r"^[0-9_-]+|[0-9_-]+$", "", slug)
                slug_clean = re.sub(r"[-_]+", " ", slug_clean).strip()
                if len(slug_clean) >= 4 and not slug_clean.isdigit():
                    a_text = slug_clean.title()
                else:
                    ats_host = parsed_ats.netloc.removeprefix("www.")
                    a_text = f"Ledig stilling ({ats_host})"

            if a_text.casefold() not in seen_titles:
                seen_titles.add(a_text.casefold())
                jobs.append({
                    "title": a_text[:120],
                    "url": clean_href,
                    "source_url": page_url,
                    "published_at": None,
                    "excerpt": f"Link to recruitment ATS page {clean_href} found on {page_url}",
                    "content_sha256": real_page_sha,
                    "kind": "ats_ad",
                })
                if len(jobs) >= 8:
                    break

    return jobs


def _jsonld_jobs(metadata: dict[str, Any], page_url: str, page_sha256: str | None = None) -> list[dict[str, Any]]:
    jobs: list[dict[str, Any]] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            kind = node.get("@type")
            kinds = set(kind if isinstance(kind, list) else [kind])
            if "JobPosting" in kinds and node.get("title"):
                title = str(node.get("title") or "").strip()
                pub = str(node.get("datePosted") or "")
                pub_iso = extract_date_from_text(pub) or (pub[:10] if re.match(r"^\d{4}-\d{2}-\d{2}", pub) else None)
                url = urllib.parse.urljoin(page_url, str(node.get("url") or "")) if node.get("url") else page_url
                excerpt = str(node.get("description") or node.get("responsibilities") or "")[:300]
                digest = page_sha256 or hashlib.sha256(f"{title}|{url}".encode("utf-8")).hexdigest()
                hiring = node.get("hiringOrganization")
                hiring_name = str(hiring.get("name") or "") if isinstance(hiring, dict) else str(hiring or "")
                jobs.append({
                    "title": title,
                    "url": url,
                    "source_url": page_url,
                    "published_at": pub_iso,
                    "excerpt": excerpt,
                    "content_sha256": digest,
                    "kind": "jsonld_job",
                    "hiring_org": hiring_name,
                })
            for child in node.values():
                walk(child)
        elif isinstance(node, list):
            for child in node:
                walk(child)

    walk(metadata.get("json-ld", []))
    walk(metadata.get("microdata", []))
    return jobs


def _jsonld_news(metadata: dict[str, Any], page_url: str, page_sha256: str | None = None) -> list[dict[str, Any]]:
    articles: list[dict[str, Any]] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            kind = node.get("@type")
            kinds = set(kind if isinstance(kind, list) else [kind])
            if kinds & {"NewsArticle", "Article", "BlogPosting"} and (node.get("headline") or node.get("name")):
                title = str(node.get("headline") or node.get("name") or "").strip()
                pub = str(node.get("datePublished") or node.get("dateCreated") or "")
                pub_iso = extract_date_from_text(pub) or (pub[:10] if re.match(r"^\d{4}-\d{2}-\d{2}", pub) else None)
                url = urllib.parse.urljoin(page_url, str(node.get("url") or "")) if node.get("url") else page_url
                excerpt = str(node.get("description") or node.get("articleBody") or "")[:300]
                digest = page_sha256 or hashlib.sha256(f"{title}|{url}".encode("utf-8")).hexdigest()
                if pub_iso:
                    articles.append({
                        "title": title,
                        "url": url,
                        "source_url": page_url,
                        "published_at": pub_iso,
                        "excerpt": excerpt,
                        "content_sha256": digest,
                        "verification": "site_listing",
                    })
            for child in node.values():
                walk(child)
        elif isinstance(node, list):
            for child in node:
                walk(child)

    walk(metadata.get("json-ld", []))
    walk(metadata.get("microdata", []))
    return articles


def _discover_sitemap_urls(base_url: str, timeout: float = 3.0) -> tuple[list[str], list[str]]:
    """Discovers news and career URLs from /sitemap.xml (and nested sitemaps) if available."""
    news_urls: list[str] = []
    career_urls: list[str] = []
    parsed = urllib.parse.urlparse(base_url)
    sitemap_url = urllib.parse.urlunparse((parsed.scheme, parsed.netloc, "/sitemap.xml", "", "", ""))
    try:
        req = urllib.request.Request(sitemap_url, headers={"User-Agent": USER_AGENT})
        with _safe_urlopen(req, timeout=timeout) as resp:
            content_type = resp.headers.get("content-type", "").lower()
            if "xml" in content_type or "text" in content_type or "/sitemap" in resp.geturl().lower():
                raw_xml = resp.read(1_000_000).decode("utf-8", errors="replace")
                locs = re.findall(r"<loc>(https?://[^<]+)</loc>", raw_xml, re.I)

                # If sitemap index with nested sitemaps, probe post/news/career sub-sitemap
                if "<sitemap>" in raw_xml.lower():
                    sub_sitemaps = re.findall(r"<sitemap>\s*<loc>(https?://[^<]+)</loc>", raw_xml, re.I)
                    for sub_url in sub_sitemaps:
                        if any(t in sub_url.lower() for t in ["post", "news", "nyhet", "karriere", "jobb"]):
                            try:
                                sub_req = urllib.request.Request(sub_url.strip(), headers={"User-Agent": USER_AGENT})
                                with _safe_urlopen(sub_req, timeout=timeout) as sub_resp:
                                    sub_xml = sub_resp.read(500_000).decode("utf-8", errors="replace")
                                    locs.extend(re.findall(r"<loc>(https?://[^<]+)</loc>", sub_xml, re.I))
                                    break
                            except Exception:
                                pass

                news_terms = ("/nyhet", "/news", "/aktuelt", "/presse", "/pressemelding", "/artikkel", "/article", "/blogg", "/siste-nytt")
                career_terms = ("/karriere", "/career", "/jobb", "/stilling", "/vacanc", "/ledig")
                candidate_news = [l.strip() for l in locs if any(t in l.lower() for t in news_terms)]
                leaf_news = [l for l in candidate_news if len(l.strip("/").split("/")) > 4 and "-" in l.strip("/").split("/")[-1]]
                candidate_careers = [l.strip() for l in locs if any(t in l.lower() for t in career_terms)]

                for l in (leaf_news or candidate_news):
                    if l not in news_urls and len(news_urls) < 3:
                        news_urls.append(l)

                for l in candidate_careers:
                    if l not in career_urls and len(career_urls) < 3:
                        career_urls.append(l)
    except Exception:
        pass
    return news_urls, career_urls


def _priority_links(base_url: str, soup: BeautifulSoup, limit: int = 3, check_sitemap: bool = False) -> list[str]:
    base_reg_domain = _registered_domain(base_url)
    career_terms = ("karriere", "jobb", "careers", "vacancies", "stillinger", "ledige-stillinger", "work-with-us", "bli-med-pa-laget", "open-positions", "rekruttering", "jobbe-hos-oss")
    news_terms = ("aktuelt", "nyheter", "siste-nytt", "news", "pressemelding", "pressemeldinger", "innsikt-og-nyheter", "artikler", "blogg", "press", "presse", "media", "publikasjoner")
    about_terms = ("om-oss", "om_oss", "about", "kontakt", "contact", "ledelse", "management", "team", "people", "locations", "lokasjoner", "avdelinger", "butikker")

    career_cands: dict[str, int] = {}
    news_cands: dict[str, int] = {}
    other_cands: dict[str, int] = {}

    for link_tag in soup.find_all("link", rel=lambda r: r and "alternate" in r):
        t = (link_tag.get("type") or "").lower()
        if ("rss" in t or "atom" in t) and "oembed" not in t:
            href = link_tag.get("href")
            if href and "oembed" not in str(href).lower() and "wp-json" not in str(href).lower():
                abs_feed = urllib.parse.urljoin(base_url, href)
                if abs_feed.startswith(("http://", "https://")) and _registered_domain(abs_feed) == base_reg_domain:
                    news_cands[abs_feed] = -1

    for anchor in soup.select("a[href]"):
        href = str(anchor.get("href") or "").strip()
        url = urllib.parse.urljoin(base_url, href)
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme not in {"http", "https"} or _registered_domain(url) != base_reg_domain:
            continue
        clean = urllib.parse.urlunparse((parsed.scheme, parsed.netloc, parsed.path or "/", "", "", ""))
        if clean.rstrip("/") == base_url.rstrip("/"):
            continue

        path_lower = parsed.path.casefold()
        lang_penalty = 50 if any(path_lower.startswith(f"/{lg}/") for lg in ("de", "fr", "es", "it", "pl", "nl", "ru", "zh", "ja")) else 0
        haystack = (path_lower + " " + anchor.get_text(" ", strip=True)).casefold()

        c_rank = next((i for i, t in enumerate(career_terms) if t in haystack), None)
        if c_rank is not None and not any(bad in haystack for bad in ("pa-jobb", "paa-jobb", "på-jobb", "på jobb")):
            score = c_rank + lang_penalty
            career_cands[clean] = min(score, career_cands.get(clean, score))
            continue

        n_rank = next((i for i, t in enumerate(news_terms) if t in haystack), None)
        if n_rank is not None:
            score = n_rank + lang_penalty
            news_cands[clean] = min(score, news_cands.get(clean, score))
            continue

        o_rank = next((i for i, t in enumerate(about_terms) if t in haystack), None)
        if o_rank is not None:
            score = o_rank + lang_penalty
            other_cands[clean] = min(score, other_cands.get(clean, score))

    # Only fetch /sitemap.xml if neither a news link nor a career link was found in HTML
    if check_sitemap and len(news_cands) == 0 and len(career_cands) == 0:
        s_news, s_careers = _discover_sitemap_urls(base_url)
        for u in s_careers:
            if u not in career_cands:
                career_cands[u] = 5
        for u in s_news:
            if u not in news_cands:
                news_cands[u] = 5

    sorted_careers = [u for u, _ in sorted(career_cands.items(), key=lambda x: x[1])]
    sorted_news = [u for u, _ in sorted(news_cands.items(), key=lambda x: x[1])]
    sorted_others = [u for u, _ in sorted(other_cands.items(), key=lambda x: x[1])]

    selected: list[str] = []
    for bucket in (
        sorted_careers[:1],
        sorted_news[:1],
        sorted_others[:1],
        sorted_news[1:2],
        sorted_careers[1:2],
        sorted_others[1:],
    ):
        for u in bucket:
            if u not in selected and len(selected) < limit:
                selected.append(u)

    return selected[:limit]


def _fetch_secondary_page(url: str, *, homepage_domain: str, timeout: float, max_bytes: int) -> tuple[dict[str, Any] | None, list[dict[str, str]], int, int, int, str | None]:
    if not _robots_allowed(url, timeout):
        return None, [], 1, 0, 0, "robots.txt disallows page"
    started = time.monotonic()
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"})
    try:
        with _safe_urlopen(request, timeout=timeout) as response:
            raw = response.read(max_bytes + 1)
            elapsed = int((time.monotonic() - started) * 1000)
            final_url = response.geturl()
            ctype = response.headers.get("content-type", "").lower()
            is_feed = ("xml" in ctype or "rss" in ctype or "atom" in ctype) and "html" not in ctype
            if len(raw) > max_bytes or (not is_feed and "html" not in ctype):
                return None, [], 2, len(raw), elapsed, "unsupported or oversized page"
            if _registered_domain(final_url) != homepage_domain:
                return None, [], 2, len(raw), elapsed, "redirected outside registered domain"
        page_sha = hashlib.sha256(raw).hexdigest()
        if is_feed:
            feed_news = extract_feed_news(raw, final_url, final_url)
            page = {
                "url": final_url,
                "title": "RSS News Feed",
                "main_text_excerpt": "",
                "content_sha256": page_sha,
                "news_items": feed_news,
                "job_postings": [],
            }
            return page, [], 2, len(raw), elapsed, None
        page_html = raw.decode("utf-8", errors="replace")
        page_soup = BeautifulSoup(page_html, "lxml")
        page_text = trafilatura.extract(page_html, url=final_url, include_links=False, include_tables=False, favor_precision=True) or ""
        if not page_text.strip() or (page_text.strip().startswith("{") and '"fr":' in page_text):
            clean_soup = BeautifulSoup(page_html, "lxml")
            for tag in clean_soup(["script", "style", "noscript", "svg"]):
                tag.decompose()
            lines = [ln.strip() for ln in clean_soup.get_text("\n").splitlines() if ln.strip() and not (ln.strip().startswith("{") and '"fr":' in ln)]
            page_text = "\n".join(lines)
        page_sha = hashlib.sha256(raw).hexdigest()
        page = {
            "url": final_url,
            "title": page_soup.title.get_text(" ", strip=True)[:500] if page_soup.title else "",
            "main_text_excerpt": page_text[:5000],
            "content_sha256": page_sha,
            "news_items": extract_website_news(final_url, page_soup, page_html, page_sha256=page_sha),
            "job_postings": extract_website_jobs(final_url, page_soup, page_html, page_sha256=page_sha),
        }
        return page, _social_links(final_url, page_soup), 2, len(raw), elapsed, None
    except Exception as exc:
        return None, [], 2, 0, int((time.monotonic() - started) * 1000), f"{type(exc).__name__}: {str(exc)[:120]}"


def _jsonld_organisations(metadata: dict[str, Any]) -> list[dict[str, Any]]:
    values: list[dict[str, Any]] = []

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            kind = value.get("@type")
            kinds = set(kind if isinstance(kind, list) else [kind])
            if kinds & {"Organization", "Corporation", "LocalBusiness", "Store", "Restaurant"}:
                values.append(value)
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(metadata.get("json-ld", []))
    return values[:20]


def _extraction_state(text: str, soup: BeautifulSoup) -> str:
    return "js_fallback_candidate" if len(text.strip()) < 100 and len(soup.select("script[src]")) >= 2 else "static_complete"


def fetch_website(url: str | None, *, timeout: float = 15.0, max_bytes: int = 2_000_000) -> tuple[dict[str, Any], dict[str, Any]]:
    supplied_url = str(url or "").strip()
    supplied_scheme = bool(re.match(r"^https?://", supplied_url, re.I))
    normalized = normalize_homepage(url)
    if not normalized:
        return evidence("website", "not_found", "registry_linked_company_website", "https://data.brreg.no/enhetsregisteret/api/enheter", note="No valid registry website URL"), {"requests": 0, "bytes": 0, "latencies_ms": []}
    try:
        assert_public_url(normalized)
    except ValueError as exc:
        return evidence("website", "blocked", "registry_linked_company_website", normalized, note=str(exc)), {"requests": 0, "bytes": 0, "latencies_ms": []}
    if not _robots_allowed(normalized, timeout):
        return evidence("website", "blocked", "registry_linked_company_website", normalized, note="robots.txt disallows this user agent"), {"requests": 1, "bytes": 0, "latencies_ms": []}
    started = time.monotonic()
    request = urllib.request.Request(normalized, headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"})
    try:
        with _safe_urlopen(request, timeout=timeout) as response:
            content_type = response.headers.get("content-type", "")
            raw = response.read(max_bytes + 1)
            elapsed = int((time.monotonic() - started) * 1000)
            if len(raw) > max_bytes:
                return evidence("website", "blocked", "registry_linked_company_website", normalized, note="Homepage exceeds byte limit"), {"requests": 2, "bytes": len(raw), "latencies_ms": [elapsed]}
            if "html" not in content_type.lower():
                return evidence("website", "source_error", "registry_linked_company_website", normalized, note=f"Unsupported content type: {content_type}"), {"requests": 2, "bytes": len(raw), "latencies_ms": [elapsed]}
            final_url = response.geturl()
            assert_public_url(final_url)
        html = raw.decode("utf-8", errors="replace")
        soup = BeautifulSoup(html, "lxml")
        structured = extruct.extract(html, base_url=final_url, syntaxes=["json-ld", "microdata", "opengraph"])
        text = trafilatura.extract(html, url=final_url, include_links=False, include_tables=False, favor_precision=True) or ""
        title = soup.title.get_text(" ", strip=True) if soup.title else ""
        description_tag = soup.select_one('meta[name="description"], meta[property="og:description"]')
        description = str(description_tag.get("content") or "").strip() if description_tag else ""
        html_social = _social_links(final_url, soup)
        struct_social = structured_social_links(structured)
        combined_social = html_social + struct_social
        seen_soc: set[tuple[str, str]] = set()
        dedup_soc: list[dict[str, str]] = []
        for s in combined_social:
            k = (s["platform"], s["url"].lower())
            if k not in seen_soc:
                seen_soc.add(k)
                dedup_soc.append(s)

        homepage_sha = __import__("hashlib").sha256(raw).hexdigest()
        value = {
            "requested_url": normalized,
            "final_url": final_url,
            "registered_domain": _registered_domain(final_url),
            "title": title[:500],
            "description": description[:2000],
            "main_text_excerpt": text[:5000],
            "social_links": dedup_soc,
            "structured_organisations": _jsonld_organisations(structured),
            "content_sha256": homepage_sha,
            "extraction_state": _extraction_state(text, soup),
        }
        homepage_news = _jsonld_news(structured, final_url, page_sha256=homepage_sha) + extract_website_news(final_url, soup, html, page_sha256=homepage_sha)
        homepage_jobs = _jsonld_jobs(structured, final_url, page_sha256=homepage_sha) + extract_website_jobs(final_url, soup, html, page_sha256=homepage_sha)
        all_news_items = list(homepage_news)
        all_job_postings = list(homepage_jobs)

        pages = [{
            "url": final_url,
            "title": title[:500],
            "main_text_excerpt": text[:5000],
            "content_sha256": value["content_sha256"],
            "news_items": homepage_news,
            "job_postings": homepage_jobs,
        }]
        social = value["social_links"]
        crawl_errors = []
        requests = 2
        bytes_received = len(raw)
        page_latencies = [elapsed]
        homepage_domain = value["registered_domain"]
        fast_path_saturated = bool(len(homepage_news) >= 1 and len(homepage_jobs) >= 1 and len(social) >= 1)
        subpage_limit = 1 if fast_path_saturated else 2
        check_sitemap = not fast_path_saturated
        for page_url in _priority_links(final_url, soup, limit=subpage_limit, check_sitemap=check_sitemap):
            page, page_social, page_requests, page_bytes, page_elapsed, page_error = _fetch_secondary_page(
                page_url,
                homepage_domain=homepage_domain,
                timeout=timeout,
                max_bytes=min(max_bytes, 1_000_000),
            )
            requests += page_requests
            bytes_received += page_bytes
            if page_elapsed:
                page_latencies.append(page_elapsed)
            if page:
                pages.append(page)
                social.extend(page_social)
                all_news_items.extend(page.get("news_items") or [])
                all_job_postings.extend(page.get("job_postings") or [])
            elif page_error:
                crawl_errors.append({"url": page_url, "error": page_error})
        value["pages"] = pages
        value["social_links"] = list({(item["platform"], item["url"]): item for item in social}.values())

        # Deduplicate news items by title and prioritize specific dated articles over index pages
        seen_news: set[str] = set()
        dedup_news: list[dict[str, Any]] = []
        for n in all_news_items:
            if not n.get("published_at"):
                continue
            k = n["title"].casefold()
            if k not in seen_news:
                seen_news.add(k)
                dedup_news.append(n)
        dedup_news.sort(
            key=lambda x: (
                0 if len(str(x.get("url") or "").strip("/").split("/")) > 4 else 1,
                0 if not str(x.get("title") or "").casefold().startswith(("nyheter", "aktuelt", "siste nytt", "news", "press")) else 1,
            )
        )

        # Deduplicate job postings by title
        seen_jobs: set[str] = set()
        dedup_jobs: list[dict[str, Any]] = []
        for j in all_job_postings:
            k = j["title"].casefold()
            if k not in seen_jobs:
                seen_jobs.add(k)
                dedup_jobs.append(j)

        value["news_items"] = dedup_news
        value["job_postings"] = dedup_jobs
        value["crawl_errors"] = crawl_errors
        for pg in pages[1:]:
            pu = str(pg.get("url") or "")
            pul = pu.lower()
            if any(k in pul for k in ("/karriere", "/career", "/vacanc", "/stilling", "/jobber", "jobb-hos-oss", "jobbe-hos-oss", "ledige-stillinger")) and not any(b in pul for b in ("pa-jobb", "paa-jobb", "på-jobb")):
                value["careers_url"] = pu
                break
        return evidence("website", "available", "registry_linked_company_website", final_url, value=value, note="Company-controlled claim layer; not an official registry fact", content_sha256=value["content_sha256"]), {"requests": requests, "bytes": bytes_received, "latencies_ms": page_latencies}
    except urllib.error.HTTPError as exc:
        elapsed = int((time.monotonic() - started) * 1000)
        status = "not_found" if exc.code in {404, 410} else "source_error"
        return evidence("website", status, "registry_linked_company_website", normalized, note=f"HTTP {exc.code}"), {"requests": 2, "bytes": 0, "latencies_ms": [elapsed]}
    except urllib.error.URLError as exc:
        if not supplied_scheme and normalized.startswith("https://"):
            first_elapsed = int((time.monotonic() - started) * 1000)
            record, metrics = fetch_website("http://" + supplied_url, timeout=timeout, max_bytes=max_bytes)
            metrics["requests"] += 2
            metrics["latencies_ms"].insert(0, first_elapsed)
            return record, metrics
        elapsed = int((time.monotonic() - started) * 1000)
        return evidence("website", "source_error", "registry_linked_company_website", normalized, note=f"URLError: {str(exc.reason)[:180]}"), {"requests": 2, "bytes": 0, "latencies_ms": [elapsed]}
    except Exception as exc:
        elapsed = int((time.monotonic() - started) * 1000)
        return evidence("website", "source_error", "registry_linked_company_website", normalized, note=f"{type(exc).__name__}: {str(exc)[:180]}"), {"requests": 2, "bytes": 0, "latencies_ms": [elapsed]}
