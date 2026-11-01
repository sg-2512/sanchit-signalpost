"""Brave Search API connector for SignalPost live agent.

Discovers candidate company websites when not listed in BRREG registry snapshot.
Strictly adheres to competition policy:
- Search APIs are used for candidate generation ONLY.
- Raw search responses are transient and never published as claim evidence.
- Candidates must pass the exact-entity identity verification gate (identity.py)
  before any claim or website is published.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from ..discovery import build_company_search_query, choose_search_candidate, parse_brave_web_results

BRAVE_ENDPOINT = "https://api.search.brave.com/res/v1/web/search"
BRAVE_API_KEY_ENV = "BRAVE_API_KEY"
BRAVE_SEARCH_API_KEY_ENV = "BRAVE_SEARCH_API_KEY"


def is_available() -> bool:
    """Return True if a Brave or generic Search API key is present in environment."""
    return bool(os.environ.get(BRAVE_API_KEY_ENV) or os.environ.get(BRAVE_SEARCH_API_KEY_ENV) or os.environ.get("SEARCH_API_KEY"))


def _get_api_key() -> str:
    key = os.environ.get(BRAVE_API_KEY_ENV) or os.environ.get(BRAVE_SEARCH_API_KEY_ENV) or os.environ.get("SEARCH_API_KEY") or ""
    return key.strip()


def discover_company_website(
    profile: dict[str, Any],
    *,
    budget: Any | None = None,
    timeout: float = 12.0,
    count: int = 5,
) -> str | None:
    """Query Brave Search to discover an official candidate website.

    Returns the candidate website URL if one passes domain and name filtering,
    or None if no candidate passes or budget is exhausted.
    """
    if not is_available():
        return None

    if budget and not budget.can_proceed():
        return None

    api_key = _get_api_key()
    if not api_key:
        return None

    try:
        primary_query = build_company_search_query(profile)
    except ValueError:
        return None

    queries = [primary_query]
    # Brand query fallback for non-.no international brand domains
    c_name = str(profile.get("name") or "")
    clean_brand = re.sub(r"\b(AS|ASA|ANS|DA|ENK|HOLDING|INVEST|GROUP|GRUPPEN)\b", "", c_name, flags=re.I).strip()
    if clean_brand and len(clean_brand) >= 3:
        muni = str(profile.get("municipality") or "Norge").strip()
        brand_q = f'"{clean_brand}" {muni} website'
        if brand_q not in queries:
            queries.append(brand_q)

    for query in queries:
        if budget and not budget.can_proceed():
            break
        url = BRAVE_ENDPOINT + "?" + urllib.parse.urlencode({
            "q": query,
            "count": count,
            "country": "no",
            "search_lang": "nb",
            "safesearch": "moderate",
            "spellcheck": "0",
        })

        request = urllib.request.Request(url, headers={
            "Accept": "application/json",
            "Accept-Encoding": "identity",
            "Cache-Control": "no-cache",
            "User-Agent": "SignalPostAgent/0.1 (research; +https://builderr.ai/signalpost)",
            "X-Subscription-Token": api_key,
        })

        started = time.monotonic()
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read()
                elapsed_ms = (time.monotonic() - started) * 1000

            # Brave Search API costs ~$0.005 per request ($5 per 1,000 queries)
            if budget:
                budget.record_request(
                    bytes_received=len(raw),
                    latency_ms=elapsed_ms,
                    cost_usd=0.005,
                )

            payload = json.loads(raw)
            results = parse_brave_web_results(payload, query=query)
            decision = choose_search_candidate(profile, results)
            selected = decision.get("selected")
            if selected and selected.get("url"):
                return str(selected["url"])
        except Exception:
            if budget:
                budget.record_request(cost_usd=0.005)
            continue

    return None


def fetch_brave_jobs(
    company_name: str,
    *,
    org_number: str = "",
    budget: Any | None = None,
    timeout: float = 12.0,
    count: int = 8,
    max_results: int = 8,
) -> list[dict[str, Any]]:
    """Discover job-posting candidates for a company via Brave Search.

    Runs two Norwegian job-discovery queries against the Brave Search API:

      1. "{company name}" ledige stillinger
      2. site:arbeidsplassen.nav.no "{company name}"

    Returns a list of job-posting observation dicts in the same shape as the
    nav_jobs/finn_jobs observations (id, source_url, job_title, employer_name,
    evidence_span, content_sha256, ...), each flagged as an unverified
    candidate:

        exact_entity = False
        rights_status = "pending_verification"

    Rationale (competition policy, see module docstring): Brave Search is a
    candidate-generation source only. Raw search responses are transient and
    are never published as claim evidence. A candidate job posting must pass
    the exact-entity identity verification gate (identity.py) before any
    hiring claim is published -- e.g. by fetching an arbeidsplassen.nav.no ad
    page and matching the employer organisation number or name.

    Returns an empty list when no API key is configured, the budget is
    exhausted, or all queries fail.
    """
    if not company_name or not str(company_name).strip():
        return []
    if not is_available():
        return []
    if budget and not budget.can_proceed():
        return []

    api_key = _get_api_key()
    if not api_key:
        return []

    name = str(company_name).strip()
    queries = [
        f'"{name}" ledige stillinger',
        f'site:arbeidsplassen.nav.no "{name}"',
    ]

    observations: list[dict[str, Any]] = []
    seen_urls: set[str] = set()
    retrieved_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    for query in queries:
        if budget and not budget.can_proceed():
            break
        url = BRAVE_ENDPOINT + "?" + urllib.parse.urlencode({
            "q": query,
            "count": count,
            "country": "no",
            "search_lang": "nb",
            "safesearch": "moderate",
            "spellcheck": "0",
        })

        request = urllib.request.Request(url, headers={
            "Accept": "application/json",
            "Accept-Encoding": "identity",
            "Cache-Control": "no-cache",
            "User-Agent": "SignalPostAgent/0.1 (research; +https://builderr.ai/signalpost)",
            "X-Subscription-Token": api_key,
        })

        started = time.monotonic()
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read()
                elapsed_ms = (time.monotonic() - started) * 1000

            # Brave Search API costs ~$0.005 per request ($5 per 1,000 queries)
            if budget:
                budget.record_request(
                    bytes_received=len(raw),
                    latency_ms=elapsed_ms,
                    cost_usd=0.005,
                )

            payload = json.loads(raw)
        except Exception:
            if budget:
                budget.record_request(cost_usd=0.005)
            continue

        for result in parse_brave_web_results(payload, query=query):
            if len(observations) >= max_results:
                break
            result_url = str(result.get("url") or "")
            if not result_url or result_url in seen_urls:
                continue
            seen_urls.add(result_url)

            title = str(result.get("title") or "").strip()
            snippet = str(result.get("snippet") or "")
            ad_digest = hashlib.sha256(
                f"{result_url}|{title}|{snippet}".encode("utf-8")
            ).hexdigest()

            obs_id = "brave-job-" + hashlib.sha256(
                f"{name}|{result_url}".encode("utf-8")
            ).hexdigest()[:24]

            observations.append({
                "id": obs_id,
                "organisation_number": str(org_number),
                "platform": "brave_search",
                "signal_type": "job_posting",
                "source_url": result_url,
                "retrieved_at": retrieved_at,
                "published_at": None,
                "content_sha256": ad_digest,
                "exact_entity": False,
                "kind": "brave_search_candidate",
                "employer_org": str(org_number),
                "employer_name": name,
                "match_type": "brave_search_candidate",
                "query": query,
                "snippet": snippet,
                "identity_proof": [
                    {
                        "type": "brave_search_candidate",
                        "employer_name": name,
                        "query": query,
                    }
                ],
                "job_title": title or "Ledig stilling",
                "evidence_span": (
                    f"Brave Search candidate job posting '{title}' for {name} "
                    f"(query: {query}) -- requires exact-entity verification before publication"
                ),
                "source_class": "third_party_search_index",
                "rights_status": "pending_verification",
                "acquisition_mode": "search_api",
                "strategy": "brave_search_job_discovery",
            })
        if len(observations) >= max_results:
            break

    return observations
