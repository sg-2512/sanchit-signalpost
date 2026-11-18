"""Opt-in, metered Google-result candidate discovery using Serper.dev.

Search results are *discovery hints*, never legal-entity evidence. The caller must
fetch each proposed site and pass both the legal-operator and entity gates.
No SERP text is persisted in company output, avoiding copyright/storage issues.
"""
from __future__ import annotations

import json
import os
import threading
import urllib.error
import urllib.request
from typing import Any

from ..discovery import score_search_candidate

ENDPOINT = "https://google.serper.dev/search"
_LOCK = threading.Lock()
_REQUESTS = 0
_DISABLED = False


def reset_serper_search() -> None:
    global _REQUESTS, _DISABLED
    with _LOCK:
        _REQUESTS, _DISABLED = 0, False


def serper_available() -> bool:
    with _LOCK:
        return bool(os.environ.get("SERPER_API_KEY", "").strip()) and not _DISABLED


def discover_company_websites_serper(
    profile: dict[str, Any], *, budget: Any, max_candidates: int = 3,
    timeout: float = 8.0,
) -> list[dict[str, str]]:
    """Return same-host proof URLs and candidate origins, never publishable facts."""
    global _REQUESTS, _DISABLED
    key = os.environ.get("SERPER_API_KEY", "").strip()
    if not key:
        return []
    try:
        cap = max(0, min(1000, int(os.environ.get("SIGNALPOST_SERPER_MAX_SEARCHES", "115"))))
    except ValueError:
        cap = 115
    # Estimated per-query spend (prepaid/free account actuals differ). The
    # application budget counts a conservative charged request.
    try:
        cost = max(0.0, min(0.01, float(os.environ.get("SIGNALPOST_SERPER_ESTIMATED_USD", "0.001"))))
    except ValueError:
        cost = 0.001
    with _LOCK:
        if _DISABLED or _REQUESTS >= cap:
            return []
        if budget.remaining_cost() < cost or not budget.try_start_request(reserve=6):
            return []
        _REQUESTS += 1

    org = str(profile.get("organisation_number") or "")
    # A nine-digit organisation-number query often returns only legal
    # aggregators; exact-name search exposes first-party websites and their
    # legal/about pages. Results remain *untrusted crawl hints*.
    name = " ".join(str(profile.get("name") or "").split())
    municipality = str(profile.get("municipality") or "").strip()
    query = f'"{name}" {municipality} nettsted'.strip()
    if not name:
        return []
    body = json.dumps({"q": query, "gl": "no", "hl": "no", "num": 10}).encode("utf-8")
    request = urllib.request.Request(
        ENDPOINT, data=body, method="POST",
        headers={
            "X-API-KEY": key, "Content-Type": "application/json",
            "Accept": "application/json", "User-Agent": "SignalPostResearch/1.0",
        },
    )
    raw = b""
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read(250_001)
        if len(raw) > 250_000:
            return []
    except urllib.error.HTTPError as exc:
        if exc.code in {401, 402, 403, 429}:
            with _LOCK:
                _DISABLED = True
        return []
    except (OSError, ValueError):
        return []
    finally:
        budget.record_request(count=0, cost_usd=cost, bytes_received=len(raw))
    try:
        payload = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        return []
    from urllib.parse import urlsplit, urlunsplit
    candidates: list[tuple[float, str, str]] = []
    for rank, result in enumerate((payload.get("organic") or [])[:10], start=1):
        if not isinstance(result, dict):
            continue
        item = {
            "url": str(result.get("link") or ""),
            "title": str(result.get("title") or ""),
            "snippet": str(result.get("snippet") or ""),
            "rank": rank,
            "provider": "serper",
        }
        assessment = score_search_candidate(profile, item)
        # Search pages without an org number can still be worth a *crawl* if
        # the result title contains every distinctive legal-name token. The
        # stricter operator identity gate remains the sole publication gate.
        from ..discovery import _tokens, GENERIC_NAME_TOKENS
        legal_tokens = {t for t in _tokens(name) if t not in GENERIC_NAME_TOKENS}
        title_tokens = set(_tokens(item["title"]))
        name_title_match = bool(legal_tokens) and legal_tokens.issubset(title_tokens)
        if (not assessment.get("publishable_candidate")
                and not (name_title_match and float(assessment.get("score") or 0) >= 0.4)):
            continue
        url = assessment.get("url") or ""
        parts = urlsplit(url)
        if parts.scheme not in {"https", "http"} or not parts.hostname:
            continue
        origin = urlunsplit((parts.scheme, parts.netloc, "/", "", ""))
        candidates.append((float(assessment.get("score") or 0), url, origin))
    candidates.sort(key=lambda item: -item[0])
    seen_hosts: set[str] = set()
    output: list[dict[str, str]] = []
    for score, proof_url, origin in candidates:
        host = urlsplit(proof_url).hostname or ""
        host_key = host.removeprefix("www.").lower()
        if host_key in seen_hosts:
            continue
        seen_hosts.add(host_key)
        output.append({"origin": origin, "proof_url": proof_url, "source": "serper_crawl_candidate"})
        if len(output) >= max_candidates:
            break
    return output
