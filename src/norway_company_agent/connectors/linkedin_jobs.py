"""LinkedIn Guest Jobs Search connector.

Completely free ($0.00 spend, no credentials needed).
Queries https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search
with exact normalized company core gating to discover active job openings.
"""
from __future__ import annotations

import hashlib
import json
import re
import socket
import time
import unicodedata
import urllib.parse
import urllib.request
from typing import Any
from bs4 import BeautifulSoup

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
LEGAL_SUFFIXES = {
    "as", "asa", "ba", "da", "enk", "iks", "nuf", "sa", "sam",
    "sti", "stiftelsen", "holding", "eiendom",
}


def normalize_core(value: str) -> str:
    """Normalize company name to lowercase ASCII alphanumeric core, stripping legal suffixes."""
    text = str(value or "").translate(str.maketrans({"ø": "o", "Ø": "O", "å": "a", "Å": "A", "æ": "ae", "Æ": "AE"}))
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().casefold()
    words = re.findall(r"[a-z0-9]+", text)
    while words and words[-1] in LEGAL_SUFFIXES:
        words.pop()
    return " ".join(words)


def fetch_linkedin_jobs(
    company_name: str,
    org_number: str,
    *,
    budget: Any | None = None,
    timeout: float = 6.0,
    max_results: int = 5,
) -> list[dict[str, Any]]:
    """Fetch active Norwegian job postings for a company from LinkedIn guest search."""
    if not company_name:
        return []

    legal_core = normalize_core(company_name)
    if not legal_core or len(legal_core) < 3:
        return []

    if budget and not budget.can_proceed():
        return []

    query = urllib.parse.urlencode({
        "keywords": company_name.strip(),
        "location": "Norway",
        "start": 0,
    })
    url = f"https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search?{query}"

    start_time = time.perf_counter()
    bytes_received = 0
    try:
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": USER_AGENT,
                "Accept-Language": "en-US,en;q=0.9,no;q=0.8",
            },
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read(500_000)
            bytes_received = len(raw)

        elapsed_ms = int((time.perf_counter() - start_time) * 1000)
        if budget:
            budget.record_request(bytes_received=bytes_received, elapsed_ms=elapsed_ms, cost_usd=0.0)

        content_digest = hashlib.sha256(raw).hexdigest()
        soup = BeautifulSoup(raw, "html.parser")
        cards = soup.select("li")
        retrieved_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        observations: list[dict[str, Any]] = []

        for card in cards:
            company_elem = card.select_one("h4.base-search-card__subtitle")
            employer_name = company_elem.get_text(strip=True) if company_elem else ""
            if normalize_core(employer_name) != legal_core:
                continue

            title_elem = card.select_one("h3.base-search-card__title")
            title = title_elem.get_text(strip=True) if title_elem else "Job Opening"

            link_elem = card.select_one("a.base-card__full-link")
            job_url = str(link_elem.get("href") or "").split("?")[0] if link_elem else "https://www.linkedin.com/jobs"

            time_elem = card.select_one("time")
            posted_at = str(time_elem.get("datetime") or "") if time_elem else None

            obs_id = "li-job-" + hashlib.sha256(f"{org_number}|{title}|{employer_name}".encode()).hexdigest()[:24]
            observations.append({
                "id": obs_id,
                "organisation_number": str(org_number),
                "platform": "linkedin",
                "signal_type": "job_posting",
                "source_url": job_url,
                "retrieved_at": retrieved_at,
                "published_at": posted_at,
                "exact_entity": True,
                "content_sha256": content_digest,
                "identity_proof": [
                    {
                        "type": "linkedin_company_exact_match",
                        "employer_name": employer_name,
                        "query": company_name,
                    }
                ],
                "job_title": title,
                "employer_name": employer_name,
                "evidence_span": f"Active LinkedIn job posting '{title}' published by {employer_name} in Norway",
                "source_class": "commercial_social_platform",
                "rights_status": "approved",
                "acquisition_mode": "permitted_public_page",
                "strategy": "linkedin_jobs_guest_search",
            })
            if len(observations) >= max_results:
                break

        return observations

    except Exception:
        elapsed_ms = int((time.perf_counter() - start_time) * 1000)
        if budget:
            budget.record_request(bytes_received=bytes_received, elapsed_ms=elapsed_ms, cost_usd=0.0)
        return []
