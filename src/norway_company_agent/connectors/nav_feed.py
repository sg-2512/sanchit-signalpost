"""Bounded NAV vacancy-feed discovery with live, exact-employer verification.

The feed is a change log, not an employer search API. Its recent entries are
indexed once per process; names are only retrieval hints. Each published ad is
re-read from NAV and must be ACTIVE with the company's or a registered
workplace's organisation number. The public token is for experimental use.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import email.utils
import hashlib
import json
import os
import re
import tempfile
import threading
import time
import urllib.parse
import urllib.error
import urllib.request
from contextlib import nullcontext
from pathlib import Path
from typing import Any

from ..evidence import capture_source_fields
from ..verified_items import is_unexpired_nav_deadline
from .nav_jobs import _strict_employer_core

BASE = "https://pam-stilling-feed.nav.no"
TOKEN_URL = BASE + "/api/publicToken"
FEED_URL = BASE + "/api/v1/feed"
MAX_FEED_PAGES = 50
MAX_DETAILS_PER_COMPANY = 8
MAX_JOBS_PER_COMPANY = 3
COLD_START_DAYS = 90
MAX_CACHE_BYTES = 8_000_000
_lock = threading.Lock()
_index: dict[str, list[dict[str, Any]]] | None = None
_token = ""
_state = "not_started"
_detail_updates: dict[str, tuple[str, str, str, str]] = {}


class _NoNavRedirect(urllib.request.HTTPRedirectHandler):
    """Keep NAV bearer credentials on the exact API origin."""

    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> None:
        return None


_OPENER = urllib.request.build_opener(_NoNavRedirect())


def reset_nav_feed() -> None:
    """Reset the per-run index when a runner executes a second batch in-process."""
    global _index, _token, _state
    with _lock:
        _index, _token, _state = None, "", "not_started"
        _detail_updates.clear()


def _nav_url(value: str, path_prefix: str) -> str | None:
    url = urllib.parse.urljoin(BASE, value)
    parts = urllib.parse.urlsplit(url)
    if parts.scheme != "https" or parts.hostname != "pam-stilling-feed.nav.no" or parts.username is not None:
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
            with _OPENER.open(urllib.request.Request(url, headers=headers), timeout=timeout) as response:
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


def _load_index(path: Path | None) -> tuple[str | None, dict[str, dict[str, Any]]]:
    if path is None:
        return None, {}
    try:
        with path.open("rb") as stream:
            raw = stream.read(MAX_CACHE_BYTES + 1)
        if len(raw) > MAX_CACHE_BYTES:
            return None, {}
        stored = json.loads(raw)
        cursor = _nav_url(stored["cursor"], "/api/v1/feed")
        if stored["version"] != 1 or not cursor:
            return None, {}
        rows = {
            key: row for key, row in stored["items"].items()
            if isinstance(row, dict) and key == row.get("uuid")
            and isinstance(row.get("name"), str)
            and _nav_url(row.get("url") or "", "/api/v1/feedentry/")
            and (not row.get("expires") or is_unexpired_nav_deadline(row["expires"]))
        }
        return cursor, rows
    except (OSError, ValueError, KeyError, AttributeError, TypeError):
        return None, {}


def _save_index(path: Path, cursor: str, rows: dict[str, dict[str, Any]]) -> None:
    raw = json.dumps({"version": 1, "cursor": cursor, "items": rows}, ensure_ascii=False).encode()
    if len(raw) > MAX_CACHE_BYTES:
        return
    temporary = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(raw)
        temporary.replace(path)
    except OSError:
        if temporary:
            temporary.unlink(missing_ok=True)


def flush_nav_feed_details(path: Path | None) -> None:
    """Persist only technical identifiers learned from registered live detail checks."""
    if path is None:
        return
    with _lock:
        if not _detail_updates:
            return
        cursor, rows = _load_index(path)
        if not cursor:
            return
        for uuid, update in _detail_updates.items():
            row = rows.get(uuid)
            if not row:
                continue
            detail_url, revision, employer, expires = update
            if row["url"] == detail_url and row.get("revision", "") == revision:
                if employer:
                    row.update(employer_org=employer, expires=expires)
                else:
                    rows.pop(uuid)
        _save_index(path, cursor, rows)
        _detail_updates.clear()


def _build_index(budget: Any, token: str, cache_path: Path | None = None) -> tuple[dict[str, list[dict[str, Any]]], str, str]:
    if not token:
        raw, _ = _get(TOKEN_URL, budget)
        token = raw.decode("utf-8", errors="replace").strip().splitlines()[-1]
    if token.count(".") != 2:
        raise RuntimeError("invalid_nav_feed_token")
    # A recent window bounds cold-run work. It is not a complete index of all
    # active NAV ads, so absence means not found in this window, not no jobs.
    cursor, by_uuid = _load_index(cache_path)
    since = email.utils.format_datetime(datetime.now(timezone.utc) - timedelta(days=COLD_START_DAYS), usegmt=True)
    url: str | None = cursor or FEED_URL
    checkpoint = None
    pages = 0
    complete = False
    while url and pages < MAX_FEED_PAGES and budget.can_proceed(reserve=2):
        raw, _ = _get(url, budget, token=token, since=since if not cursor and pages == 0 else "")
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
                revision = str(item.get("date_modified") or entry.get("sistEndret") or "")
                previous = by_uuid.get(uuid) or {}
                row = {"uuid": uuid, "name": str(entry.get("businessName") or ""), "url": detail_url, "revision": revision}
                if revision and previous.get("revision") == revision and previous.get("url") == detail_url:
                    row.update({key: previous[key] for key in ("employer_org", "expires") if key in previous})
                by_uuid[uuid] = row
        next_url = payload.get("next_url")
        next_page = _nav_url(str(next_url), "/api/v1/feed") if next_url else None
        if next_url and (not next_page or next_page == url):
            raise RuntimeError("invalid_nav_feed_cursor")
        checkpoint = next_page or url
        url = next_page
        complete = url is None
    if cache_path and checkpoint:
        _save_index(cache_path, checkpoint, by_uuid)
    index: dict[str, list[dict[str, Any]]] = {}
    for item in by_uuid.values():
        key = _strict_employer_core(item["name"])
        if key:
            index.setdefault(key, []).append(item)
        if re.fullmatch(r"\d{9}", str(item.get("employer_org") or "")):
            index.setdefault(f"org:{item['employer_org']}", []).append(item)
    return index, token, "complete" if complete else f"partial_{pages}_pages"


def nav_feed_jobs_for_company(
    company_name: str, org_number: str, *, subunits: Any = (), budget: Any,
    allow_experimental_token: bool = False,
    cache_path: Path | None = None,
) -> tuple[list[dict[str, Any]], list[str], dict[str, Any]]:
    """Return currently active jobs and employer homepages for an exact entity."""
    global _index, _token, _state
    registered_token = os.environ.get("SIGNALPOST_NAV_FEED_TOKEN", "").strip()
    if not registered_token and not allow_experimental_token:
        return [], [], {"feed_state": "registered_token_required", "candidate_ads": 0, "verified_ads": 0}
    with _lock:
        if _index is None and _state == "not_started":
            try:
                shared = budget.attributed_to("") if hasattr(budget, "attributed_to") else nullcontext()
                with shared:
                    _index, _token, _state = _build_index(budget, registered_token, cache_path if registered_token else None)
            except Exception as exc:
                _index, _token, _state = {}, "", f"failed:{type(exc).__name__}"
        index, token, state = _index or {}, _token, _state
    diagnostics: dict[str, Any] = {"feed_state": state, "cold_start_lookback_days": COLD_START_DAYS,
                                   "candidate_ads": 0, "verified_ads": 0}
    if not token:
        return [], [], diagnostics
    subunit_rows = [row for row in subunits if isinstance(row, dict)]
    wanted = {re.sub(r"\D", "", str(org_number))}
    wanted.update(re.sub(r"\D", "", str(row.get("organisation_number") or "")) for row in subunit_rows)
    names = [company_name, *(str(row.get("name") or "") for row in subunit_rows)]
    candidates: list[dict[str, Any]] = []
    seen: set[str] = set()
    keys = [*(f"org:{org}" for org in sorted(wanted) if len(org) == 9), *map(_strict_employer_core, names)]
    for key in dict.fromkeys(keys):
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
            content_key = "ad_content" if detail.get("ad_content") else "json"
            ad = detail.get(content_key) or {}
            employer = ad.get("employer") or {}
            employer_org = re.sub(r"\D", "", str(employer.get("orgnr") or ""))
            expires = str(ad.get("expires") or "")
            uuid = str(detail.get("uuid") or "")
            if uuid != item["uuid"]:
                continue
            current = str(detail.get("status") or "").upper() == "ACTIVE" and is_unexpired_nav_deadline(expires)
            if cache_path and registered_token:
                with _lock:
                    if not current:
                        _detail_updates[uuid] = (item["url"], item.get("revision", ""), "", "")
                    elif re.fullmatch(r"\d{9}", employer_org):
                        _detail_updates[uuid] = (item["url"], item.get("revision", ""), employer_org, expires)
            if not current or employer_org not in wanted:
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
                "valid_through": expires,
                "application_due": str(ad.get("applicationDue") or "").strip()[:80] or None,
                "nav_feed": {"status": "ACTIVE", "checked_at": now, "source_url": item["url"],
                             "registered_access": bool(registered_token and token == registered_token)},
                "source_fields": capture_source_fields(detail, (
                    "/uuid", "/status", f"/{content_key}/employer/orgnr", f"/{content_key}/expires",
                    f"/{content_key}/title", f"/{content_key}/jobtitle", f"/{content_key}/applicationDue",
                )),
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
