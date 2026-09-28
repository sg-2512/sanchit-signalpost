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
    """Return True if a Brave Search API key is present in environment."""
    return bool(os.environ.get(BRAVE_API_KEY_ENV) or os.environ.get(BRAVE_SEARCH_API_KEY_ENV))


def _get_api_key() -> str:
    key = os.environ.get(BRAVE_API_KEY_ENV) or os.environ.get(BRAVE_SEARCH_API_KEY_ENV) or ""
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
        query = build_company_search_query(profile)
    except ValueError:
        return None

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
        return None

    except Exception:
        if budget:
            budget.record_request(cost_usd=0.005)
        return None
