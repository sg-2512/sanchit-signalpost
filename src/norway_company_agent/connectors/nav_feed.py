"""Bounded NAV vacancy-feed discovery with live, exact-employer verification.

The feed is a change log, not an employer search API. Its recent entries are
indexed once per process; names are only retrieval hints. Each published ad is
re-read from NAV and must be ACTIVE with the company's or a registered
workplace's organisation number. The public token is for experimental use.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import email.utils
from contextlib import nullcontext
import hashlib
import json
import os
import re
import threading
import time
import urllib.parse
import urllib.error
import urllib.request
from typing import Any

from .nav_jobs import _strict_employer_core

BASE = "https://pam-stilling-feed.nav.no"
TOKEN_URL = BASE + "/api/publicToken"
FEED_URL = BASE + "/api/v1/feed"
MAX_FEED_PAGES = 50
MAX_DETAILS_PER_COMPANY = 8
MAX_JOBS_PER_COMPANY = 3
_lock = threading.Lock()
_index: dict[str, list[dict[str, Any]]] | None = None
_token = ""
_state = "not_started"


def reset_nav_feed() -> None:
    """Reset the per-run index when a runner executes a second batch in-process."""
    global _index, _token, _state
    with _lock:
        _index, _token, _state = None, "", "not_started"


def _nav_url(value: str, path_prefix: str) -> str | None:
    url = urllib.parse.urljoin(BASE, value)
    parts = urllib.parse.urlsplit(url)
    if parts.scheme != "https" or parts.hostname != "pam-stilling-feed.nav.no":
        return None
    if parts.port not in (None, 443) or not parts.path.startswith(path_prefix):
        return None
    return url


def _get(url: str, budget: Any, *, token: str = "", since: str = "", timeout: float = 12.0) -> tuple[bytes, dict[str, Any]]:
    headers = {"Accept": "application/json", "User-Agent": "SignalPostResearch/1.0"}
    if token:
        headers["Authorization"] = "Bearer " + token
    if since:
        headers["If-Modified-Since"] = since
    for attempt in range(2):
        if not budget.try_start_request(reserve=2):
            raise RuntimeError("request_budget_exhausted")
        started = time.monotonic()
        received = 0
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=timeout) as response:
                if not _nav_url(response.geturl(), "/api/"):
                    raise RuntimeError("unexpected_nav_redirect")
                raw = response.read(5_000_001)
                received = len(raw)
                if received > 5_000_000:
                    raise RuntimeError("oversize_nav_response")
                return raw, dict(response.headers)
        except urllib.error.HTTPError as exc:
            if exc.code not in {429, 502, 503, 504} or attempt:
                raise
            retry_after = str(exc.headers.get("Retry-After") or "")
            wait = float(retry_after) if retry_after.isdigit() else 2.0
            time.sleep(min(max(wait, 0.5), 5.0))
        finally:
            # The slot is charged before sending. This adds only byte/latency data.
            budget.record_request(count=0, bytes_received=received,
                                  elapsed_ms=int((time.monotonic() - started) * 1000))
    raise RuntimeError("nav_retry_exhausted")


def _build_index(budget: Any) -> tuple[dict[str, list[dict[str, Any]]], str, str]:
    token = os.environ.get("SIGNALPOST_NAV_FEED_TOKEN", "").strip()
    if not token:
        raw, _ = _get(TOKEN_URL, budget)
        token = raw.decode("utf-8", errors="replace").strip().splitlines()[-1]
    if token.count(".") != 2:
        raise RuntimeError("invalid_nav_feed_token")
    # A recent window bounds cold-run work. It is not a complete index of all
    # active NAV ads, so absence means not found in this window, not no jobs.
    since = email.utils.format_datetime(datetime.now(timezone.utc) - timedelta(days=21), usegmt=True)
    by_uuid: dict[str, dict[str, Any]] = {}
    url: str | None = FEED_URL
    pages = 0
    complete = False
    while url and pages < MAX_FEED_PAGES and budget.can_proceed(reserve=2):
        raw, _ = _get(url, budget, token=token, since=since if pages == 0 else "")
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise RuntimeError("invalid_nav_feed_page")
        pages += 1
        for item in payload.get("items") or []:
            if not isinstance(item, dict):
                continue
            entry = item.get("_feed_entry") or {}
            uuid = str(entry.get("uuid") or "")
            if not re.fullmatch(r"[0-9a-fA-F-]{36}", uuid):
                continue
            if str(entry.get("status") or "").upper() != "ACTIVE":
                by_uuid.pop(uuid, None)
                continue
            detail_url = _nav_url(str(item.get("url") or ""), "/api/v1/feedentry/")
            if detail_url:
                by_uuid[uuid] = {"uuid": uuid, "name": str(entry.get("businessName") or ""), "url": detail_url}
        next_url = payload.get("next_url")
        url = _nav_url(str(next_url), "/api/v1/feed") if next_url else None
        if not url:
            complete = True
    index: dict[str, list[dict[str, Any]]] = {}
    for item in by_uuid.values():
        key = _strict_employer_core(item["name"])
        if key:
            index.setdefault(key, []).append(item)
    return index, token, "complete" if complete else f"partial_{pages}_pages"


def nav_feed_jobs_for_company(
    company_name: str, org_number: str, *, subunits: Any = (), budget: Any,
) -> tuple[list[dict[str, Any]], list[str], dict[str, Any]]:
    """Return currently active jobs and employer homepages for an exact entity."""
    global _index, _token, _state
    with _lock:
        if _index is None and _state == "not_started":
            try:
                # This is one shared feed index for the entire batch. Do not
                # charge its outbound calls to the unlucky first company.
                global_scope = budget.company_context("") if hasattr(budget, "company_context") else nullcontext()
                with global_scope:
                    _index, _token, _state = _build_index(budget)
            except Exception as exc:
                _index, _token, _state = {}, "", f"failed:{type(exc).__name__}"
        index, token, state = _index or {}, _token, _state
    diagnostics: dict[str, Any] = {
        "feed_state": state, "candidate_ads": 0, "verified_ads": 0,
        "lookback_days": 21, "scope": "recent_changed_ads_not_all_active_vacancies",
    }
    if not token:
        return [], [], diagnostics
    subunit_rows = [row for row in subunits if isinstance(row, dict)]
    wanted = {re.sub(r"\D", "", str(org_number))}
    wanted.update(re.sub(r"\D", "", str(row.get("organisation_number") or "")) for row in subunit_rows)
    names = [company_name, *(str(row.get("name") or "") for row in subunit_rows)]
    candidates: list[dict[str, Any]] = []
    seen: set[str] = set()
    for name in names:
        key = _strict_employer_core(name)
        for item in index.get(key, []):
            if item["uuid"] not in seen:
                seen.add(item["uuid"])
                candidates.append(item)
    diagnostics["candidate_ads"] = len(candidates)
    observations: list[dict[str, Any]] = []
    homepages: list[str] = []
    for item in candidates[:MAX_DETAILS_PER_COMPANY]:
        if len(observations) >= MAX_JOBS_PER_COMPANY or not budget.can_proceed(reserve=2):
            break
        try:
            raw, _ = _get(item["url"], budget, token=token)
            detail = json.loads(raw)
            ad = detail.get("ad_content") or {}
            employer = ad.get("employer") or {}
            employer_org = re.sub(r"\D", "", str(employer.get("orgnr") or ""))
            expires = str(ad.get("expires") or "")[:10]
            if (str(detail.get("status") or "").upper() != "ACTIVE" or employer_org not in wanted
                    or (expires and expires < datetime.now(timezone.utc).date().isoformat())):
                continue
            uuid = str(detail.get("uuid") or "")
            if uuid != item["uuid"]:
                continue
            title = str(ad.get("title") or ad.get("jobtitle") or "").strip()
            if not title:
                continue
            homepage = str(employer.get("homepage") or "").strip()
            if homepage.startswith("https://") and homepage not in homepages:
                homepages.append(homepage)
            now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
            observations.append({
                "id": "nav-feed-job-" + hashlib.sha256(f"{org_number}|{uuid}".encode()).hexdigest()[:24],
                "organisation_number": str(org_number), "platform": "job_board", "signal_type": "job_posting",
                "source_url": f"https://arbeidsplassen.nav.no/stillinger/stilling/{uuid}",
                "retrieved_at": now, "published_at": ad.get("published"), "expires_at": ad.get("expires"),
                "exact_entity": True, "content_sha256": hashlib.sha256(raw).hexdigest(),
                "kind": "nav_ad", "employer_org": employer_org, "employer_name": employer.get("name"),
                "match_type": "nav_feed_employer_orgnr_match", "job_title": title,
                "evidence_span": f"Active NAV vacancy '{title}' for employer organisation number {employer_org}",
                "source_class": "official_public_employment_registry", "rights_status": "approved",
                "acquisition_mode": "official_api", "strategy": "nav_official_vacancy_feed",
                "identity_proof": [{"type": "nav_feed_employer_orgnr_match", "employer_org": employer_org}],
            })
        except Exception:
            diagnostics["detail_failures"] = diagnostics.get("detail_failures", 0) + 1
    diagnostics["verified_ads"] = len(observations)
    return observations, homepages, diagnostics
