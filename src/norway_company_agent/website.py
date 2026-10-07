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

from bs4 import BeautifulSoup
import extruct
import tldextract
import trafilatura

from .evidence import evidence

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
        if "ssl" in exc_str or "certificate" in exc_str or "hostname" in exc_str:
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


def _registered_domain(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    ext = tldextract.extract(parsed.hostname or "")
    return ext.top_domain_under_public_suffix


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
    m = re.search(r"\b(202[0-6])[-/](\d{1,2})[-/](\d{1,2})\b", text)
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


def extract_website_news(page_url: str, soup: BeautifulSoup, raw_html: str) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    seen_titles: set[str] = set()
    is_news_page = any(k in page_url.lower() for k in ["aktuelt", "nyhet", "news", "press", "pressemelding", "innsikt", "blogg", "media", "artikkel", "siste-nytt"])

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
        if _registered_domain(link) != _registered_domain(page_url):
            link = page_url

        time_tag = container.find("time")
        pub_date = None
        if time_tag:
            pub_date = time_tag.get("datetime") or extract_date_from_text(time_tag.get_text(" ", strip=True))
        if not pub_date:
            pub_date = extract_date_from_text(container.get_text(" ", strip=True))
        if not pub_date:
            img = container.find("img", src=True)
            if img:
                pub_date = extract_date_from_text(img["src"])
        if not pub_date and is_news_page:
            pub_date = time.strftime("%Y-%m-%d", time.gmtime())

        seen_titles.add(title.casefold())
        digest = hashlib.sha256(f"{title}|{link}".encode("utf-8")).hexdigest()
        items.append({
            "title": title,
            "url": link,
            "published_at": pub_date,
            "excerpt": container.get_text(" ", strip=True)[:300],
            "content_sha256": digest,
        })
        if len(items) >= 10:
            break

    if not items and is_news_page:
        h1 = soup.find("h1")
        raw_title = h1.get_text(" ", strip=True) if h1 else (soup.title.get_text(" ", strip=True) if soup.title else "")
        if len(raw_title) >= 5 and not any(skip in raw_title.casefold() for skip in ["om oss", "kontakt", "cookie", "personvern", "meny", "forside"]):
            title = raw_title.split(" - ")[0].split(" | ")[0].strip()
            pub_date = extract_date_from_text(raw_html[:3000]) or time.strftime("%Y-%m-%d", time.gmtime())
            digest = hashlib.sha256(f"{title}|{page_url}".encode("utf-8")).hexdigest()
            items.append({
                "title": title[:200],
                "url": page_url,
                "published_at": pub_date,
                "excerpt": (soup.find("main") or soup.find("article") or soup).get_text(" ", strip=True)[:300],
                "content_sha256": digest,
            })

    return items


def extract_website_jobs(page_url: str, soup: BeautifulSoup, raw_html: str) -> list[dict[str, Any]]:
    jobs: list[dict[str, Any]] = []
    seen_titles: set[str] = set()
    is_career_page = any(k in page_url.lower() for k in ["stilling", "karriere", "jobb", "career", "vacanc", "work-with-us", "bli-med", "open-position", "rekruttering", "jobbe-hos-oss"])

    selectors = [
        "[class*='job']", "[class*='stilling']", "[class*='career']", "[class*='vacancy']",
        ".position", ".opening", "article",
    ]
    candidate_elements = soup.select(", ".join(selectors)) if is_career_page else []

    for container in candidate_elements:
        h = container.find(["h2", "h3", "h4", "h5", "a"])
        if not h:
            continue
        title = h.get_text(" ", strip=True)
        if len(title) < 5 or len(title) > 150 or title.casefold() in seen_titles:
            continue
        if any(skip in title.casefold() for skip in ["meny", "kontakt", "om oss", "cookie", "personvern", "vis alle", "hopp til", "skip to", "hovedinnhold"]):
            continue
        a = container.find("a", href=True) or (h if h.name == "a" and h.get("href") else None)
        link = urllib.parse.urljoin(page_url, a["href"]) if a else page_url
        if link.startswith(("tel:", "mailto:", "javascript:", "#")):
            continue
        seen_titles.add(title.casefold())
        digest = hashlib.sha256(f"{title}|{link}".encode("utf-8")).hexdigest()
        jobs.append({
            "title": title,
            "url": link,
            "published_at": time.strftime("%Y-%m-%d", time.gmtime()),
            "excerpt": container.get_text(" ", strip=True)[:300],
            "content_sha256": digest,
        })
        if len(jobs) >= 8:
            break

    # If career page but no individual job cards, treat general recruitment page as active job vacancy
    if not jobs and is_career_page:
        h1 = soup.find("h1")
        raw_title = h1.get_text(" ", strip=True) if h1 else (soup.title.get_text(" ", strip=True) if soup.title else "Ledige stillinger")
        if any(skip in raw_title.casefold() for skip in ["hopp til", "skip to", "hovedinnhold"]):
            raw_title = soup.title.get_text(" ", strip=True) if soup.title else "Jobb og karriere"
        title = raw_title.split(" - ")[0].split(" | ")[0].strip() or "Ledige stillinger"
        digest = hashlib.sha256(raw_html.encode("utf-8", errors="replace")).hexdigest()
        jobs.append({
            "title": title[:100],
            "url": page_url,
            "published_at": time.strftime("%Y-%m-%d", time.gmtime()),
            "excerpt": soup.get_text(" ", strip=True)[:400],
            "content_sha256": digest,
        })

    # Detect Norwegian and international ATS portals (Webcruiter, Jobbnorge, Teamtailor, Recman, Easycruit, Finn, ReachMee, etc.)
    ats_domains = (
        "webcruiter.no", "webcruiter.com", "jobbnorge.no", "teamtailor.com",
        "recman.no", "recman.io", "easycruit.com", "reachmee.com", "finn.no/jobb",
        "bamboohr.com", "lever.co", "greenhouse.io", "workable.com",
    )
    candidate_elements = soup.select("a[href]") + soup.select("iframe[src]")
    for anchor in candidate_elements:
        href = str(anchor.get("href") or anchor.get("src") or "").strip()
        if any(ats in href.lower() for ats in ats_domains) and not href.startswith(("tel:", "mailto:", "javascript:", "#")):
            clean_href = urllib.parse.urljoin(page_url, href)
            a_text = anchor.get_text(" ", strip=True) or anchor.get("title") or "Ledig stilling"
            if len(a_text) < 4 or a_text.casefold() in {"les mer", "søk her", "søk stilling", "apply here", "apply", "klikk her"}:
                parsed_ats = urllib.parse.urlparse(clean_href)
                a_text = f"Ledig stilling ({parsed_ats.netloc})"
            if a_text.casefold() not in seen_titles:
                seen_titles.add(a_text.casefold())
                digest = hashlib.sha256(f"{a_text}|{clean_href}".encode("utf-8")).hexdigest()
                jobs.append({
                    "title": a_text[:120],
                    "url": clean_href,
                    "published_at": time.strftime("%Y-%m-%d", time.gmtime()),
                    "excerpt": f"External recruitment ATS posting at {clean_href}",
                    "content_sha256": digest,
                })
                if len(jobs) >= 8:
                    break

    return jobs


def _jsonld_jobs(metadata: dict[str, Any], page_url: str) -> list[dict[str, Any]]:
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
                digest = hashlib.sha256(f"{title}|{url}".encode("utf-8")).hexdigest()
                jobs.append({
                    "title": title,
                    "url": url,
                    "published_at": pub_iso or time.strftime("%Y-%m-%d", time.gmtime()),
                    "excerpt": excerpt,
                    "content_sha256": digest,
                })
            for child in node.values():
                walk(child)
        elif isinstance(node, list):
            for child in node:
                walk(child)

    walk(metadata.get("json-ld", []))
    walk(metadata.get("microdata", []))
    return jobs


def _jsonld_news(metadata: dict[str, Any], page_url: str) -> list[dict[str, Any]]:
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
                digest = hashlib.sha256(f"{title}|{url}".encode("utf-8")).hexdigest()
                articles.append({
                    "title": title,
                    "url": url,
                    "published_at": pub_iso or time.strftime("%Y-%m-%d", time.gmtime()),
                    "excerpt": excerpt,
                    "content_sha256": digest,
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
    base = urllib.parse.urlparse(base_url)
    career_terms = ("karriere", "jobb", "careers", "vacancies", "stillinger", "ledige-stillinger", "work-with-us", "bli-med-pa-laget", "open-positions", "rekruttering", "jobbe-hos-oss")
    news_terms = ("news", "press", "aktuelt", "nyheter", "pressemelding", "pressemeldinger", "innsikt-og-nyheter", "media", "presse", "artikler", "siste-nytt", "blogg", "publikasjoner")
    about_terms = ("om-oss", "om_oss", "about", "kontakt", "contact", "ledelse", "management", "team", "people", "locations", "lokasjoner", "avdelinger", "butikker")

    career_cands: dict[str, int] = {}
    news_cands: dict[str, int] = {}
    other_cands: dict[str, int] = {}

    for anchor in soup.select("a[href]"):
        href = str(anchor.get("href") or "").strip()
        url = urllib.parse.urljoin(base_url, href)
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme not in {"http", "https"} or parsed.netloc.lower() != base.netloc.lower():
            continue
        clean = urllib.parse.urlunparse((parsed.scheme, parsed.netloc, parsed.path or "/", "", "", ""))
        if clean.rstrip("/") == base_url.rstrip("/"):
            continue

        haystack = (parsed.path + " " + anchor.get_text(" ", strip=True)).casefold()

        c_rank = next((i for i, t in enumerate(career_terms) if t in haystack), None)
        if c_rank is not None:
            career_cands[clean] = min(c_rank, career_cands.get(clean, c_rank))
            continue

        n_rank = next((i for i, t in enumerate(news_terms) if t in haystack), None)
        if n_rank is not None:
            news_cands[clean] = min(n_rank, news_cands.get(clean, n_rank))
            continue

        o_rank = next((i for i, t in enumerate(about_terms) if t in haystack), None)
        if o_rank is not None:
            other_cands[clean] = min(o_rank, other_cands.get(clean, o_rank))

    # If fewer than 2 news links or fewer than 2 career links found in HTML and sitemap check enabled
    if check_sitemap and (len(news_cands) < 2 or len(career_cands) < 2):
        s_news, s_careers = _discover_sitemap_urls(base_url)
        for u in s_careers:
            if u not in career_cands:
                career_cands[u] = 5
        for u in s_news:
            if u not in news_cands:
                news_cands[u] = 5

    selected: list[str] = []
    # Up to 2 career links guaranteed
    for u, _ in sorted(career_cands.items(), key=lambda x: x[1])[:2]:
        selected.append(u)
    # Up to 2 news links guaranteed
    for u, _ in sorted(news_cands.items(), key=lambda x: x[1])[:2]:
        if u not in selected:
            selected.append(u)
    # Fill remaining slots up to limit with other priority links
    for u, _ in sorted(other_cands.items(), key=lambda x: x[1]):
        if len(selected) >= limit:
            break
        if u not in selected:
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
            if len(raw) > max_bytes or "html" not in response.headers.get("content-type", "").lower():
                return None, [], 2, len(raw), elapsed, "unsupported or oversized page"
            if _registered_domain(final_url) != homepage_domain:
                return None, [], 2, len(raw), elapsed, "redirected outside registered domain"
        page_html = raw.decode("utf-8", errors="replace")
        page_soup = BeautifulSoup(page_html, "lxml")
        page_text = trafilatura.extract(page_html, url=final_url, include_links=False, include_tables=False, favor_precision=True) or ""
        page = {
            "url": final_url,
            "title": page_soup.title.get_text(" ", strip=True)[:500] if page_soup.title else "",
            "main_text_excerpt": page_text[:5000],
            "content_sha256": hashlib.sha256(raw).hexdigest(),
            "news_items": extract_website_news(final_url, page_soup, page_html),
            "job_postings": extract_website_jobs(final_url, page_soup, page_html),
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

        value = {
            "requested_url": normalized,
            "final_url": final_url,
            "registered_domain": _registered_domain(final_url),
            "title": title[:500],
            "description": description[:2000],
            "main_text_excerpt": text[:5000],
            "social_links": dedup_soc,
            "structured_organisations": _jsonld_organisations(structured),
            "content_sha256": __import__("hashlib").sha256(raw).hexdigest(),
            "extraction_state": _extraction_state(text, soup),
        }
        homepage_news = _jsonld_news(structured, final_url) + extract_website_news(final_url, soup, html)
        homepage_jobs = _jsonld_jobs(structured, final_url) + extract_website_jobs(final_url, soup, html)
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
        for page_url in _priority_links(final_url, soup, check_sitemap=True):
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

        # Deduplicate news items by title
        seen_news: set[str] = set()
        dedup_news: list[dict[str, Any]] = []
        for n in all_news_items:
            k = n["title"].casefold()
            if k not in seen_news:
                seen_news.add(k)
                dedup_news.append(n)

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
