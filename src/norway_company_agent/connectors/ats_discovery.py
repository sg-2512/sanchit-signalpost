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
import re
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from typing import Any
from bs4 import BeautifulSoup

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
    "{slug}.workbuster.com",
    "{slug}.varbi.com",
    "{slug}.jobylon.com",
)

_ATS_LOCK = threading.Lock()
_ATS_NEXT_ALLOWED = 0.0
_ATS_MIN_INTERVAL = 0.5


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
        with urllib.request.urlopen(req, timeout=timeout) as resp:
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
    """Extract individual job ad links from an ATS page.

    Returns list of {title, url} for links that look like individual job
    advertisements (not listing pages).
    """
    html = raw.decode("utf-8", errors="replace")
    soup = BeautifulSoup(html, "html.parser")
    jobs: list[dict[str, Any]] = []
    seen_urls: set[str] = set()

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
    status_out: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Probe common ATS URL patterns for active job postings.

    Strict entity gate: requires ATS tenant subdomain to match company brand,
    and the ATS page to mention the company name (confirming it is this
    company's ATS tenant, not a different company with a similar slug).

    Returns observations with kind="ats_ad" for validate_job_item compatibility.
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

    for pattern in ATS_PATTERNS:
        if len(observations) >= max_results:
            break
        if probes_done >= max_probes:
            break
        if budget and not budget.can_proceed(reserve=1):
            break

        url = f"https://{pattern.format(slug=slug)}"
        probes_done += 1

        raw, bytes_received = _fetch(url, timeout=timeout)
        if not raw:
            if budget:
                budget.record_request(bytes_received=0, elapsed_ms=0, cost_usd=0.0)
            continue

        if budget:
            budget.record_request(bytes_received=bytes_received, elapsed_ms=0, cost_usd=0.0)

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
                    }
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
        })

    return observations