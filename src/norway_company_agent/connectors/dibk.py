"""Verify central construction approval against the official DiBK register."""
from __future__ import annotations

import re
import threading
import time
from collections import deque
from datetime import date, datetime, timezone
from urllib.parse import urlsplit

from ..evidence import capture_source_fields
from ..http import fetch_json

ENDPOINT = "https://sgregister.dibk.no/api/enterprises"
_REQUESTS: deque[float] = deque()
_LOCK = threading.Lock()


def _below_rate_limit() -> bool:
    with _LOCK:
        now = time.monotonic()
        while _REQUESTS and now - _REQUESTS[0] >= 300:
            _REQUESTS.popleft()
        if len(_REQUESTS) >= 300:
            return False
        _REQUESTS.append(now)
        return True


def fetch_construction_approval(org: str, *, budget=None) -> dict | None:
    if not re.fullmatch(r"[0-9]{9}", org) or not _below_rate_limit():
        return None
    fetch = budget.fetch_json if budget is not None else fetch_json
    result = fetch(f"{ENDPOINT}/{org}", timeout=6.0, attempts=1)
    if (result.status != 200 or urlsplit(result.url).hostname != "sgregister.dibk.no"
            or not isinstance(result.body, dict) or not result.content_sha256):
        return None
    body = result.body.get("dibk-sgdata", result.body)
    if not isinstance(body, dict):
        return None
    company = body.get("enterprise") or {}
    status = body.get("status") or {}
    if str(company.get("organizational_number")) != org or status.get("approved") is not True:
        return None
    valid_through = str(status.get("approval_period_to") or "")
    valid_from = str(status.get("approval_period_from") or "")
    try:
        today = datetime.now(timezone.utc).date()
        if date.fromisoformat(valid_through) < today or (valid_from and date.fromisoformat(valid_from) > today):
            return None
    except ValueError:
        return None
    areas = [
        {"function": row.get("function"), "subject_area": row.get("subject_area"), "grade": row.get("grade")}
        for row in body.get("valid_approval_areas", []) if isinstance(row, dict) and row.get("subject_area")
    ]
    metrics = {"authority": "Direktoratet for byggkvalitet (DiBK)", "license_type": "Sentral godkjenning",
               "status": "approved", "valid_from": valid_from or None, "valid_through": valid_through, "approval_areas": areas,
               "as_of": result.retrieved_at, "data_license": "CC BY 4.0"}
    terms = body.get("additional_terms")
    if isinstance(terms, dict) and isinstance(terms.get("educational_enterprise_approved"), bool):
        metrics["approved_training_company"] = terms["educational_enterprise_approved"]
    prefix = "/dibk-sgdata" if body is not result.body else ""
    source_fields = capture_source_fields(result.body, tuple(prefix + key for key in (
        "/enterprise/organizational_number", "/status/approved", "/status/approval_period_from",
        "/status/approval_period_to", "/valid_approval_areas", "/additional_terms/educational_enterprise_approved",
    )))
    return {"id": f"approval-dibk-{org}", "organisation_number": org, "platform": "dibk",
            "signal_type": "regulatory_license_record", "source_url": result.url,
            "content_sha256": result.content_sha256, "retrieved_at": result.retrieved_at,
            "exact_entity": True, "identity_proof": [{"type": "official_approval_register",
                "organisation_number": org, "name": company.get("name")}],
            "acquisition_mode": "official_api", "rights_status": "approved",
            "source_class": "official_supervisory_authority", "strategy": "dibk_central_approval",
            "source_fields": source_fields,
            "metrics": metrics}
