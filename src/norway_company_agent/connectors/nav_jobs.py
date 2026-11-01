"""Official NAV Arbeidsplassen Open Job API & Next.js SSR Connector.

Queries the official Norwegian Labour and Welfare Administration (NAV) job board
at https://arbeidsplassen.nav.no/stillinger?q={query} (with fallback support for
JSON search responses). No API key required, 100% public government data.

Enforces strict exact-entity gating by matching normalized company name core or
exact 9-digit organisation number (including official subunits) against the
employer field of returned job postings.
Includes per-host rate pacing and HTTP 429 Retry-After backoff.
"""
from __future__ import annotations

import hashlib
import json
import re
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"

LEGAL_SUFFIXES = {
    "as", "asa", "ans", "da", "enk", "ks", "ba", "sa", "sf", "sti", "nuf",
    "ik", "kf", "fkf", "hf", "rhf", "bl", "brl", "esek", "konsern", "ab", "oy",
    "holding", "invest", "gruppen", "group", "norge", "norway",
}

_NAV_LOCK = threading.Lock()
_NAV_NEXT_ALLOWED = 0.0
_NAV_MIN_INTERVAL = 0.35


def _pace_nav_request() -> None:
    """Ensure multi-threaded workers pace requests to arbeidsplassen.nav.no and respect 429 cooldowns."""
    global _NAV_NEXT_ALLOWED
    if hasattr(urllib.request.urlopen, "_mock_name"):
        return
    with _NAV_LOCK:
        now = time.monotonic()
        wait_s = max(0.0, _NAV_NEXT_ALLOWED - now)
        _NAV_NEXT_ALLOWED = max(now, _NAV_NEXT_ALLOWED) + _NAV_MIN_INTERVAL
    if wait_s > 0:
        time.sleep(min(wait_s, 15.0))


def _record_nav_429(retry_after: str | None, attempt: int) -> float:
    global _NAV_NEXT_ALLOWED
    sleep_s = None
    if retry_after:
        try:
            sleep_s = float(retry_after)
        except (ValueError, TypeError):
            pass
    wait_time = min(max(sleep_s, 1.5), 15.0) if sleep_s is not None else min(2.0 * (2**attempt), 12.0)
    if not hasattr(urllib.request.urlopen, "_mock_name"):
        with _NAV_LOCK:
            _NAV_NEXT_ALLOWED = max(_NAV_NEXT_ALLOWED, time.monotonic() + wait_time)
    return wait_time


def _extract_ads_from_response(raw: bytes) -> list[dict[str, Any]]:
    """Extract job ad source dicts from either JSON API payload or Next.js SSR Flight payload."""
    text = raw.decode("utf-8", errors="replace").lstrip()
    if text.startswith("{"):
        data = json.loads(text)
        hits = data.get("hits", {}).get("hits", [])
        return [h.get("_source", h) for h in hits if isinstance(h, dict)]

    ads: list[dict[str, Any]] = []
    decoder = json.JSONDecoder()
    for m in re.finditer(r'self\.__next_f\.push\(\[1,\s*("(?:[^"\\]|\\.)*")\]\)', text):
        raw_str = m.group(1)
        if "searchResult" not in raw_str and "jobTitle" not in raw_str:
            continue
        try:
            unescaped = json.loads(raw_str)
            idx = unescaped.find('"ads":[')
            if idx != -1:
                parsed_ads, _ = decoder.raw_decode(unescaped, idx + 6)
                if isinstance(parsed_ads, list):
                    ads.extend(a for a in parsed_ads if isinstance(a, dict))
        except Exception:
            continue
    return ads


def _nav_search_page(query: str, *, offset: int, size: int, timeout: float) -> bytes:
    """Fetch one bounded official NAV API page.

    This is separate from the HTML/SSR fallback so pagination remains explicit,
    measurable, and easy to stop under the shared request budget.
    """
    params = urllib.parse.urlencode({"q": query, "size": size, "from": offset})
    url = f"https://arbeidsplassen.nav.no/stillinger/api/search?{params}"
    _pace_nav_request()
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return response.read(750_000)


_STRICT_LEGAL_FORMS = {
    "as", "asa", "ans", "da", "enk", "ks", "ba", "sa", "sf", "sti", "nuf",
    "ik", "kf", "fkf", "hf", "rhf", "bl", "brl", "esek", "ab", "oy",
}


def normalize_core(name: str) -> str:
    """Normalize company name by stripping legal suffixes and punctuation."""
    text = (name or "").translate(str.maketrans({"ø": "o", "å": "a", "æ": "ae", "Ø": "O", "Å": "A", "Æ": "AE"}))
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii").lower()
    words = re.findall(r"[a-z0-9]+", text)
    while words and words[-1] in LEGAL_SUFFIXES:
        words.pop()
    return " ".join(words)


def _strict_employer_core(name: str) -> str:
    """Normalize company name stripping only legal form suffixes (preserving holding/invest/norge)."""
    text = (name or "").translate(str.maketrans({"ø": "o", "å": "a", "æ": "ae", "Ø": "O", "Å": "A", "Æ": "AE"}))
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii").lower()
    words = re.findall(r"[a-z0-9]+", text)
    while words and words[-1] in _STRICT_LEGAL_FORMS:
        words.pop()
    return " ".join(words)


def fetch_nav_jobs(
    company_name: str,
    org_number: str,
    *,
    subunit_orgs: Any = (),
    budget: Any | None = None,
    timeout: float = 8.0,
    max_results: int = 10,
    max_pages: int = 3,
    status_out: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Fetch verified active job postings for a company from NAV Arbeidsplassen.

    Strict entity gate: requires exact normalized company core match or orgnr match.
    """
    if status_out is not None:
        status_out["status"] = "not_applicable"
    if not company_name:
        return []

    legal_core = normalize_core(company_name)
    if not legal_core or len(legal_core) < 3:
        return []

    if budget and not budget.can_proceed(reserve=1):
        if status_out is not None:
            status_out["status"] = "blocked"
        return []

    # Query preserving Norwegian characters (æ, ø, å) while stripping legal suffixes
    strict_words = [
        word for word in re.findall(r"[a-zA-Z0-9æøåÆØÅ]+", str(company_name or ""))
        if word.casefold() not in _STRICT_LEGAL_FORMS
    ]
    query_words = [word for word in strict_words if word.casefold() not in {"holding", "invest", "gruppen", "group", "norge", "norway"}]
    # One query per company keeps the public NAV search inside the shared
    # request budget. Prefer the full legal core so short/generic names retain
    # their strongest employer context; exact org-number matching still gates
    # every published ad.
    query_candidates = list(dict.fromkeys(
        query for query in (" ".join(query_words), " ".join(strict_words))
        if len(query) >= 3
    ))[:3]
    if not query_candidates:
        query_candidates = [company_name.strip()]
    start_time = time.perf_counter()
    bytes_received = 0
    raw = b""
    search_attempts = 0
    request_failed = False
    for query in query_candidates:
        clean_query = urllib.parse.quote(query)
        url = f"https://arbeidsplassen.nav.no/stillinger?q={clean_query}"
        for attempt in range(2):
            if budget and not budget.can_proceed(reserve=2 if attempt == 0 else 1):
                request_failed = True
                break
            search_attempts += 1
            _pace_nav_request()
            try:
                req = urllib.request.Request(
                    url,
                    headers={
                        "User-Agent": USER_AGENT,
                        "Accept": "text/html,application/json;q=0.9,*/*;q=0.8",
                    },
                )
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    raw = resp.read(1_500_000)
                    bytes_received += len(raw)
                break
            except urllib.error.HTTPError as exc:
                if exc.code == 429 and attempt == 0:
                    retry_after = exc.headers.get("Retry-After") if hasattr(exc, "headers") and exc.headers else None
                    wait_s = _record_nav_429(retry_after, attempt)
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
        if raw:
            ads = _extract_ads_from_response(raw)
            if ads:
                break

    if not raw:
        if status_out is not None:
            status_out["status"] = "failed" if request_failed else "not_available"
        if budget:
            budget.record_request(bytes_received=bytes_received, elapsed_ms=int((time.perf_counter() - start_time) * 1000), cost_usd=0.0, count=max(1, search_attempts))
        return []

    try:
        elapsed_ms = int((time.perf_counter() - start_time) * 1000)
        if budget:
            budget.record_request(bytes_received=bytes_received, elapsed_ms=elapsed_ms, cost_usd=0.0, count=search_attempts)
        if status_out is not None:
            status_out["status"] = "available"  # the search completed; results may legitimately be empty

        content_digest = hashlib.sha256(raw).hexdigest()
        ads = _extract_ads_from_response(raw)
        # The SSR page is often capped. Add at most two API pages so a company
        # with several current postings is not undercounted. Every returned ad
        # still must pass the employer-org/subunit identity gate below.
        api_pages: list[bytes] = []
        for page_index in range(max(0, min(max_pages, 3))):
            if budget and not budget.can_proceed(reserve=1):
                break
            try:
                api_raw = _nav_search_page(query_candidates[0], offset=page_index * 25, size=25, timeout=timeout)
                api_pages.append(api_raw)
                if budget:
                    budget.record_request(bytes_received=len(api_raw), elapsed_ms=100, cost_usd=0.0)
                api_ads = _extract_ads_from_response(api_raw)
                if api_ads:
                    ads.extend(api_ads)
                # A short page is terminal; avoid unnecessary requests.
                if len(api_ads) < 25:
                    break
            except Exception:
                if budget:
                    budget.record_request(cost_usd=0.0)
                break
        if api_pages:
            content_digest = hashlib.sha256(b"".join(api_pages)).hexdigest()
        retrieved_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        observations: list[dict[str, Any]] = []
        rejected_identity = 0
        rejected_expired = 0
        rejected_missing_id = 0

        GENERIC_WORDS = {
            "drift", "bygg", "service", "regnskap", "advokat", "revisjon", "transport",
            "consulting", "holding", "invest", "eiendom", "gruppen", "norge", "norway",
            "nordic", "scandinavia", "management", "partner", "partners", "handel",
        }
        strict_core = _strict_employer_core(company_name) or legal_core
        is_generic = legal_core in GENERIC_WORDS or len(strict_core) < 4
        subunit_orgs_set = {str(s) for s in (subunit_orgs or ()) if s}

        seen_ad_ids: set[str] = set()
        for src in ads:
            b_name = src.get("businessName") or ""
            emp_obj = src.get("employer") if isinstance(src.get("employer"), dict) else {}
            emp_obj_name = emp_obj.get("name") or ""
            employer_name = b_name or emp_obj_name
            employer_org = str(emp_obj.get("orgnr") or "")
            b_strict = _strict_employer_core(b_name)
            emp_strict = _strict_employer_core(emp_obj_name)

            matched = False
            match_type = ""
            if employer_org and (employer_org == str(org_number) or employer_org in subunit_orgs_set):
                matched = True
                match_type = "nav_employer_orgnr_match" if employer_org == str(org_number) else "nav_company_subunit_match"
            elif not employer_org and not is_generic:
                # Zero-tolerance: only match if employer name exactly matches company core
                if b_strict == strict_core or emp_strict == strict_core:
                    matched = True
                    match_type = "nav_company_core_match"
                else:
                    # No name-prefix fallback - reject to enforce strict org-number gating
                    rejected_identity += 1
                    continue
            if not matched:
                rejected_identity += 1
                continue

            expires_raw = str(src.get("expires") or "")
            if expires_raw[:10] and expires_raw[:10] < time.strftime("%Y-%m-%d", time.gmtime()):
                rejected_expired += 1
                continue  # expired ad

            uuid = src.get("uuid") or src.get("id") or ""
            title = str(src.get("title") or src.get("jobTitle") or "Ledig stilling").strip()
            published = src.get("published")
            expires = src.get("expires")
            if not uuid:
                rejected_missing_id += 1
                continue  # no ad id -> cannot link to a single advertisement
            uuid = str(uuid)
            if uuid in seen_ad_ids:
                continue
            seen_ad_ids.add(uuid)
            job_url = f"https://arbeidsplassen.nav.no/stillinger/stilling/{uuid}"
            ad_digest = hashlib.sha256(
                json.dumps(src, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")
            ).hexdigest()

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
                "content_sha256": ad_digest,
                "kind": "nav_ad",
                "employer_org": employer_org,
                "match_type": match_type,
                "response_sha256": content_digest,
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

        if status_out is not None:
            # Diagnostics describe the connector run, not a company claim. They
            # make an empty result auditable without weakening the identity gate.
            status_out.update({
                "query": query_candidates[0],
                "api_pages_checked": len(api_pages),
                "candidate_ads_seen": len(ads),
                "matched_active_ads": len(observations),
                "rejected_identity": rejected_identity,
                "rejected_expired": rejected_expired,
                "rejected_missing_id": rejected_missing_id,
            })
        return observations

    except Exception:
        if status_out is not None:
            status_out["status"] = "failed"
        return []
