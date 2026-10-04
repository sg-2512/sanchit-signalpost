from __future__ import annotations

import re
import unicodedata
import urllib.parse
import urllib.request
from typing import Any

from .website import normalize_homepage


BLOCKED_DISCOVERY_HOSTS = {
    "proff.no", "purehelp.no", "1881.no", "gulesider.no", "firmalisten.no", "companywall.no",
    "firmadatabasen.no", "sokfirma.no", "yra.no", "northdata.com", "nor47business.com",
    "linkedin.com", "facebook.com", "instagram.com", "x.com", "twitter.com", "youtube.com", "tiktok.com",
}
GENERIC_NAME_TOKENS = {"as", "asa", "ans", "da", "enk", "sa", "nuf", "company", "norge", "norway", "gruppen", "group"}


def build_company_search_query(profile: dict[str, Any]) -> str:
    name = " ".join(str(profile.get("name") or "").split())
    org = re.sub(r"\D", "", str(profile.get("organisation_number") or ""))
    municipality = " ".join(str(profile.get("municipality") or "").split())
    if not name or not org:
        raise ValueError("Company discovery requires a legal name and organisation number")
    location = f" {municipality}" if municipality else ""
    return f'"{name}" {org}{location}'


def parse_brave_web_results(payload: dict[str, Any], *, query: str) -> list[dict[str, Any]]:
    results = (payload.get("web") or {}).get("results") or []
    parsed = []
    for rank, result in enumerate(results, start=1):
        if not isinstance(result, dict) or not result.get("url"):
            continue
        parsed.append({
            "url": result.get("url"),
            "title": result.get("title") or "",
            "snippet": result.get("description") or "",
            "rank": rank,
            "provider": "brave_search_api",
            "query": query,
        })
    return parsed


def _tokens(value: Any) -> list[str]:
    text = str(value or "").translate(str.maketrans({"ø": "o", "å": "a", "æ": "ae", "Ø": "O", "Å": "A", "Æ": "AE"}))
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().casefold()
    return [token for token in re.findall(r"[a-z0-9]+", text) if len(token) > 1]


def score_search_candidate(profile: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
    normalized = normalize_homepage(result.get("url"))
    if not normalized:
        return {"status": "rejected", "score": 0.0, "publishable_candidate": False, "reasons": ["invalid HTTP(S) candidate URL"]}
    host = (urllib.parse.urlparse(normalized).hostname or "").casefold().removeprefix("www.")
    if any(host == blocked or host.endswith("." + blocked) for blocked in BLOCKED_DISCOVERY_HOSTS):
        return {"status": "rejected", "score": 0.0, "publishable_candidate": False, "url": normalized, "host": host, "reasons": ["directory, aggregator, or social host is not a company website candidate"]}

    name_tokens = [token for token in _tokens(profile.get("name")) if token not in GENERIC_NAME_TOKENS]
    title_tokens = _tokens(result.get("title"))
    snippet_tokens = _tokens(result.get("snippet"))
    evidence_tokens = set(title_tokens + snippet_tokens + _tokens(host))
    host_compact = "".join(_tokens(host))
    name_compact = "".join(name_tokens)
    org = re.sub(r"\D", "", str(profile.get("organisation_number") or ""))
    evidence_digits = re.sub(r"\D", "", f"{result.get('title', '')} {result.get('snippet', '')}")
    municipality_tokens = set(_tokens(profile.get("municipality")))

    org_match = bool(org and org in evidence_digits)
    all_name_tokens = bool(name_tokens and set(name_tokens).issubset(evidence_tokens))
    all_name_tokens_in_title = bool(name_tokens and set(name_tokens).issubset(set(title_tokens)))
    name_in_host = bool(name_compact and name_compact in host_compact)
    municipality_match = bool(municipality_tokens and municipality_tokens <= set(snippet_tokens))
    score = 0.0
    reasons = []
    if org_match:
        score += 0.75
        reasons.append("exact organisation number appears in result evidence")
    if all_name_tokens_in_title:
        score += 0.45
        reasons.append("all distinctive legal-name tokens appear in the result title")
    elif all_name_tokens:
        score += 0.25
        reasons.append("all distinctive legal-name tokens appear across result evidence")
    if name_in_host:
        score += 0.3
        reasons.append("normalized legal name appears in candidate hostname")
    if municipality_match:
        score += 0.1
        reasons.append("registry municipality appears in result snippet")
    score = min(score, 1.0)
    # Registry/directory pages routinely reproduce both the legal name and org
    # number. A candidate must therefore also have the distinctive company name
    # in its hostname before it is worth crawling as a company-owned website.
    publishable_candidate = score >= 0.75 and name_in_host and (org_match or all_name_tokens_in_title)
    return {
        "status": "accepted_for_crawl" if publishable_candidate else "review" if score >= 0.6 else "rejected",
        "score": score,
        "publishable_candidate": publishable_candidate,
        "url": normalized,
        "host": host,
        "rank": result.get("rank"),
        "provider": result.get("provider"),
        "query": result.get("query"),
        "reasons": reasons or ["insufficient exact-entity evidence"],
        "method": "deterministic_search_candidate_identity_v1",
    }


def choose_search_candidate(profile: dict[str, Any], results: list[dict[str, Any]]) -> dict[str, Any]:
    assessed = [score_search_candidate(profile, result) for result in results]
    assessed.sort(key=lambda item: (-item.get("score", 0.0), item.get("rank") or 10_000, item.get("url") or ""))
    accepted = [item for item in assessed if item.get("publishable_candidate")]
    return {
        "selected": accepted[0] if accepted else None,
        "candidates": assessed,
        "abstained": not accepted,
        "policy": "A search result is only a crawl candidate. Publication still requires fetched-page exact-entity verification.",
    }


def probe_heuristic_domain(
    profile: dict[str, Any],
    *,
    budget: Any | None = None,
    timeout: float = 4.0,
) -> str | None:
    """Heuristic Level-3 Norwegian domain probe (e.g. equinor.no, kongsberg.no).

    Generates high-probability Norwegian .no domain candidates from company legal name,
    probes them via HTTP GET, and verifies that the company's 9-digit org number appears on the page.
    Completely free ($0.00 spend, zero API keys needed).
    """
    name = str(profile.get("name") or "")
    org = str(profile.get("organisation_number") or "")
    if not name or not org:
        return None

    tokens = [t for t in _tokens(name) if t not in GENERIC_NAME_TOKENS]
    if not tokens:
        return None

    candidates: list[str] = []
    # Candidate 1: full compact core (e.g. 'equinor', 'kongsberggruppen')
    candidates.append("".join(tokens))
    # Candidate 2: hyphenated core (e.g. 'kongsberg-gruppen')
    if len(tokens) >= 2:
        candidates.append("-".join(tokens))
    # Candidate 3: first distinctive word if long enough (e.g. 'equinor' for 'Equinor ASA')
    if len(tokens[0]) >= 4 and tokens[0] != candidates[0]:
        candidates.append(tokens[0])

    for slug in candidates[:3]:
        if len(slug) < 3:
            continue
        url = f"https://www.{slug}.no"
        if budget and not budget.can_proceed():
            return None

        try:
            req = urllib.request.Request(
                url,
                headers={"User-Agent": "Mozilla/5.0 (compatible; SignalpostAgent/1.0; +https://builderr.ai)"},
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                if resp.status in (200, 301, 302):
                    raw = resp.read(250_000).decode("utf-8", errors="replace")
                    if budget:
                        budget.record_request(bytes_received=len(raw), cost_usd=0.0)
                    if org in raw or name.lower() in raw.lower():
                        return resp.geturl() or url
        except Exception:
            pass

    return None


GENERIC_MAIL_DOMAINS = {
    "gmail.com", "hotmail.com", "outlook.com", "yahoo.com", "icloud.com",
    "me.com", "mac.com", "live.no", "live.com", "online.no", "c2i.net",
    "frisurf.no", "broadpark.no", "start.no", "spray.no", "getmail.no",
    "ntebb.no", "lyse.net", "enitel.no", "styrerommet.net", "vibbo.no", "bbl.no",
    "mail.com", "zoho.com", "proton.me", "protonmail.com", "fastmail.com",
}


def extract_email_domain_candidate(profile: dict[str, Any]) -> str | None:
    """Extract a high-confidence website URL candidate from company email registered in BRREG.

    If a company registered 'kontakt@firmanavn.no' with Brønnøysund, this extracts
    'https://www.firmanavn.no'. Excludes generic personal email hosts (gmail, hotmail, etc.).
    $0.00 cost, zero API keys required.
    """
    ev = profile.get("evidence") or {}
    reg_live = (ev.get("registry_live") or {}).get("value") or {}
    reg_bulk = (ev.get("registry") or {}).get("value") or {}
    email = reg_live.get("epostadresse") or reg_bulk.get("epostadresse") or profile.get("email") or ""
    if not email or "@" not in str(email):
        return None
    domain = str(email).split("@")[-1].lower().strip()
    domain = domain.translate(str.maketrans({"æ": "ae", "ø": "o", "å": "a"}))
    if not domain or domain in GENERIC_MAIL_DOMAINS or "." not in domain:
        return None
    return f"https://www.{domain}"
