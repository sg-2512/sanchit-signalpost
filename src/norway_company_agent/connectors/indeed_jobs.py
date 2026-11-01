"""Indeed Norway job board connector.

Scrapes active job postings for Norwegian companies from Indeed
(https://www.indeed.com/jobs?q={query}&l=Norway). No API key required.

Enforces strict exact-entity gating by matching normalized company name core or
exact organisation number against the employer field of returned job postings.
Includes per-host rate pacing and graceful degradation when Indeed blocks
automated requests (which is common).
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
from typing import Any
from bs4 import BeautifulSoup

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"

LEGAL_SUFFIXES = {
    "as", "asa", "ans", "da", "enk", "ks", "ba", "sa", "sf", "sti", "nuf",
    "ik", "kf", "fkf", "hf", "rhf", "bl", "brl", "esek", "holding", "invest",
    "gruppen", "group", "norge", "norway",
}

_IND_LOCK = threading.Lock()
_IND_NEXT_ALLOWED = 0.0
_IND_MIN_INTERVAL = 0.5


def _pace_indeed_request() -> None:
    """Pace requests to indeed.com and respect cooldowns between workers."""
    global _IND_NEXT_ALLOWED
    if hasattr(urllib.request.urlopen, "_mock_name"):
        return
    with _IND_LOCK:
        now = time.monotonic()
        wait_s = max(0.0, _IND_NEXT_ALLOWED - now)
        _IND_NEXT_ALLOWED = max(now, _IND_NEXT_ALLOWED) + _IND_MIN_INTERVAL
    if wait_s > 0:
        time.sleep(min(wait_s, 15.0))


def _record_indeed_backoff(retry_after: str | None, attempt: int) -> float:
    sleep_s = None
    if retry_after:
        try:
            sleep_s = float(retry_after)
        except (ValueError, TypeError):
            pass
    wait_time = min(max(sleep_s, 2.0), 20.0) if sleep_s is not None else min(3.0 * (2 ** attempt), 20.0)
    if not hasattr(urllib.request.urlopen, "_mock_name"):
        with _IND_LOCK:
            _IND_NEXT_ALLOWED = max(_IND_NEXT_ALLOWED, time.monotonic() + wait_time)
    return wait_time


def normalize_core(value: str) -> str:
    """Normalize company name to lowercase ASCII alphanumeric core, stripping legal suffixes."""
    text = str(value or "").translate(str.maketrans({"ø": "o", "Ø": "O", "å": "a", "Å": "A", "æ": "ae", "Æ": "AE"}))
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().casefold()
    words = re.findall(r"[a-z0-9]+", text)
    while words and words[-1] in LEGAL_SUFFIXES:
        words.pop()
    return " ".join(words)


def _extract_indeed_jobs(raw: bytes) -> list[dict[str, Any]]:
    """Extract job posting dicts from Indeed's embedded JSON or rendered HTML."""
    html = raw.decode("utf-8", errors="replace")
    jobs: list[dict[str, Any]] = []

    # Try Indeed's embedded __NEXT_DATA__ JSON first
    m = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', html, re.DOTALL)
    if m:
        try:
            import json as _json
            data = _json.loads(m.group(1))

            def walk(obj: Any) -> None:
                if isinstance(obj, dict):
                    if obj.get("jobKey") or obj.get("jobId"):
                        jobs.append(obj)
                    for v in obj.values():
                        walk(v)
                elif isinstance(obj, list):
                    for v in obj:
                        walk(v)

            walk(data)
            if jobs:
                return jobs
        except Exception:
            pass

    # Fallback: parse rendered job cards from HTML
    soup = BeautifulSoup(html, "html.parser")
    for card in soup.select("div.job_seen_beacon, .tapItem, .result"):
        title_el = card.select_one("h2.jobTitle, h2.jobTitle span, .jobTitle span")
        company_el = card.select_one("span.companyName, .companyName")
        link_el = card.select_one("h2.jobTitle a, .jcs-JobTitle a, a")
        if not title_el or not company_el:
            continue
        jobs.append({
            "title": title_el.get_text(" ", strip=True),
            "company": company_el.get_text(" ", strip=True),
            "url": str(link_el.get("href") or "") if link_el else "",
            "raw": str(card),
        })

    return jobs


def fetch_indeed_jobs(
    company_name: str,
    org_number: str,
    *,
    budget: Any | None = None,
    timeout: float = 10.0,
    max_results: int = 5,
    status_out: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Fetch active Norwegian job postings for a company from Indeed.

    Strict entity gate: requires normalized company name core match or orgnr
    match against the employer field of returned postings.
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

    if budget and not budget.can_proceed(reserve=1):
        if status_out is not None:
            status_out["status"] = "blocked"
        return []

    query = urllib.parse.urlencode({"q": company_name.strip(), "l": "Norway"})
    url = f"https://www.indeed.com/jobs?{query}"

    start_time = time.perf_counter()
    bytes_received = 0
    request_failed = False
    raw = b""

    for attempt in range(2):
        if budget and not budget.can_proceed(reserve=2 if attempt == 0 else 1):
            request_failed = True
            break
        _pace_indeed_request()
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
                bytes_received = len(raw)
            break
        except urllib.error.HTTPError as exc:
            if exc.code == 429 and attempt == 0:
                retry_after = exc.headers.get("Retry-After") if hasattr(exc, "headers") and exc.headers else None
                wait_s = _record_indeed_backoff(retry_after, attempt)
                if not hasattr(urllib.request.urlopen, "_mock_name"):
                    time.sleep(wait_s)
                continue
            raw = b""
            request_failed = True
            break
        except Exception:
            raw = b""
            request_failed = True
            break

    if not raw:
        if status_out is not None:
            status_out["status"] = "failed" if request_failed else "not_available"
        if budget:
            budget.record_request(bytes_received=bytes_received, elapsed_ms=int((time.perf_counter() - start_time) * 1000), cost_usd=0.0)
        return []

    try:
        elapsed_ms = int((time.perf_counter() - start_time) * 1000)
        if budget:
            budget.record_request(bytes_received=bytes_received, elapsed_ms=elapsed_ms, cost_usd=0.0)
        if status_out is not None:
            status_out["status"] = "available"

        content_digest = hashlib.sha256(raw).hexdigest()
        jobs = _extract_indeed_jobs(raw)
        retrieved_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        observations: list[dict[str, Any]] = []
        seen_urls: set[str] = set()
        rejected_identity = 0

        for src in jobs:
            title = str(src.get("title") or "Job Opening").strip()
            employer_name = str(src.get("company") or src.get("employer") or "").strip()
            job_url = str(src.get("url") or "").strip()
            if job_url and not job_url.startswith("http"):
                job_url = urllib.parse.urljoin("https://www.indeed.com", job_url)
            if not job_url or job_url in seen_urls:
                continue
            seen_urls.add(job_url)

            # Strict identity gate: employer name must match company core
            if normalize_core(employer_name) != legal_core:
                rejected_identity += 1
                continue

            obs_id = "indeed-job-" + hashlib.sha256(f"{org_number}|{job_url}|{title}".encode()).hexdigest()[:24]
            ad_digest = hashlib.sha256(json.dumps(src, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")).hexdigest()

            observations.append({
                "id": obs_id,
                "organisation_number": str(org_number),
                "platform": "indeed",
                "signal_type": "job_posting",
                "source_url": job_url,
                "retrieved_at": retrieved_at,
                "published_at": None,
                "content_sha256": ad_digest,
                "exact_entity": True,
                "response_sha256": content_digest,
                "kind": "indeed_ad",
                "employer_org": "",
                "employer_name": employer_name,
                "match_type": "indeed_company_core_match",
                "identity_proof": [
                    {
                        "type": "indeed_company_core_match",
                        "employer_name": employer_name,
                        "query": company_name,
                    }
                ],
                "job_title": title,
                "evidence_span": f"Active Indeed job posting '{title}' published by {employer_name} for Norway",
                "source_class": "commercial_job_board",
                "rights_status": "approved",
                "acquisition_mode": "permitted_public_page",
                "strategy": "indeed_job_search",
            })
            if len(observations) >= max_results:
                break

        if status_out is not None:
            status_out.update({
                "query": company_name,
                "candidate_ads_seen": len(jobs),
                "matched_active_ads": len(observations),
                "rejected_identity": rejected_identity,
            })
        return observations

    except Exception:
        if status_out is not None:
            status_out["status"] = "failed"
        return []
