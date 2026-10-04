"""NAV Arbeidsplassen public job vacancy connector.

Free, open official Norwegian government source (no credentials needed).
Queries https://arbeidsplassen.nav.no/stillinger/api/search with exact
normalized company name matching to eliminate wrong-company job postings.
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


def fetch_nav_jobs(
    company_name: str,
    org_number: str,
    *,
    budget: Any | None = None,
    timeout: float = 6.0,
    max_results: int = 5,
) -> list[dict[str, Any]]:
    """Fetch verified active job postings for a company from NAV Arbeidsplassen.

    Strict entity gate: requires exact normalized company core match.
    """
    if not company_name:
        return []

    legal_core = normalize_core(company_name)
    if not legal_core or len(legal_core) < 3:
        return []

    if budget and not budget.can_proceed():
        return []

    # Query by clean core brand first (e.g. 'DNB' or 'Equinor'), then fallback to full name
    query_str = legal_core if len(legal_core) >= 3 else company_name.strip()
    clean_query = urllib.parse.quote(query_str)
    url = f"https://arbeidsplassen.nav.no/stillinger/api/search?q={clean_query}&size=10"

    start_time = time.perf_counter()
    bytes_received = 0
    try:
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "application/json",
            },
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read(500_000)
            bytes_received = len(raw)
            data = json.loads(raw.decode("utf-8", errors="replace"))

        elapsed_ms = int((time.perf_counter() - start_time) * 1000)
        if budget:
            budget.record_request(bytes_received=bytes_received, elapsed_ms=elapsed_ms, cost_usd=0.0)

        content_digest = hashlib.sha256(raw).hexdigest()
        hits = data.get("hits", {}).get("hits", [])
        retrieved_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        observations: list[dict[str, Any]] = []

        for hit in hits:
            src = hit.get("_source", {})
            employer_name = src.get("businessName") or (src.get("employer") or {}).get("name") or ""
            employer_org = str((src.get("employer") or {}).get("orgnr") or "")
            employer_core = normalize_core(employer_name)

            # Strict company identity match
            matched = False
            match_type = "nav_company_core_match"
            if employer_org and employer_org == str(org_number):
                matched = True
                match_type = "nav_employer_orgnr_match"
            elif employer_core == legal_core:
                matched = True

            if not matched:
                continue

            uuid = src.get("uuid") or src.get("id") or ""
            title = str(src.get("title") or "Ledig stilling").strip()
            published = src.get("published")
            expires = src.get("expires")
            job_url = f"https://arbeidsplassen.nav.no/stillinger/stilling/{uuid}" if uuid else "https://arbeidsplassen.nav.no/stillinger"

            obs_id = "nav-job-" + hashlib.sha256(f"{org_number}|{uuid or title}".encode()).hexdigest()[:24]
            observations.append({
                "id": obs_id,
                "organisation_number": str(org_number),
                "platform": "job_board",
                "signal_type": "job_posting",
                "source_url": job_url,
                "retrieved_at": retrieved_at,
                "published_at": published,
                "expires_at": expires,
                "exact_entity": True,
                "content_sha256": content_digest,
                "identity_proof": [
                    {
                        "type": match_type,
                        "employer_name": employer_name,
                        "employer_org": employer_org,
                        "query": company_name,
                    }
                ],
                "job_title": title,
                "employer_name": employer_name,
                "evidence_span": f"Active job posting '{title}' published by {employer_name} on NAV Arbeidsplassen",
                "source_class": "official_public_employment_registry",
                "rights_status": "approved",
                "acquisition_mode": "official_api",
                "strategy": "nav_arbeidsplassen_search",
            })
            if len(observations) >= max_results:
                break

        return observations

    except Exception:
        elapsed_ms = int((time.perf_counter() - start_time) * 1000)
        if budget:
            budget.record_request(bytes_received=bytes_received, elapsed_ms=elapsed_ms, cost_usd=0.0)
        return []
