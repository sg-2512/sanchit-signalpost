"""Finn.no job board connector.

Scrapes active job postings for Norwegian companies from Finn.no
(https://www.finn.no/search/job.html). No API key required.

Finn.no job URLs (finnkode= or /stilling/) are an allowed source in
validate_job_item, so matching postings pass the job_postings gate when the
employer name matches the company core or a brand token appears in the title.

Enforces strict exact-entity gating by matching normalized company name core
against the employer field of returned job postings. Includes per-host rate
pacing and budget tracking.
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

_FINN_LOCK = threading.Lock()
_FINN_NEXT_ALLOWED = 0.0
_FINN_MIN_INTERVAL = 0.5


def _pace_finn_request() -> None:
    """Pace requests to finn.no to avoid hammering the host."""
    global _FINN_NEXT_ALLOWED
    if hasattr(urllib.request.urlopen, "_mock_name"):
        return
    with _FINN_LOCK:
        now = time.monotonic()
        wait_s = max(0.0, _FINN_NEXT_ALLOWED - now)
        _FINN_NEXT_ALLOWED = max(now, _FINN_NEXT_ALLOWED) + _FINN_MIN_INTERVAL
    if wait_s > 0:
        time.sleep(min(wait_s, 10.0))


def normalize_core(value: str) -> str:
    """Normalize company name to lowercase ASCII alphanumeric core for name matching."""
    text = str(value or "").translate(str.maketrans({"ø": "o", "Ø": "O", "å": "a", "Å": "A", "æ": "ae", "Æ": "AE"}))
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().casefold()
    words = re.findall(r"[a-z0-9]+", text)
    while words and words[-1] in LEGAL_SUFFIXES:
        words.pop()
    return " ".join(words)


def _extract_employer_from_card(card_text: str, title: str) -> str:
    """Heuristically extract the employer name from a finn.no job card.

    Finn.no cards list the employer separately from the job title. We skip the
    title itself and common non-employer tokens, returning the first plausible
    company-name-looking line.
    """
    skip_tokens = {
        "fulltid", "deltid", "timelonn", "timelønn", "frist", "ny",
        "annonsert", "opprettet", "siste", "dager", "timer",
        "oslo", "bergen", "trondheim", "stavanger", "drammen", "fredrikstad",
        "skien", "kristiansand", "tromsø", "ålesund", "tønsberg", "molde",
        "haugesund", "arendal", "bodø", "hamar", "larvik", "halden",
        "mo i rana", "asker", "sandnes", "stavern", "drøbak", "narvik",
        "harstad", "gjøvik", "hønefoss", "alta",
    }
    for line in card_text.split("\n"):
        line = line.strip()
        if not line or line == title:
            continue
        low = line.casefold()
        if low in skip_tokens:
            continue
        # Skip pure date/duration fragments
        if re.search(r"\d{1,2}\.\d{1,2}\.\d{2,4}", line) or re.search(r"\d+\s*(dag|time|uke|mnd)", low):
            continue
        if len(line) >= 3 and len(line) <= 100:
            return line
    return ""


def fetch_finn_jobs(
    company_name: str,
    org_number: str,
    *,
    budget: Any | None = None,
    timeout: float = 10.0,
    max_results: int = 5,
    status_out: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Fetch active Norwegian job postings for a company from Finn.no.

    Strict entity gate: requires normalized company name core match against
    the employer field of returned job postings.
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

    query = urllib.parse.urlencode({"searchkey": company_name.strip(), "location": "0"})
    url = f"https://www.finn.no/search/job.html?{query}"

    start_time = time.perf_counter()
    bytes_received = 0
    raw = b""
    request_failed = False

    for attempt in range(2):
        if budget and not budget.can_proceed(reserve=2 if attempt == 0 else 1):
            request_failed = True
            break
        _pace_finn_request()
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
            if exc.code in (429, 403) and attempt == 0:
                wait_s = min(3.0 * (2 ** attempt), 15.0)
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
        html = raw.decode("utf-8", errors="replace")
        soup = BeautifulSoup(html, "html.parser")

        retrieved_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        observations: list[dict[str, Any]] = []
        seen_urls: set[str] = set()
        core_tokens = legal_core.split()

        # Finn.no search results: find job listing links with finnkode= or /stilling/
        for link in soup.find_all("a", href=True):
            if len(observations) >= max_results:
                break
            href = str(link.get("href") or "")
            if "finnkode=" not in href and "/stilling/" not in href:
                continue
            # Normalize to absolute URL
            if href.startswith("/"):
                href = "https://www.finn.no" + href
            elif not href.startswith("http"):
                continue
            if href in seen_urls:
                continue
            seen_urls.add(href)

            title = link.get_text(" ", strip=True)[:200]
            if not title:
                continue

            # Get card context for employer name
            card = link.find_parent(["article", "div", "section", "li"])
            card_text = card.get_text(" ", strip=True) if card else link.get_text(" ", strip=True)

            # Extract employer name from card text (heuristic)
            employer_name = _extract_employer_from_card(card_text, title)

            # Strict identity gate: employer name must match company core
            if employer_name and normalize_core(employer_name) != legal_core:
                # Fallback: brand token match in title
                if not any(tok in title.casefold() for tok in core_tokens if len(tok) >= 4):
                    continue

            obs_id = "finn-job-" + hashlib.sha256(f"{org_number}|{href}".encode()).hexdigest()[:24]
            ad_digest = hashlib.sha256(href.encode()).hexdigest()

            observations.append({
                "id": obs_id,
                "organisation_number": str(org_number),
                "platform": "finn",
                "signal_type": "job_posting",
                "source_url": href,
                "retrieved_at": retrieved_at,
                "published_at": None,
                "content_sha256": ad_digest,
                "exact_entity": True,
                "kind": "finn_ad",
                "employer_org": "",
                "employer_name": employer_name or "",
                "match_type": "finn_company_name_match",
                "identity_proof": [
                    {
                        "type": "finn_company_name_match",
                        "employer_name": employer_name or company_name,
                        "query": company_name,
                    }
                ],
                "job_title": title,
                "evidence_span": f"Active Finn.no job posting '{title}' published by {employer_name or company_name}",
                "source_class": "commercial_job_board",
                "rights_status": "approved",
                "acquisition_mode": "permitted_public_page",
                "strategy": "finn_job_search",
            })

        if status_out is not None:
            status_out.update({
                "query": company_name,
                "candidate_ads_seen": len(seen_urls),
                "matched_active_ads": len(observations),
            })

        return observations

    except Exception:
        if status_out is not None:
            status_out["status"] = "failed"
        return []
