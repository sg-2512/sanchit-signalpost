"""LinkedIn Company Profile connector via open guest typeahead API.

Completely free ($0.00 spend, no credentials needed).
Discovers verified LinkedIn corporate pages using exact normalized legal core gating
and anti-impersonation security filtering.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
import unicodedata
import urllib.parse
import urllib.request
from typing import Any

from ..social_security import verify_social_channel_security

LEGAL_SUFFIXES = {
    "as", "asa", "ba", "da", "enk", "iks", "nuf", "sa", "sam",
    "sti", "stiftelsen", "holding", "eiendom",
}

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"


def is_available() -> bool:
    """Always available — zero API key required."""
    return True


def normalize_company_core(value: str) -> str:
    """Normalize company name to lowercase ASCII alphanumeric core, stripping legal suffixes."""
    text = str(value or "").translate(str.maketrans({"ø": "o", "Ø": "O", "å": "a", "Å": "A", "æ": "ae", "Æ": "AE"}))
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().casefold()
    words = re.findall(r"[a-z0-9]+", text)
    while words and words[-1] in LEGAL_SUFFIXES:
        words.pop()
    return " ".join(words)


def discover_linkedin_company(
    company_name: str,
    org_number: str,
    *,
    website_domain: str | None = None,
    budget: Any | None = None,
    timeout: float = 6.0,
) -> dict[str, Any] | None:
    """Discover verified LinkedIn company profile via LinkedIn guest typeahead.

    Strict entity gate: requires exact normalized legal core match and social security pass.
    """
    if not company_name:
        return None

    legal_core = normalize_company_core(company_name)
    if not legal_core or len(legal_core) < 3:
        return None

    if budget and not budget.can_proceed():
        return None

    clean_query = urllib.parse.quote(company_name.strip())
    url = f"https://www.linkedin.com/jobs-guest/api/typeaheadHits?typeaheadType=COMPANY&query={clean_query}"

    start_time = time.perf_counter()
    bytes_received = 0
    try:
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "application/json",
                "Accept-Language": "en-US,en;q=0.9,no;q=0.8",
            },
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read(500_000)
            bytes_received = len(raw)
            data = json.loads(raw.decode("utf-8", errors="replace"))

        elapsed_ms = int((time.perf_counter() - start_time) * 1000)
        if budget:
            budget.record_request(bytes_received=bytes_received, elapsed_ms=elapsed_ms, cost_usd=0.0)

        if not isinstance(data, list):
            return None

        for item in data:
            if item.get("type") != "COMPANY" or not item.get("id"):
                continue
            display_name = str(item.get("displayName") or "")
            candidate_core = normalize_company_core(display_name)

            if candidate_core == legal_core:
                company_id = str(item["id"])
                slug = re.sub(r"[^a-z0-9\-]+", "-", display_name.lower()).strip("-")
                canonical_url = f"https://www.linkedin.com/company/{slug}" if slug else f"https://www.linkedin.com/company/{company_id}"

                # Security & authenticity screening
                sec = verify_social_channel_security(
                    platform="linkedin",
                    channel_or_profile_name=display_name,
                    target_url=canonical_url,
                    company_name=company_name,
                    website_domain=website_domain,
                )
                if not sec["is_safe"]:
                    continue

                return {
                    "id": "linkedin-" + hashlib.sha256(f"{org_number}|{company_id}".encode()).hexdigest()[:24],
                    "organisation_number": str(org_number),
                    "platform": "linkedin",
                    "signal_type": "official_profile",
                    "source_url": canonical_url,
                    "linkedin_company_id": company_id,
                    "display_name": display_name,
                    "exact_entity": True,
                    "match_type": "exact_legal_core",
                    "source": "linkedin_guest_api",
                    "security_assessment": sec,
                }

    except Exception:
        elapsed_ms = int((time.perf_counter() - start_time) * 1000)
        if budget:
            budget.record_request(bytes_received=bytes_received, elapsed_ms=elapsed_ms, cost_usd=0.0)

    return None
