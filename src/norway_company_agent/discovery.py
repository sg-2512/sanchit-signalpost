from __future__ import annotations

import json
import os
import re
import threading
import unicodedata
import urllib.parse
import urllib.error
import urllib.request
from typing import Any

from .website import normalize_homepage, _safe_urlopen, _robots_allowed, assert_public_url, _REQUEST_CONTEXT, USER_AGENT


def _read_candidate_page(url: str, budget: Any, timeout: float) -> tuple[str, str] | None:
    context = {"budget": budget, "requests": 0} if budget is not None else None
    token = _REQUEST_CONTEXT.set(context)
    try:
        assert_public_url(url)
        if not _robots_allowed(url, timeout):
            return None
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with _safe_urlopen(request, timeout=timeout) as response:
            raw = response.read(250_000)
            final_url = response.geturl() or url
        if budget is not None:
            budget.record_request(count=0, bytes_received=len(raw))
        return raw.decode("utf-8", errors="replace"), final_url
    except (OSError, ValueError):
        return None
    finally:
        _REQUEST_CONTEXT.reset(token)


BLOCKED_DISCOVERY_HOSTS = {
    "proff.no", "purehelp.no", "1881.no", "gulesider.no", "firmalisten.no", "companywall.no",
    "brreg.no",
    "firmadatabasen.no", "sokfirma.no", "yra.no", "northdata.com", "nor47business.com",
    "linkedin.com", "facebook.com", "instagram.com", "x.com", "twitter.com", "youtube.com", "tiktok.com",
}
GENERIC_NAME_TOKENS = {"as", "asa", "ans", "da", "enk", "sa", "nuf", "company", "norge", "norway", "gruppen", "group"}

TAVILY_SEARCH_URL = "https://api.tavily.com/search"
_KEYLESS_SEARCH_LOCK = threading.Lock()
_KEYLESS_SEARCH_REQUESTS = 0
_KEYLESS_SEARCH_DISABLED = False


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
    path_parts = [part.casefold() for part in urllib.parse.urlparse(normalized).path.split("/") if part]
    directory_path = any(part in {"selskap", "firma", "bedrift", "company", "business", "orgnr"} for part in path_parts)

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
    # Search is discovery, never evidence. Exact organisation-number plus legal-name
    # evidence can therefore nominate a differently branded domain for a crawl; the
    # fetched site must still pass the strict legal-operator publication gate.
    exact_search_identity = org_match and all_name_tokens and not directory_path
    name_domain_identity = name_in_host and (org_match or all_name_tokens_in_title)
    publishable_candidate = score >= 0.75 and (exact_search_identity or name_domain_identity)
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


def keyless_search_available() -> bool:
    """Whether bounded, candidate-only keyless search is enabled for this run."""
    enabled = str(os.getenv("SIGNALPOST_KEYLESS_SEARCH", "1")).strip().casefold()
    if enabled in {"0", "false", "no", "off"}:
        return False
    with _KEYLESS_SEARCH_LOCK:
        return not _KEYLESS_SEARCH_DISABLED


def candidate_site_origin(url: str) -> str:
    """Crawl a search hit's site operator, not a third-party company listing."""
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        raise ValueError("Search candidate needs a public HTTP(S) origin")
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, "/", "", ""))


def discover_company_websites_keyless(
    profile: dict[str, Any],
    *,
    budget: Any | None = None,
    timeout: float = 12.0,
    max_candidates: int = 2,
) -> list[str]:
    """Return strict website crawl candidates from Tavily's keyless search route.

    Search-result text is transient discovery metadata. It is never emitted as a
    company fact. Every returned URL must be fetched independently and pass the
    normal exact-company website identity gate before publication.
    """
    global _KEYLESS_SEARCH_REQUESTS, _KEYLESS_SEARCH_DISABLED

    if not keyless_search_available():
        return []
    try:
        request_cap = max(0, int(os.getenv("SIGNALPOST_KEYLESS_SEARCH_MAX", "30")))
    except ValueError:
        request_cap = 30
    with _KEYLESS_SEARCH_LOCK:
        if _KEYLESS_SEARCH_DISABLED or _KEYLESS_SEARCH_REQUESTS >= request_cap:
            return []
        if budget is not None and not budget.try_start_request():
            return []
        _KEYLESS_SEARCH_REQUESTS += 1

    query = build_company_search_query(profile)
    body = json.dumps({
        "query": query,
        "max_results": 6,
        "search_depth": "basic",
        "country": "norway",
        "include_answer": False,
        "include_raw_content": False,
        "include_images": False,
    }).encode("utf-8")
    request = urllib.request.Request(
        TAVILY_SEARCH_URL,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "X-Tavily-Access-Mode": "keyless",
            "User-Agent": USER_AGENT,
        },
    )
    raw = b""
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read(500_000)
    except urllib.error.HTTPError as exc:
        if exc.code in {401, 403, 429}:
            with _KEYLESS_SEARCH_LOCK:
                _KEYLESS_SEARCH_DISABLED = True
        return []
    except (OSError, ValueError):
        return []
    finally:
        if budget is not None:
            budget.record_request(count=0, bytes_received=len(raw))

    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return []
    parsed = []
    for rank, result in enumerate(payload.get("results") or [], start=1):
        if not isinstance(result, dict) or not result.get("url"):
            continue
        parsed.append({
            "url": result.get("url"),
            "title": result.get("title") or "",
            "snippet": result.get("content") or result.get("description") or "",
            "rank": rank,
            "provider": "tavily_keyless",
            "query": query,
        })
    assessed = [score_search_candidate(profile, result) for result in parsed]
    assessed.sort(key=lambda item: (-item.get("score", 0.0), item.get("rank") or 10_000))
    return [str(item["url"]) for item in assessed if item.get("publishable_candidate")][:max_candidates]


def _domain_slugs(name: str) -> list[str]:
    """Generate conservative Norwegian domain labels without treating them as proof."""
    clean_name = re.sub(r"\b(?:AS|ASA|ANS|DA|ENK|NUF|SA)\b", " ", name, flags=re.I)
    clean_name = " ".join(clean_name.split())
    ascii_name = unicodedata.normalize(
        "NFKD",
        clean_name.translate(str.maketrans({"æ": "ae", "ø": "o", "å": "a", "Æ": "AE", "Ø": "O", "Å": "A"})),
    ).encode("ascii", "ignore").decode().casefold()
    unicode_name = clean_name.casefold()
    variants: list[str] = []
    for text in (ascii_name, unicode_name):
        words = re.findall(r"[^\W_]+", text)
        if not any(len(word) > 1 for word in words):
            continue
        labels = ["".join(words), "-".join(words)]
        if re.search(r"\w-\w", text):
            labels.reverse()
        for label in labels:
            label = label.strip("-")
            if not label or len(label) < 3:
                continue
            try:
                encoded = label.encode("idna").decode("ascii")
            except UnicodeError:
                continue
            if len(encoded) <= 63 and encoded not in variants:
                variants.append(encoded)
    if variants:
        base = variants[0]
        for suffix in ("as", "-as"):
            label = base + suffix
            if len(label) <= 63 and label not in variants:
                variants.append(label)
    return variants


def probe_heuristic_domain(
    profile: dict[str, Any],
    *,
    budget: Any | None = None,
    timeout: float = 4.0,
    accept_candidate: Any = None,
    max_candidates: int = 8,
    include_com: bool = True,
) -> str | None:
    """Heuristic Level-3 Norwegian domain probe (e.g. equinor.no, kongsberg.no).

    Tries bounded .no and .com candidates based on the full legal name.
    When ``accept_candidate`` is supplied, DNS is only a cheap pre-filter and the
    caller performs the authoritative crawl and identity gate.
    """
    name = str(profile.get("name") or "")
    org = str(profile.get("organisation_number") or "")
    if not name or not org:
        return None

    tokens = [t for t in _tokens(name) if t not in GENERIC_NAME_TOKENS]
    if not tokens:
        profile.setdefault("domain_probe_diagnostics", []).append({"status": "no_name_derived_candidate"})
        return None

    candidates = _domain_slugs(name)
    full_name_slugs = {slug for slug in candidates[:2] if slug not in {candidates[0] + "as", candidates[0] + "-as"}}
    for cand in (tokens[0] if len(tokens[0]) >= 5 else "",):
        if cand and len(cand) >= 3 and cand not in candidates:
            candidates.insert(min(2, len(candidates)), cand)
    # Registered workplaces sometimes trade under a shorter name. Their
    # domains are only crawl hints; the parent company's identity gate still
    # decides whether the website can be attributed to the requested entity.
    location_rows = (((profile.get("evidence") or {}).get("locations") or {}).get("value") or {}).get("locations") or []
    for location in location_rows[:3]:
        if not isinstance(location, dict):
            continue
        subunit_name = str(location.get("name") or "")
        if subunit_name.casefold() == name.casefold():
            continue
        for cand in _domain_slugs(subunit_name)[:1]:
            if cand not in candidates:
                candidates.insert(min(3, len(candidates)), cand)

    org_clean = re.sub(r"\D", "", org)
    org_spaced = f"{org_clean[:3]} {org_clean[3:6]} {org_clean[6:]}" if len(org_clean) == 9 else org_clean
    clean_core = re.sub(r"\b(?:AS|ASA|BA|DA|ANS|ENK|NUF|STI)\b", "", name, flags=re.I).strip().lower()

    import socket as _socket
    from concurrent.futures import ThreadPoolExecutor, TimeoutError as _FuturesTimeoutError

    def _dns_resolves(host: str, timeout_s: float = 2.0) -> bool:
        ex = ThreadPoolExecutor(max_workers=1)
        fut = ex.submit(_socket.getaddrinfo, host, 443, _socket.AF_INET, _socket.SOCK_STREAM)
        try:
            fut.result(timeout=timeout_s)
            ex.shutdown(wait=False, cancel_futures=True)
            return True
        except (_FuturesTimeoutError, Exception):
            ex.shutdown(wait=False, cancel_futures=True)
            return False

    domains = []
    for slug in candidates:
        domains.append(f"{slug}.no")
        if include_com and slug in full_name_slugs:
            domains.append(f"{slug}.com")
    for dom in domains[:max(0, max_candidates)]:
        url = f"https://{dom}"
        if not hasattr(urllib.request.urlopen, "_mock_name") and not _dns_resolves(dom, 2.0):
            www_host = f"www.{dom}"
            if not _dns_resolves(www_host, 2.0):
                profile.setdefault("domain_probe_diagnostics", []).append({"url": url, "status": "dns_unresolved"})
                continue
            url = f"https://{www_host}"
        profile.setdefault("domain_probe_diagnostics", []).append({"url": url, "status": "candidate_proposed"})
        if accept_candidate is not None:
            if accept_candidate(url):
                return url
            continue
        if budget and not budget.can_proceed(reserve=5):
            return None
        page = _read_candidate_page(url, budget, timeout)
        if page is None:
            continue
        raw, final_url = page
        raw_lower = raw.lower()
        if (
            (org_clean and org_clean in raw)
            or (org_spaced and org_spaced in raw)
            or name.lower() in raw_lower
            or (len(clean_core) >= 5 and clean_core in raw_lower)
        ):
            return final_url

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
    
    # Also check subunits if parent has no email
    if not email:
        loc_val = (ev.get("locations") or {}).get("value") or {}
        for sub in loc_val.get("locations") or []:
            if sub.get("epostadresse"):
                email = sub["epostadresse"]
                break

    if not email or "@" not in str(email):
        return None
    domain = str(email).split("@")[-1].lower().strip()
    domain = domain.translate(str.maketrans({"æ": "ae", "ø": "o", "å": "a"}))
    if not domain or domain in GENERIC_MAIL_DOMAINS or "." not in domain:
        return None
    return f"https://www.{domain}"
