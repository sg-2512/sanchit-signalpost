"""Source-backed Norwegian regulatory approvals."""
from __future__ import annotations

import hashlib
import json
import time
import urllib.parse
import urllib.request
from typing import Any

ARBEIDSTILSYNET_API_ENDPOINT = "https://data.arbeidstilsynet.no/renhold/api/v1/virksomhet"
FINANSTILSYNET_REGISTRY_URL = "https://www.finanstilsynet.no/virksomhetsregisteret"
USER_AGENT = "SignalPostAgent/1.0 (+https://builderr.ai/signalpost)"


def fetch_regulatory_licenses(
    org_number: str,
    company_name: str,
    industry_code: str = "",
    *,
    budget: Any | None = None,
    timeout: float = 6.0,
) -> list[dict[str, Any]]:
    """Fetch official regulatory licenses and supervisory approvals for an enterprise."""
    if not org_number or len(org_number) < 9:
        return []

    clean_org = org_number.strip().replace(" ", "")
    clean_name = company_name.strip()
    is_cleaning_or_staffing = any(industry_code.startswith(prefix) for prefix in ("81.2", "78.1", "78.2", "78.3"))
    if not is_cleaning_or_staffing or (budget and not budget.can_proceed()):
        return []
    start_time = time.perf_counter()
    bytes_received = 0
    retrieved_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    observations: list[dict[str, Any]] = []

    # 1. Arbeidstilsynet Cleaning & Staffing Approval Check
    attempted = False
    if is_cleaning_or_staffing:
        url = f"{ARBEIDSTILSYNET_API_ENDPOINT}/{clean_org}"
        try:
            req = urllib.request.Request(
                url,
                headers={
                    "User-Agent": USER_AGENT,
                    "Accept": "application/json",
                },
            )
            attempted = True
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read(200_000)
                bytes_received += len(raw)
                data = json.loads(raw.decode("utf-8", errors="replace"))

            source_org = str(data.get("organisasjonsnummer") or data.get("orgnr") or "").replace(" ", "")
            status = str(data.get("status") or data.get("godkjenningsstatus") or "").strip()
            if source_org == clean_org and status.casefold() in {"godkjent", "godkjent med vilkår"}:
                h_digest = hashlib.sha256(raw).hexdigest()
                obs_id = f"license-arbeidstilsynet-{clean_org}-{h_digest[:16]}"
                observations.append({
                "id": obs_id,
                "organisation_number": clean_org,
                "platform": "arbeidstilsynet",
                "signal_type": "regulatory_license_record",
                "source_url": url,
                "retrieved_at": retrieved_at,
                "content_sha256": h_digest,
                "exact_entity": True,
                "identity_proof": [
                    {
                        "type": "official_labour_inspection_license",
                        "licensee_name": clean_name,
                        "organisation_number": clean_org,
                        "status": status,
                    }
                ],
                "acquisition_mode": "official_api",
                "rights_status": "approved",
                "source_class": "official_supervisory_authority",
                "evidence_span": f"Arbeidstilsynet godkjenning for {clean_name}: {status}",
                "metrics": {
                    "authority": "Arbeidstilsynet",
                    "license_type": "Offentlig godkjent virksomhet",
                    "status": status,
                },
                "strategy": "arbeidstilsynet_license_lookup",
                })
        except Exception:
            pass

    elapsed_ms = int((time.perf_counter() - start_time) * 1000)
    if budget and attempted:
        budget.record_request(cost_usd=0.0, bytes_received=bytes_received, elapsed_ms=elapsed_ms)

    return observations
