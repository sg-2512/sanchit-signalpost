from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Iterable

from .evidence import evidence, utc_now
from .http import fetch_json
from .official import accounting_obligation_assessment
from .sampling import iter_bulk


TERMINAL_STATES = {
    "complete",
    "available",
    "not_available",
    "blocked",
    "not_applicable",
    "ambiguous",
    "failed",
    "not_found",
    "blocked_policy",
    "blocked_robots",
    "source_error",
    "budget_exhausted",
    "submission_error",
}


def read_organisation_inputs(path: str | Path) -> list[dict[str, Any]]:
    source = Path(path)
    text = source.read_text(encoding="utf-8-sig")
    values: list[Any]
    if source.suffix == ".json":
        body = json.loads(text)
        values = body if isinstance(body, list) else body.get("organisation_numbers", [])
    elif source.suffix == ".jsonl":
        values = [json.loads(line) for line in text.splitlines() if line.strip()]
    else:
        values = []
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            if line.startswith("{") and line.endswith("}"):
                try:
                    values.append(json.loads(line))
                    continue
                except Exception:
                    pass
            values.append(line)
    records = []
    seen_orgs: set[str] = set()
    for value in values:
        if isinstance(value, dict):
            org = (
                value.get("organisation_number")
                or value.get("organisasjonsnummer")
                or value.get("orgnr")
                or value.get("org_nr")
                or value.get("orgNumber")
            )
        else:
            org = value
        raw_org = str(org or "").strip()
        digits = "".join(character for character in raw_org if character.isdigit())
        clean_org = digits if len(digits) == 9 else raw_org
        if not clean_org:
            clean_org = f"input_{len(records) + 1}"
        if clean_org in seen_orgs:
            continue
        seen_orgs.add(clean_org)
        record = {"organisation_number": clean_org}
        if isinstance(value, dict):
            for key in ("evaluation_split", "sample_slice"):
                if value.get(key) is not None:
                    record[key] = value[key]
        records.append(record)
    return records


def read_organisation_numbers(path: str | Path) -> list[str]:
    return [record["organisation_number"] for record in read_organisation_inputs(path)]


def profiles_from_bulk(path: str | Path, organisation_numbers: Iterable[str]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    requested = list(organisation_numbers)
    wanted = set(requested)
    hasher = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(1_048_576):
            hasher.update(chunk)
    snapshot_sha256 = hasher.hexdigest()
    retrieved_at = utc_now()
    found: dict[str, dict[str, Any]] = {}
    scanned = 0
    for profile in iter_bulk(path):
        scanned += 1
        org = profile["organisation_number"]
        if org not in wanted:
            continue
        raw = profile.pop("raw", {})
        profile["evidence"] = {
            "registry": evidence(
                "registry",
                "available",
                "official_registry_bulk",
                "https://data.brreg.no/enhetsregisteret/api/enheter/lastned/csv",
                value=raw,
                retrieved_at=retrieved_at,
                content_sha256=snapshot_sha256,
                source_row_key=org,
            ),
            "accounting_obligation": accounting_obligation_assessment(profile),
        }
        found[org] = profile
        if len(found) == len(wanted):
            break
    missing = [org for org in requested if org not in found]
    if missing:
        for org in missing:
            res = fetch_json(f"https://data.brreg.no/enhetsregisteret/api/enheter/{org}", timeout=5.0, attempts=2)
            if res.status == 200 and isinstance(res.body, dict):
                body = res.body
                found[org] = {
                    "organisation_number": org,
                    "name": body.get("navn") or f"Organisation {org}",
                    "legal_form": (body.get("organisasjonsform") or {}).get("kode"),
                    "employees": body.get("antallAnsatte"),
                    "bankrupt": bool(body.get("konkurs")),
                    "liquidating": bool(body.get("underAvvikling")),
                    "website": body.get("hjemmeside"),
                    "industry_code": (body.get("naeringskode1") or {}).get("kode"),
                    "industry_label": (body.get("naeringskode1") or {}).get("beskrivelse"),
                    "municipality": (body.get("forretningsadresse") or {}).get("kommune"),
                    "municipality_number": (body.get("forretningsadresse") or {}).get("kommunenummer"),
                    "address": body.get("forretningsadresse"),
                    "latest_submitted_accounts": None,
                    "sample_slice": "live_lookup",
                    "evaluation_split": "test",
                    "evidence": {
                        "registry": evidence(
                            "registry",
                            "available",
                            "official_registry_live",
                            res.url,
                            value=body,
                            retrieved_at=res.retrieved_at or retrieved_at,
                            content_sha256=res.content_sha256,
                            source_row_key=org,
                        ),
                        "accounting_obligation": accounting_obligation_assessment(body),
                    },
                }
            else:
                found[org] = {
                    "organisation_number": org,
                    "name": f"Organisation {org}",
                    "legal_form": None,
                    "employees": None,
                    "bankrupt": False,
                    "liquidating": False,
                    "website": None,
                    "industry_code": None,
                    "industry_label": None,
                    "municipality": None,
                    "municipality_number": None,
                    "address": None,
                    "latest_submitted_accounts": None,
                    "sample_slice": "unseen",
                    "evaluation_split": "test",
                    "evidence": {
                        "registry": evidence(
                            "registry",
                            "not_found",
                            "official_registry_bulk",
                            "https://data.brreg.no/enhetsregisteret/api/enheter/lastned/csv",
                            note="Organisation number absent from bulk registry snapshot and live registry",
                            retrieved_at=retrieved_at,
                            content_sha256=snapshot_sha256,
                            source_row_key=org,
                        ),
                        "accounting_obligation": evidence(
                            "accounting_obligation",
                            "not_applicable",
                            "official_rule_interpretation",
                            "https://www.brreg.no/",
                            note="Not found in registry bulk snapshot",
                            retrieved_at=retrieved_at,
                        ),
                    },
                }
    return [found[org] for org in requested], {
        "registry_snapshot_sha256": snapshot_sha256,
        "registry_rows_scanned": scanned,
        "requested": len(requested),
        "selected": len(found),
    }


def evidence_terminal_state(record: dict[str, Any] | None) -> str:
    if not record:
        return "submission_error"
    status = record.get("status")
    if status == "available":
        return "complete"
    if status == "not_applicable":
        return "not_applicable"
    if status == "not_found":
        return "not_found"
    if status == "blocked":
        note = str(record.get("note") or "").casefold()
        return "blocked_robots" if "robot" in note else "blocked_policy"
    if status == "budget_exhausted":
        return "budget_exhausted"
    if status == "source_error":
        note = str(record.get("note") or "").casefold()
        if "budget" in note:
            return "budget_exhausted"
        return "source_error"
    return "submission_error"


def _to_availability_state(status: str | None) -> str:
    if not status:
        return "failed"
    s = str(status).casefold()
    if s == "available":
        return "available"
    if s in {"not_found", "absent"}:
        return "not_available"
    if s == "blocked":
        return "blocked"
    if s == "not_applicable":
        return "not_applicable"
    if s == "ambiguous":
        return "ambiguous"
    return "failed"


def build_contract_claims_and_evidence(
    profile: dict[str, Any],
    completed_at: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    org = str(profile.get("organisation_number") or "")
    ev = profile.get("evidence") or {}

    evidence_items: list[dict[str, Any]] = []
    claims: list[dict[str, Any]] = []

    # 1. Collect evidence items
    for mod, rec in ev.items():
        if not isinstance(rec, dict):
            continue
        ev_id = f"ev-{mod}-{org}"
        evidence_items.append({
            "id": ev_id,
            "source_url": str(rec.get("source_url") or ""),
            "source_class": str(rec.get("source_class") or rec.get("source_type") or "official"),
            "retrieved_at": rec.get("retrieved_at") or completed_at,
            "content_sha256": str(rec.get("content_sha256") or ""),
            "claim_span": f"{mod} observation record for {profile.get('name', org)} ({org})",
        })

    # 2. Legal Identity Claim
    reg_val = (ev.get("registry_live") or {}).get("value") or (ev.get("registry") or {}).get("value") or {}
    name = profile.get("name") or reg_val.get("navn") or f"Organisation {org}"
    legal_form = profile.get("legal_form") or (reg_val.get("organisasjonsform") or {}).get("kode") or "AS"
    claims.append({
        "field": "legal_identity",
        "value": {"name": name, "legal_form": legal_form, "organisation_number": org},
        "availability": "available",
        "confidence": 1.0,
        "evidence_ids": [f"ev-registry-{org}"] if "registry" in ev else [f"ev-registry_live-{org}"],
    })

    # 3. Official Website Claim
    w_rec = ev.get("website") or {}
    w_val = w_rec.get("value") or {}
    w_status = w_rec.get("status")
    website_url = profile.get("website") or w_val.get("final_url")
    is_publishable = (w_val.get("identity_assessment") or {}).get("publishable", False)
    if w_status == "available" and is_publishable and website_url:
        claims.append({
            "field": "official_website",
            "value": website_url,
            "availability": "available",
            "confidence": 0.99,
            "evidence_ids": [f"ev-website-{org}"],
        })
    elif w_status == "blocked":
        claims.append({
            "field": "official_website",
            "value": None,
            "availability": "blocked",
            "confidence": 0.0,
            "evidence_ids": [f"ev-website-{org}"],
        })
    elif (w_status == "available" and not is_publishable) or w_status == "ambiguous":
        claims.append({
            "field": "official_website",
            "value": None,
            "availability": "ambiguous",
            "confidence": 0.0,
            "evidence_ids": [f"ev-website-{org}"],
        })
    else:
        claims.append({
            "field": "official_website",
            "value": None,
            "availability": "not_available",
            "confidence": 0.0,
            "evidence_ids": [f"ev-website-{org}"],
        })

    # 4. Annual Accounts Claim
    f_rec = ev.get("financials") or {}
    f_status = f_rec.get("status")
    f_val = f_rec.get("value") or {}
    records = f_val.get("records") or []
    if f_status == "available" and records:
        claims.append({
            "field": "annual_accounts",
            "value": records[0],
            "availability": "available",
            "confidence": 1.0,
            "evidence_ids": [f"ev-financials-{org}"],
        })
    else:
        claims.append({
            "field": "annual_accounts",
            "value": None,
            "availability": _to_availability_state(f_status),
            "confidence": 0.0,
            "evidence_ids": [f"ev-financials-{org}"],
        })

    # 5. Management & Board Roles Claim
    r_rec = ev.get("roles") or {}
    r_status = r_rec.get("status")
    r_val = r_rec.get("value") or {}
    roles_list = r_val.get("roles") or []
    if r_status == "available" and roles_list:
        claims.append({
            "field": "roles",
            "value": roles_list,
            "availability": "available",
            "confidence": 1.0,
            "evidence_ids": [f"ev-roles-{org}"],
        })
    else:
        claims.append({
            "field": "roles",
            "value": None,
            "availability": _to_availability_state(r_status),
            "confidence": 0.0,
            "evidence_ids": [f"ev-roles-{org}"],
        })

    # 6. Operating Locations (Subunits) Claim
    l_rec = ev.get("locations") or {}
    l_status = l_rec.get("status")
    l_val = l_rec.get("value") or {}
    locs_list = l_val.get("locations") or []
    if l_status == "available" and locs_list:
        claims.append({
            "field": "operating_locations",
            "value": locs_list,
            "availability": "available",
            "confidence": 1.0,
            "evidence_ids": [f"ev-locations-{org}"],
        })
    else:
        claims.append({
            "field": "operating_locations",
            "value": None,
            "availability": _to_availability_state(l_status),
            "confidence": 0.0,
            "evidence_ids": [f"ev-locations-{org}"],
        })

    # 7. Hiring / Recruitment Status Claim
    hiring = profile.get("hiring") or {}
    if hiring:
        claims.append({
            "field": "hiring_status",
            "value": {
                "appears_to_be_hiring": hiring.get("appears_to_be_hiring", False),
                "status": hiring.get("status"),
                "active_job_postings_count": hiring.get("active_job_postings_count", 0),
            },
            "availability": "available",
            "confidence": 0.95 if hiring.get("confidence") == "high" else 0.80,
            "evidence_ids": [f"ev-hiring-{org}"],
        })
    else:
        claims.append({
            "field": "hiring_status",
            "value": None,
            "availability": "not_available",
            "confidence": 0.0,
            "evidence_ids": [],
        })

    # 8. Accounting Obligation Claim
    a_rec = ev.get("accounting_obligation") or {}
    a_status = a_rec.get("status")
    a_val = a_rec.get("value") or {}
    claims.append({
        "field": "accounting_obligation",
        "value": a_val.get("classification"),
        "availability": _to_availability_state(a_status),
        "confidence": 1.0,
        "evidence_ids": [f"ev-accounting_obligation-{org}"],
    })

    return claims, evidence_items


def terminal_envelope(
    profile: dict[str, Any],
    *,
    run_id: str,
    modules: Iterable[str],
    started_at: str,
    completed_at: str,
) -> dict[str, Any]:
    org = str(profile.get("organisation_number") or "")
    module_states = {}
    for module in modules:
        record = profile.get("evidence", {}).get(module)
        module_states[module] = {
            "state": evidence_terminal_state(record),
            "retry_count": int((record or {}).get("retry_count") or 0),
            "final_timestamp": (record or {}).get("retrieved_at") or completed_at,
        }
    entity_state = "submission_error" if any(item["state"] == "submission_error" for item in module_states.values()) else "complete"

    # Canonical company-level result state according to Builderr specification
    reg_status = (profile.get("evidence", {}).get("registry") or {}).get("status")
    reg_live_status = (profile.get("evidence", {}).get("registry_live") or {}).get("status")
    reg_module_state = module_states.get("registry", {}).get("state")

    if (
        entity_state == "submission_error"
        or reg_status in {"submission_error", "source_error"}
        or reg_live_status in {"submission_error", "source_error"}
        or reg_module_state in {"submission_error", "source_error"}
        or any(s.get("state") == "submission_error" for s in module_states.values())
    ):
        company_availability = "failed"
        entity_state = "submission_error"
    elif reg_status == "not_found" and reg_live_status in {None, "not_found", "absent"}:
        company_availability = "not_available"
    elif reg_status == "blocked" or reg_live_status == "blocked":
        company_availability = "blocked"
    elif reg_status == "not_applicable":
        company_availability = "not_applicable"
    elif reg_status == "ambiguous":
        company_availability = "ambiguous"
    elif profile.get("name") and not str(profile.get("name")).startswith("Organisation "):
        company_availability = "available"
    elif reg_status == "available" or reg_live_status == "available":
        company_availability = "available"
    else:
        company_availability = "not_available"

    claims, evidence_list = build_contract_claims_and_evidence(profile, completed_at)
    run_metrics = profile.get("run_metrics") or {}

    return {
        "run_id": run_id,
        "organisation_number": org,
        "availability": company_availability,
        "state": entity_state,
        "started_at": started_at,
        "completed_at": completed_at,
        "modules": module_states,
        "run": {
            "run_id": run_id,
            "started_at": started_at,
            "completed_at": completed_at,
            "terminal_status": "completed" if entity_state == "complete" and company_availability != "failed" else "failed",
            "availability": company_availability,
        },
        "legal_identity": {
            "organisation_number": org,
            "name": profile.get("name"),
            "legal_form": profile.get("legal_form"),
            "municipality": profile.get("municipality"),
        },
        "claims": claims,
        "evidence": evidence_list,
        "changes": [],
        "errors": [],
        "operations": {
            "requests": int(run_metrics.get("requests") or 0),
            "runtime_ms": int((run_metrics.get("elapsed_s") or 0) * 1000),
            "third_party_cost_usd": 0.0,
        },
        "profile": profile,
    }


def validate_envelopes(envelopes: list[dict[str, Any]], expected_count: int) -> dict[str, Any]:
    orgs = [item.get("organisation_number") for item in envelopes]
    invalid_states = [
        {"organisation_number": item.get("organisation_number"), "state": state.get("state")}
        for item in envelopes
        for state in item.get("modules", {}).values()
        if state.get("state") not in TERMINAL_STATES
    ]
    checks = {
        "exact_expected_count": len(envelopes) == expected_count,
        "unique_organisation_numbers": len(orgs) == len(set(orgs)),
        "all_entity_states_terminal": all(item.get("state") in TERMINAL_STATES for item in envelopes),
        "all_module_states_terminal": not invalid_states,
        "zero_silent_drops": len(envelopes) == expected_count and len(orgs) == len(set(orgs)),
    }
    return {"passed": all(checks.values()), "checks": checks, "invalid_states": invalid_states}


def profile_complete_for_modules(profile: dict[str, Any], modules: Iterable[str]) -> bool:
    records = profile.get("evidence", {})
    return all(module in records and records[module].get("status") != "not_fetched" for module in modules)
