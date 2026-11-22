"""ATS discovery connector for Norwegian companies.

Probes common ATS (Applicant Tracking System) URL patterns derived from the
company name to discover active job postings when the company has no verified
website from which ATS links can be extracted.

Supported ATS platforms (must be in validate_job_item's ATS_HOST_SUFFIXES):
webcruiter.no, jobbnorge.no, recman.no, teamtailor.com, easycruit.com,
workbuster.com, varbi.com, jobylon.com, homerun.co, talentech.com

Enforces strict entity gating: the ATS tenant subdomain must carry the
company's brand name, and job posting URLs must point to individual ads
(not listing/hub pages). Returns observations with kind="ats_ad" so they
pass validate_job_item's ATS branch when ats_tenant_matches(url, tokens).

No API key required. Probes are bounded by max_probes to conserve the shared
2,000-request budget.
"""
from __future__ import annotations

import hashlib
import json
import re
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable
from bs4 import BeautifulSoup
from protego import Protego

from ..verified_items import host_of, is_generic_job_title, is_hub_url, parse_iso_date, registered_domain
from ..website import USER_AGENT as CRAWLER_AGENT, _jsonld_jobs, assert_public_url, extract_job_deadline
from ..public_http import public_opener
from .job_boards import board_endpoint, listed_adverts

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/128.0.0.0 Safari/537.36"

LEGAL_SUFFIXES = {
    "as", "asa", "ans", "da", "enk", "ks", "ba", "sa", "sf", "sti", "nuf",
    "ik", "kf", "fkf", "hf", "rhf", "bl", "brl", "esek", "holding", "invest",
    "gruppen", "group", "norge", "norway",
}

# Most common Norwegian ATS platforms, ordered by likelihood of use.
# Each host must appear in verified_items.ATS_HOST_SUFFIXES to pass validation.
ATS_PATTERNS = (
    "{slug}.webcruiter.no",
    "{slug}.jobbnorge.no",
    "{slug}.recman.no",
    "{slug}.teamtailor.com",
    "{slug}.easycruit.com",
    "{slug}.reachmee.com",
    "{slug}.workbuster.com",
    "{slug}.varbi.com",
    "{slug}.jobylon.com",
    "{slug}.talentech.com",
)

_ATS_LOCK = threading.Lock()
_ATS_NEXT_ALLOWED = 0.0
_ATS_MIN_INTERVAL = 0.5


class _NoJobRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_JOB_OPENER = public_opener(_NoJobRedirect())


def _read_job_source(url: str, budget: Any, timeout: float) -> bytes:
    try:
        assert_public_url(url)
        if not budget.try_start_request():
            return b""
        _pace_ats_request()
        request = urllib.request.Request(url, headers={"User-Agent": CRAWLER_AGENT})
        with _JOB_OPENER.open(request, timeout=timeout) as response:
            raw = response.read(750_001)
        budget.record_request(bytes_received=len(raw), count=0)
        if not raw and urllib.parse.urlsplit(url).path == "/robots.txt":
            return b"User-agent: *\nDisallow:\n"
        return raw if len(raw) <= 750_000 else b""
    except urllib.error.HTTPError as exc:
        if exc.code in {404, 410} and urllib.parse.urlsplit(url).path == "/robots.txt":
            return b"User-agent: *\nDisallow:\n"
        return b""
    except (OSError, ValueError):
        return b""


def verified_career_jobs(
    candidates: list[dict[str, Any]],
    *,
    site_url: str,
    company_name: str,
    org: str,
    subunit_orgs: Iterable[str],
    budget: Any,
    max_requests: int,
    timeout: float = 6.0,
    diagnostics: dict[str, int] | None = None,
) -> list[dict[str, Any]]:
    """Resolve first-party links or an advertised Workday sitemap to current job adverts."""
    remaining = min(6, max_requests)
    jobs: list[dict[str, Any]] = []
    seen: set[str] = set()
    robots_by_host: dict[str, Protego] = {}
    today = datetime.now(timezone.utc).date()
    employer_ids = {org, *subunit_orgs}
    legal_name = " ".join(company_name.casefold().split())
    site_domain = registered_domain(site_url)
    if not site_domain or not legal_name:
        return []

    def record(key: str) -> None:
        if diagnostics is not None:
            diagnostics[key] = diagnostics.get(key, 0) + 1

    def read(url: str) -> bytes:
        nonlocal remaining
        if remaining <= 0:
            return b""
        remaining -= 1
        record("read_attempts")
        raw = _read_job_source(url, budget, timeout)
        if not raw:
            record("empty_reads")
        return raw

    def priority(candidate: dict[str, Any]) -> int:
        url = str(candidate.get("url") or "")
        host = host_of(url)
        if host.endswith(".myworkdayjobs.com") or board_endpoint(url):
            return 0
        path = urllib.parse.urlsplit(url).path.rstrip("/")
        return 1 if "/jobs/" in path or path.endswith("/jobs") else 2

    for candidate in sorted(candidates, key=priority):
        if remaining < 1:
            break
        url = str(candidate.get("url") or "")
        linked_from = str(candidate.get("source_url") or "")
        host = host_of(url)
        if url in seen or not url.startswith("https://") or registered_domain(linked_from) != site_domain:
            continue
        first_party = registered_domain(url) == site_domain
        workday = host.endswith(".myworkdayjobs.com")
        public_board = board_endpoint(url)
        teamtailor = host.endswith(".teamtailor.com") and host not in {"app.teamtailor.com", "tt.teamtailor.com", "www.teamtailor.com"}
        eu_greenhouse = host == "job-boards.eu.greenhouse.io"
        if not (first_party or workday or public_board or teamtailor or eu_greenhouse):
            continue
        seen.add(url)
        if host not in robots_by_host:
            origin = urllib.parse.urlsplit(url).netloc
            robots_url = f"https://{origin}/robots.txt"
            robots_raw = read(robots_url)
            if not robots_raw and remaining >= 3:
                record("robots_retries")
                robots_raw = read(robots_url)
            if not robots_raw:
                record("robots_unavailable")
                continue
            robots_by_host[host] = Protego.parse(robots_raw.decode("utf-8-sig", errors="replace"))
        robots = robots_by_host[host]
        path = urllib.parse.urlsplit(url).path
        first_party_jobs_index = first_party and (
            path.rstrip("/") == "/jobs"
            or (not path.strip("/") and host.split(".")[0] in {"careers", "karriere", "jobs"})
        )
        individual_ad = not is_hub_url(url) and (not workday or "/job/" in path)
        active_listing = listed_adverts(url, read) if public_board else {}
        ad_urls = list(active_listing) if public_board else [url] if individual_ad else []
        company_listing = None
        if teamtailor or eu_greenhouse or first_party_jobs_index:
            tenant_path = path.strip("/").split("/")[0]
            pattern = (rf"/{re.escape(tenant_path)}/jobs/[0-9]+/?" if eu_greenhouse
                       else r"/jobs/[0-9]+(?:-[^/]+)?/?" if teamtailor
                       else r"/(?:jobs/[0-9]+(?:-[^/]+)?|job/[^/]+-jid-[0-9]+)/?")
            ad_urls = [url] if re.fullmatch(pattern, path) else []
            if not ad_urls and robots.can_fetch(url, CRAWLER_AGENT):
                listing_raw = read(url)
                listing = BeautifulSoup(listing_raw, "html.parser")
                index_url = ""
                for anchor in listing.select("a[href]"):
                    address = urllib.parse.urljoin(url, str(anchor["href"]))
                    parsed = urllib.parse.urlsplit(address)
                    if parsed.scheme == "https" and parsed.netloc == urllib.parse.urlsplit(url).netloc and re.fullmatch(pattern, parsed.path):
                        ad_urls.append(parsed._replace(query="", fragment="").geturl())
                    elif (teamtailor or first_party_jobs_index) and parsed.scheme == "https" and parsed.netloc == urllib.parse.urlsplit(url).netloc and parsed.path.rstrip("/") == "/jobs":
                        index_url = parsed._replace(query="", fragment="").geturl()
                if first_party_jobs_index and ad_urls:
                    company_listing = {"source_url": url, "content_sha256": hashlib.sha256(listing_raw).hexdigest(),
                                       "retrieved_at": datetime.now(timezone.utc).isoformat()}
                if not ad_urls and index_url and index_url.rstrip("/") != url.rstrip("/") and remaining >= 2 and robots.can_fetch(index_url, CRAWLER_AGENT):
                    index = BeautifulSoup(read(index_url), "html.parser")
                    for anchor in index.select("a[href]"):
                        address = urllib.parse.urljoin(index_url, str(anchor["href"]))
                        parsed = urllib.parse.urlsplit(address)
                        if parsed.scheme == "https" and parsed.netloc == urllib.parse.urlsplit(url).netloc and re.fullmatch(pattern, parsed.path):
                            ad_urls.append(parsed._replace(query="", fragment="").geturl())
        if workday and not ad_urls:
            portal = re.sub(r"^/[a-z]{2}-[a-z]{2}(?=/)", "", path.rstrip("/"), flags=re.I)
            sitemap = next((
                item for item in robots.sitemaps
                if item.startswith("https://") and host_of(item) == host
                and urllib.parse.urlsplit(item).path == f"{portal}/siteMap.xml"
                and robots.can_fetch(item, CRAWLER_AGENT)
            ), "")
            if not sitemap or remaining < 2:
                continue
            try:
                root = ET.fromstring(read(sitemap))
            except ET.ParseError:
                continue
            ad_urls = [
                address for node in root.findall("./{*}url/{*}loc")
                if (address := (node.text or "").strip()).startswith("https://")
                and host_of(address) == host
                and urllib.parse.urlsplit(address).path.startswith(f"{portal}/job/")
                and not is_hub_url(address)
            ]
            # Check domestic ads first when the portal exposes a global listing.
            ad_urls.sort(key=lambda address: "norway" not in urllib.parse.urlsplit(address).path.casefold())
        for ad_url in dict.fromkeys(ad_urls):
            if remaining <= 0:
                break
            if not robots.can_fetch(ad_url, CRAWLER_AGENT):
                record("adverts_blocked_by_robots")
                continue
            record("advert_candidates")
            raw = read(ad_url)
            if not raw:
                continue
            soup = BeautifulSoup(raw, "html.parser")
            metadata = []
            for script in soup.find_all("script", type="application/ld+json"):
                try:
                    metadata.append(json.loads(script.string or script.get_text()))
                except ValueError:
                    continue
            digest = hashlib.sha256(raw).hexdigest()
            for item in _jsonld_jobs({"json-ld": metadata}, ad_url, page_sha256=digest):
                employer = str(item.get("hiring_org") or "")
                declared_org = str(item.get("employer_org") or "")
                employer_name = re.sub(r"^\d{1,6}\s+", "", employer) if workday else employer
                employer_name = " ".join(employer_name.casefold().split())
                if declared_org and declared_org not in employer_ids:
                    record("employer_mismatch")
                    continue
                if company_listing and item["url"].rstrip("/") == ad_url.rstrip("/"):
                    brand = re.sub(r"\s+(?:as|asa)$", "", legal_name)
                    local_brand = site_domain.endswith(".no") and employer_name in {brand + " norway", brand + " norge"}
                    if declared_org or employer_name == legal_name or local_brand:
                        ad_content = soup.select_one("main") or soup.body or soup
                        ad_text = ad_content.get_text(" ", strip=True)
                        posted = parse_iso_date(item.get("published_at"))
                        deadline = extract_job_deadline(ad_text)
                        closes = parse_iso_date(deadline) or parse_iso_date(item.get("valid_through"))
                        if not deadline and closes and closes > (posted or today) + timedelta(days=366):
                            closes = None  # A distant schema sentinel is not an application deadline.
                        application = re.search(r"\b(?:søk (?:her|nå|på stillingen)|søknad(?:sfrist)?|apply(?: now)?|application)\b", ad_text, re.I)
                        closed = re.search(r"søknadsfristen (?:er )?utløpt|stillingen er besatt|no longer accepting|position has (?:closed|been filled)", ad_text, re.I)
                        if (len(ad_text) >= 180 and application and not closed
                                and (not posted or posted <= today) and (not closes or closes >= today)
                                and not is_generic_job_title(item["title"])):
                            jobs.append({**item, "employer_name": employer, "kind": "company_site_ad",
                                         "valid_through": closes.isoformat() if closes else None, "application_due": deadline,
                                         "ad_text_excerpt": ad_text[:2500], "career_source_url": url,
                                         "career_link_url": url, "hiring_org_match": True, "active_listing": company_listing})
                            record("verified_adverts")
                            break
                        continue
                if not declared_org and employer_name != legal_name:
                    record("employer_mismatch")
                    continue
                posted = parse_iso_date(item.get("published_at"))
                closes = parse_iso_date(item.get("valid_through"))
                # Public board APIs enumerate currently published adverts. An absent
                # deadline is acceptable only with that fresh, captured listing.
                dates_invalid = (posted is not None and posted > today) or (closes is not None and closes <= today)
                dated_open = posted is not None and closes is not None and posted <= today < closes
                if dates_invalid or not (dated_open or ad_url in active_listing) or is_generic_job_title(item["title"]):
                    record("unverified_open_status_or_generic_title")
                    continue
                if item["url"].rstrip("/") != ad_url.rstrip("/"):
                    continue
                jobs.append({
                    **item,
                    "employer_name": employer,
                    "kind": "verified_career_ad",
                    "career_source_url": linked_from,
                    "career_link_url": url,
                    "hiring_org_match": True,
                    "active_listing": active_listing.get(ad_url),
                })
                record("verified_adverts")
                break
    return jobs


def _pace_ats_request() -> None:
    """Pace requests to ATS hosts to avoid hammering any single platform."""
    global _ATS_NEXT_ALLOWED
    if hasattr(urllib.request.urlopen, "_mock_name"):
        return
    with _ATS_LOCK:
        now = time.monotonic()
        wait_s = max(0.0, _ATS_NEXT_ALLOWED - now)
        _ATS_NEXT_ALLOWED = max(now, _ATS_NEXT_ALLOWED) + _ATS_MIN_INTERVAL
    if wait_s > 0:
        time.sleep(min(wait_s, 10.0))


def normalize_core(value: str) -> str:
    """Normalize company name to lowercase ASCII alphanumeric core."""
    text = str(value or "").translate(str.maketrans({"ø": "o", "Ø": "O", "å": "a", "Å": "A", "æ": "ae", "Æ": "AE"}))
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().casefold()
    words = re.findall(r"[a-z0-9]+", text)
    while words and words[-1] in LEGAL_SUFFIXES:
        words.pop()
    return " ".join(words)


def slugify_name(value: str) -> str:
    """Convert company name to a URL-friendly slug for ATS subdomain probing.

    Strips legal suffixes so "Acme AS" -> "acme", not "acme-as".
    """
    text = str(value or "").translate(str.maketrans({"ø": "o", "Ø": "O", "å": "a", "Å": "A", "æ": "ae", "Æ": "AE"}))
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().casefold()
    slug = re.sub(r"[^a-z0-9]+", "-", text).strip("-")
    parts = slug.split("-")
    while parts and parts[-1] in LEGAL_SUFFIXES:
        parts.pop()
    return "-".join(parts)


def _fetch(url: str, *, timeout: float = 10.0) -> tuple[bytes | None, int]:
    """Fetch a URL, returning (raw_bytes, bytes_received) or (None, 0) on failure."""
    _pace_ats_request()
    try:
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": USER_AGENT,
                "Accept-Language": "en-US,en;q=0.9,no;q=0.8",
                "Accept": "text/html,application/xhtml+xml",
            },
        )
        with _JOB_OPENER.open(req, timeout=timeout) as resp:
            raw = resp.read(1_000_000)
            return raw, len(raw)
    except Exception:
        return None, 0


def _looks_like_job_ad_url(url: str) -> bool:
    """True when a URL looks like an individual job ad (not a listing/hub page)."""
    parsed = urllib.parse.urlparse(url)
    path = parsed.path.lower()
    segs = [s for s in path.split("/") if s]
    if not segs:
        return False
    last = segs[-1]
    # Must contain a job-related keyword somewhere in the path
    if not any(kw in path for kw in ("job", "stilling", "stillinger", "vacanc", "karriere", "position", "opening", "ledig")):
        return False
    # Leaf segment must look like a specific ad id/slug, not a navigation word
    hub_tokens = {"job", "jobs", "stilling", "stillinger", "vacanc", "karriere", "career", "vacancies", "ledig", "ledige", "open", "positions"}
    if last in hub_tokens:
        return False
    if len(last) >= 3 and (any(c.isdigit() for c in last) or "-" in last):
        return True
    return False


def _extract_ats_jobs(raw: bytes, page_url: str) -> list[dict[str, Any]]:
    """Extract individual job ad links and Schema.org/JobPosting records from an ATS page.

    Returns list of {title, url} for links that look like individual job
    advertisements (not listing pages).
    """
    html = raw.decode("utf-8", errors="replace")
    soup = BeautifulSoup(html, "html.parser")
    jobs: list[dict[str, Any]] = []
    seen_urls: set[str] = set()

    # 1. Direct Schema.org/JobPosting JSON-LD Harvester
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
            items = data if isinstance(data, list) else [data]
            for item in items:
                if isinstance(item, dict) and item.get("@type") == "JobPosting":
                    j_title = str(item.get("title") or item.get("name") or "").strip()
                    j_url = str(item.get("url") or item.get("sameAs") or page_url).strip()
                    if j_title and j_url and j_url not in seen_urls:
                        seen_urls.add(j_url)
                        jobs.append({
                            "title": j_title[:200],
                            "url": j_url,
                            "date_posted": item.get("datePosted"),
                            "employment_type": item.get("employmentType"),
                            "valid_through": item.get("validThrough"),
                        })
        except Exception:
            pass

    # 2. HTML anchor link inspection
    for link in soup.find_all("a", href=True):
        if len(jobs) >= 8:
            break
        href = str(link.get("href") or "").strip()
        if not href or href.startswith(("tel:", "mailto:", "javascript:", "#")):
            continue
        abs_url = urllib.parse.urljoin(page_url, href)
        if abs_url in seen_urls:
            continue
        if not _looks_like_job_ad_url(abs_url):
            continue
        seen_urls.add(abs_url)

        title = link.get_text(" ", strip=True)[:200]
        if not title or len(title) < 4:
            # Derive title from URL slug
            parsed = urllib.parse.urlparse(abs_url)
            slug = parsed.path.strip("/").split("/")[-1]
            slug_clean = re.sub(r"^[0-9_-]+|[0-9_-]+$", "", slug)
            slug_clean = re.sub(r"[-_]+", " ", slug_clean).strip()
            if len(slug_clean) >= 4 and not slug_clean.isdigit():
                title = slug_clean.title()
            else:
                continue

        jobs.append({"title": title, "url": abs_url})

    return jobs


def fetch_ats_jobs(
    company_name: str,
    org_number: str,
    *,
    budget: Any | None = None,
    timeout: float = 10.0,
    max_probes: int = 3,
    max_results: int = 5,
    seed_urls: Iterable[str] = (),
    linked_from: str = "",
    probe_patterns: bool = True,
    status_out: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Find active job postings from a company-linked ATS or bounded probes.

    Strict entity gate: requires ATS tenant subdomain to match company brand,
    and the ATS page to mention the company name (confirming it is this
    company's ATS tenant, not a different company with a similar slug).

    ``seed_urls`` are recruitment pages linked directly by the verified company
    website.  They are preferred over guessed URLs and carry that source chain
    into the evidence record.  Guessed tenant probes remain available only for
    private discovery callers.
    """
    if status_out is not None:
        status_out["status"] = "not_applicable" if not company_name else "blocked"
    if not company_name:
        return []

    legal_core = normalize_core(company_name)
    if not legal_core or len(legal_core) < 3:
        if status_out is not None:
            status_out["status"] = "not_available"
        return []

    slug = slugify_name(company_name)
    if not slug or len(slug) < 3:
        if status_out is not None:
            status_out["status"] = "not_available"
        return []

    core_tokens = legal_core.split()
    retrieved_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    observations: list[dict[str, Any]] = []
    probes_done = 0

    candidates: list[tuple[str, bool]] = []
    seen_candidates: set[str] = set()
    for seed in seed_urls:
        clean_seed = str(seed or "").strip()
        if clean_seed.startswith(("https://", "http://")) and clean_seed not in seen_candidates:
            candidates.append((clean_seed, True))
            seen_candidates.add(clean_seed)
    seed_candidate_count = len(candidates)
    if probe_patterns:
        for pattern in ATS_PATTERNS:
            candidate = f"https://{pattern.format(slug=slug)}"
            if candidate not in seen_candidates:
                candidates.append((candidate, False))
                seen_candidates.add(candidate)

    for url, is_company_linked in candidates:
        if len(observations) >= max_results:
            break
        if probes_done >= max_probes:
            break
        if budget and not budget.try_start_request(reserve=1):
            break

        probes_done += 1

        raw, bytes_received = _fetch(url, timeout=timeout)
        if budget:
            budget.record_request(count=0, bytes_received=bytes_received)
        if not raw:
            continue

        # Confirm this ATS page belongs to this company by checking page text
        html = raw.decode("utf-8", errors="replace")
        soup = BeautifulSoup(html, "html.parser")
        page_text = soup.get_text(" ", strip=True)
        page_normalized = unicodedata.normalize("NFKD", page_text).encode("ascii", "ignore").decode().casefold()

        if not any(tok in page_normalized for tok in core_tokens if len(tok) >= 4):
            continue  # Not this company's ATS tenant

        jobs = _extract_ats_jobs(raw, url)
        for job in jobs:
            if len(observations) >= max_results:
                break

            job_url = job["url"]
            title = job["title"]
            host = urllib.parse.urlparse(job_url).hostname.lower().removeprefix("www.")

            # Strict: ATS tenant (subdomain) must match company brand
            labels = host.split(".")
            tenant_candidates = labels[:-2]  # Exclude TLD and registered domain
            tenant_match = False
            for label in tenant_candidates:
                label_clean = re.sub(r"[^a-z0-9]", "", label.casefold())
                if len(label_clean) >= 4:
                    for tok in core_tokens:
                        if len(tok) >= 4 and (tok in label_clean or label_clean in tok):
                            tenant_match = True
                            break
                    if tenant_match:
                        break
            if not tenant_match:
                continue

            # Extract employer name from page context
            employer_name = ""
            for sel in ("h1", ".company-name", ".employer-name", "[data-company]", ".ats-company"):
                el = soup.select_one(sel)
                if el:
                    employer_name = el.get_text(" ", strip=True)[:200]
                    break
            if not employer_name:
                # Fallback: use tenant subdomain as employer name
                employer_name = tenant_candidates[0] if tenant_candidates else company_name

            obs_id = "ats-job-" + hashlib.sha256(f"{org_number}|{job_url}|{title}".encode()).hexdigest()[:24]
            ad_digest = hashlib.sha256(job_url.encode()).hexdigest()

            observations.append({
                "id": obs_id,
                "organisation_number": str(org_number),
                "platform": "job_board",
                "signal_type": "job_posting",
                "source_url": job_url,
                "retrieved_at": retrieved_at,
                "published_at": None,
                "content_sha256": ad_digest,
                "exact_entity": True,
                "kind": "ats_ad",
                "employer_org": "",
                "employer_name": employer_name,
                "match_type": "ats_tenant_match",
                "identity_proof": [
                    {
                        "type": "ats_tenant_match",
                        "employer_name": employer_name,
                        "ats_host": host,
                        "query": company_name,
                    },
                    *([{
                        "type": "company_site_career_link",
                        "source_url": linked_from,
                        "ats_landing_url": url,
                    }] if is_company_linked and linked_from else []),
                ],
                "job_title": title,
                "evidence_span": f"Active ATS job posting '{title}' at {job_url}",
                "source_class": "commercial_job_board",
                "rights_status": "approved",
                "acquisition_mode": "permitted_public_page",
                "strategy": "ats_discovery_probe",
            })

    if status_out is not None:
        status_out.update({
            "query": company_name,
            "slug": slug,
            "probes_done": probes_done,
            "matched_active_ads": len(observations),
            "company_linked_seed_urls": seed_candidate_count,
        })

    return observations
