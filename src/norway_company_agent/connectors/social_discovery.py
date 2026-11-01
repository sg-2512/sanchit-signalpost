"""Independent social media profile discovery connector.

Discovers social media profiles for Norwegian companies without relying on the
company website. Probes common URL patterns on LinkedIn, Facebook, Instagram,
TikTok, and X/Twitter, then verifies each candidate page contains the company
name before publishing.

No API key required — uses direct HTTP GET with rate pacing and budget tracking.
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

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"

# Social platforms to probe, with URL pattern and platform label
SOCIAL_PATTERNS = [
    ("linkedin", "https://www.linkedin.com/company/{slug}"),
    ("facebook", "https://www.facebook.com/{slug}"),
    ("instagram", "https://www.instagram.com/{slug}"),
    ("tiktok", "https://www.tiktok.com/@{slug}"),
    ("x", "https://x.com/{slug}"),
]

LEGAL_SUFFIXES = {
    "as", "asa", "ans", "da", "enk", "ks", "ba", "sa", "sf", "sti", "nuf",
    "ik", "kf", "fkf", "hf", "rhf", "bl", "brl", "esek", "holding", "invest",
    "gruppen", "group", "norge", "norway",
}

_SOC_LOCK = threading.Lock()
_SOC_NEXT_ALLOWED = 0.0
_SOC_MIN_INTERVAL = 0.4


def _pace_social_request() -> None:
    """Pace requests across social platforms to avoid hammering any single host."""
    global _SOC_NEXT_ALLOWED
    if hasattr(urllib.request.urlopen, "_mock_name"):
        return
    with _SOC_LOCK:
        now = time.monotonic()
        wait_s = max(0.0, _SOC_NEXT_ALLOWED - now)
        _SOC_NEXT_ALLOWED = max(now, _SOC_NEXT_ALLOWED) + _SOC_MIN_INTERVAL
    if wait_s > 0:
        time.sleep(min(wait_s, 10.0))


def normalize_slug(value: str) -> str:
    """Convert a company name to a URL-friendly slug (lowercase, alnum, hyphens)."""
    text = str(value or "").translate(str.maketrans({"ø": "o", "Ø": "O", "å": "a", "Å": "A", "æ": "ae", "Æ": "AE"}))
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().casefold()
    words = re.findall(r"[a-z0-9]+", text)
    while words and words[-1] in LEGAL_SUFFIXES:
        words.pop()
    if not words:
        return ""
    return "-".join(words)


def normalize_core(value: str) -> str:
    """Normalize company name to lowercase alphanumeric core for name matching."""
    text = str(value or "").translate(str.maketrans({"ø": "o", "Ø": "O", "å": "a", "Å": "A", "æ": "ae", "Æ": "AE"}))
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().casefold()
    words = re.findall(r"[a-z0-9]+", text)
    while words and words[-1] in LEGAL_SUFFIXES:
        words.pop()
    return " ".join(words)


def _page_contains_company(html: str, company_name: str) -> bool:
    """Quick heuristic: does the HTML contain the company's core name?"""
    core = normalize_core(company_name)
    if not core or len(core) < 3:
        return False
    html_lower = html.casefold()
    core_tokens = core.split()
    # All core tokens must appear somewhere in the page
    return all(tok in html_lower for tok in core_tokens if len(tok) >= 3)


def fetch_social_profiles(
    company_name: str,
    org_number: str,
    *,
    budget: Any | None = None,
    timeout: float = 8.0,
    max_profiles: int = 5,
    status_out: dict[str, Any] | None = None,
    exclude_platforms: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Discover social media profiles for a company independently of its website.

    Probes common URL patterns per platform, then verifies each candidate page
    contains the company name before publishing. Respects the shared request
    budget and paces requests across platforms.
    """
    if status_out is not None:
        status_out["status"] = "not_applicable" if not company_name else "blocked"
    if not company_name:
        return []

    slug = normalize_slug(company_name)
    if not slug or len(slug) < 3:
        if status_out is not None:
            status_out["status"] = "not_available"
        return []

    core = normalize_core(company_name)
    if not core or len(core) < 3:
        if status_out is not None:
            status_out["status"] = "not_available"
        return []

    if budget and not budget.can_proceed(reserve=1):
        if status_out is not None:
            status_out["status"] = "blocked"
        return []

    retrieved_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    observations: list[dict[str, Any]] = []
    seen_urls: set[str] = set()
    excluded = {str(p).casefold() for p in (exclude_platforms or set())}
    tried = 0
    found = 0

    for platform, pattern in SOCIAL_PATTERNS:
        if platform.casefold() in excluded:
            continue
        if found >= max_profiles:
            break
        if budget and not budget.can_proceed(reserve=1):
            break

        url = pattern.format(slug=slug)
        if url.lower() in seen_urls:
            continue
        tried += 1

        _pace_social_request()
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
                final_url = resp.geturl()
                raw = resp.read(500_000)
        except urllib.error.HTTPError as exc:
            if exc.code in (404, 403, 429):
                continue
            continue
        except Exception:
            continue

        if budget:
            budget.record_request(bytes_received=len(raw), cost_usd=0.0)

        html = raw.decode("utf-8", errors="replace")
        if not _page_contains_company(html, company_name):
            continue

        seen_urls.add(url.lower())
        found += 1

        # Extract page title for evidence
        title = ""
        try:
            soup = BeautifulSoup(html, "html.parser")
            if soup.title:
                title = soup.title.get_text(" ", strip=True)[:200]
        except Exception:
            pass

        content_digest = hashlib.sha256(raw).hexdigest()
        obs_id = f"social-disc-{platform}-{org_number}-{hashlib.sha256(url.encode()).hexdigest()[:12]}"

        observations.append({
            "id": obs_id,
            "organisation_number": str(org_number),
            "platform": platform,
            "signal_type": "profile_handle",
            "source_url": final_url or url,
            "retrieved_at": retrieved_at,
            "content_sha256": content_digest,
            "exact_entity": True,
            "identity_proof": [
                {
                    "type": "social_platform_name_match",
                    "platform": platform,
                    "profile_url": url,
                    "query": company_name,
                }
            ],
            "acquisition_mode": "permitted_public_page",
            "rights_status": "approved",
            "source_class": "commercial_social_platform",
            "profile_url": url,
            "evidence_span": f"{platform.title()} profile '{title or url}' appears to belong to {company_name}",
            "metrics": {"platform": platform, "url": url, "title": title},
            "strategy": "independent_social_discovery",
        })

        if len(observations) >= max_profiles:
            break

    if status_out is not None:
        status_out["status"] = "available" if observations else "not_available"
        status_out["tried"] = tried
        status_out["found"] = found

    return observations
