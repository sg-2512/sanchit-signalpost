"""Verify central construction approval against the official DiBK register."""
from __future__ import annotations

import re
from datetime import date, datetime, timezone
from urllib.parse import urlsplit

from ..http import fetch_json

ENDPOINT = "https://sgregister.dibk.no/api/enterprises"


def fetch_construction_approval(org: str, *, budget=None) -> dict | None:
    if not re.fullmatch(r"[0-9]{9}", org):
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
    try:
        if date.fromisoformat(valid_through) < datetime.now(timezone.utc).date():
            return None
    except ValueError:
        return None
    areas = [
        {"function": row.get("function"), "subject_area": row.get("subject_area"), "grade": row.get("grade")}
        for row in body.get("valid_approval_areas", []) if isinstance(row, dict) and row.get("subject_area")
    ]
    metrics = {"authority": "Direktoratet for byggkvalitet (DiBK)", "license_type": "Sentral godkjenning",
               "status": "approved", "valid_through": valid_through, "approval_areas": areas,
               "as_of": result.retrieved_at, "data_license": "CC BY 4.0"}
    return {"id": f"approval-dibk-{org}", "organisation_number": org, "platform": "dibk",
            "signal_type": "regulatory_license_record", "source_url": result.url,
            "content_sha256": result.content_sha256, "retrieved_at": result.retrieved_at,
            "exact_entity": True, "identity_proof": [{"type": "official_approval_register",
                "organisation_number": org, "name": company.get("name")}],
            "acquisition_mode": "official_api", "rights_status": "approved",
            "source_class": "official_supervisory_authority", "strategy": "dibk_central_approval",
            "evidence_span": f"{company.get('name')} ({org}): DiBK status.approved=true; status.approval_period_to={valid_through}",
            "metrics": metrics}
