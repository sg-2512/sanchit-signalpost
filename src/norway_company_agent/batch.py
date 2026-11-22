from __future__ import annotations

import hashlib
import json
import re
import urllib.parse
from pathlib import Path
from typing import Any, Callable, Iterable

from .evidence import evidence, utc_now
from .http import FetchResult, fetch_json
from .official import accounting_obligation_assessment, compare_annual_accounts, latest_account, fiscal_year_of, normalize_retained_accounts, registry_company_status
from .identity import published_website_url
from .verified_items import first_party_news_about_company, name_in_text, registered_domain, validate_job_item, validate_news_item
from .sampling import iter_bulk


TERMINAL_STATES = {"available", "not_available", "blocked", "not_applicable", "ambiguous", "failed"}


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
            raise ValueError(f"Duplicate organisation number in input: {clean_org}")
        seen_orgs.add(clean_org)
        record = {"organisation_number": clean_org}
        if isinstance(value, dict):
            for key in ("evaluation_split", "sample_slice", "source_leads"):
                if value.get(key) is not None:
                    record[key] = value[key]
        records.append(record)
    return records


def read_organisation_numbers(path: str | Path) -> list[str]:
    return [record["organisation_number"] for record in read_organisation_inputs(path)]


def profiles_from_bulk(
    path: str | Path | None,
    organisation_numbers: Iterable[str],
    *,
    fetcher: Callable[..., FetchResult] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    from concurrent.futures import ThreadPoolExecutor

    requested = list(organisation_numbers)
    wanted = set(requested)
    retrieved_at = utc_now()
    found: dict[str, dict[str, Any]] = {}
    scanned = 0
    bulk_path = Path(path) if path else None
    if bulk_path and bulk_path.exists():
        hasher = hashlib.sha256()
        with open(bulk_path, "rb") as f:
            while chunk := f.read(1_048_576):
                hasher.update(chunk)
        snapshot_sha256 = hasher.hexdigest()
        for profile in iter_bulk(bulk_path):
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
    else:
        snapshot_sha256 = hashlib.sha256(b"live_registry_only").hexdigest()

    missing = [org for org in requested if org not in found]
    if missing:
        def _fetch_missing(org: str) -> tuple[str, dict[str, Any]]:
            res = (fetcher or fetch_json)(f"https://data.brreg.no/enhetsregisteret/api/enheter/{org}", timeout=5.0, attempts=2)
            if res.status == 200 and isinstance(res.body, dict) and str(res.body.get("organisasjonsnummer")) == org:
                body = res.body
                reg_ev_obj = evidence(
                    "registry",
                    "available",
                    "official_registry_live",
                    res.url,
                    value=body,
                    retrieved_at=res.retrieved_at or retrieved_at,
                    content_sha256=res.content_sha256,
                    source_row_key=org,
                )
                return org, {
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
                    "latest_submitted_accounts": str(body.get("sisteInnsendteAarsregnskap") or "") or None,
                    "sample_slice": "live_lookup",
                    "evaluation_split": "test",
                    "evidence": {
                        "registry": reg_ev_obj,
                        "accounting_obligation": accounting_obligation_assessment({**body, "evidence": {"registry": reg_ev_obj}}),
                    },
                }
            status = "not_found" if res.status in {404, 410} else "blocked" if res.status in {401, 403, 429} else "source_error"
            note = res.error or ("Registry response did not verify the requested organisation number" if res.status == 200 else f"HTTP {res.status}")
            return org, {
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
                        status,
                        "official_registry_live",
                        res.url,
                        note=note,
                        retrieved_at=res.retrieved_at or retrieved_at,
                        http_status=res.status or None,
                    ),
                    "accounting_obligation": evidence(
                        "accounting_obligation",
                        "not_applicable" if status == "not_found" else status,
                        "official_rule_interpretation",
                        "https://www.brreg.no/",
                        note="Registry identity was unavailable",
                        retrieved_at=retrieved_at,
                    ),
                },
            }

        with ThreadPoolExecutor(max_workers=min(16, max(1, len(missing)))) as pool:
            for org, prof in pool.map(_fetch_missing, missing):
                found[org] = prof

    return [found[org] for org in requested], {
        "registry_snapshot_sha256": snapshot_sha256,
        "registry_rows_scanned": scanned,
        "requested": len(requested),
        "selected": len(found),
    }


def evidence_terminal_state(record: dict[str, Any] | None) -> str:
    if not record:
        return "failed"
    status = record.get("status")
    field = record.get("field")
    if status == "available":
        return "available"
    if status == "not_applicable":
        return "not_applicable"
    if status in {"not_found", "absent", "not_available"}:
        return "not_available"
    if status == "blocked":
        return "blocked"
    if status == "source_error":
        # A failed request cannot establish absence, including for optional
        # external modules and group or subunit lookups.
        return "failed"
    if status == "budget_exhausted":
        return "failed"
    return "failed"


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


_MONTH_MAP = {
    "januar": "01", "january": "01", "jan": "01",
    "februar": "02", "february": "02", "feb": "02",
    "mars": "03", "march": "03", "mar": "03",
    "april": "04", "apr": "04",
    "mai": "05", "may": "05",
    "juni": "06", "june": "06", "jun": "06",
    "juli": "07", "july": "07", "jul": "07",
    "august": "08", "aug": "08",
    "september": "09", "sep": "09", "sept": "09",
    "oktober": "10", "october": "10", "okt": "10", "oct": "10",
    "november": "11", "nov": "11",
    "desember": "12", "december": "12", "des": "12", "dec": "12",
}


def _normalize_date_str(val: Any) -> str | None:
    if not val:
        return None
    import re as _re
    s = str(val).strip()
    m = _re.search(r"\b(20\d{2})[-/](\d{1,2})[-/](\d{1,2})(?:T|\b|\s|$)", s)
    if m:
        return f"{m.group(1)}-{m.group(2).zfill(2)}-{m.group(3).zfill(2)}"
    m = _re.search(r"\b(\d{1,2})\.(\d{1,2})\.(20\d{2})\b", s)
    if m:
        return f"{m.group(3)}-{m.group(2).zfill(2)}-{m.group(1).zfill(2)}"
    m = _re.search(r"\b(\d{1,2})\.?\s+([a-zA-ZæøåÆØÅ]+)\s+(20\d{2})\b", s)
    if m:
        mon = _MONTH_MAP.get(m.group(2).lower())
        if mon:
            return f"{m.group(3)}-{mon}-{m.group(1).zfill(2)}"
    return None


def build_contract_claims_and_evidence(
    profile: dict[str, Any],
    completed_at: str,
    *,
    first_party_activity_only: bool = False,
    require_legal_operator: bool = False,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    from .identity import apply_entity_identity_gate

    org = str(profile.get("organisation_number") or "")
    ev = profile.get("evidence") or {}

    # Re-apply multi-factor entity identity gate when real crawl content is present so cached profiles stay current
    w_rec = ev.get("website") or {}
    w_val = w_rec.get("value") or {}
    if w_rec.get("status") == "available" and require_legal_operator:
        gated = apply_entity_identity_gate(profile, w_rec, require_legal_operator=True)
        w_rec = gated["website"]
        w_val = w_rec.get("value") or {}
    elif (w_rec.get("status") == "available" and isinstance(w_val, dict)
          and ((w_val.get("identity_assessment") or {}).get("publishable")
               or (w_val.get("entity_identity_assessment") or {}).get("operator_conflict"))
          and (w_val.get("title") or w_val.get("main_text_excerpt") or w_val.get("final_url"))):
        gated = apply_entity_identity_gate(profile, w_rec)
        w_rec = gated["website"]
        w_val = w_rec.get("value") or {}

    reg_live_rec = ev.get("registry_live") or {}
    reg_bulk_rec = ev.get("registry") or {}

    # Ensure accounting_obligation evidence resolves to the retained official registry response body
    a_rec = ev.get("accounting_obligation") or {}
    if isinstance(a_rec, dict) and a_rec.get("status") == "available":
        official_reg_sha = reg_live_rec.get("content_sha256") or reg_bulk_rec.get("content_sha256")
        official_reg_url = reg_live_rec.get("source_url") or reg_bulk_rec.get("source_url")
        if official_reg_sha:
            a_rec["content_sha256"] = official_reg_sha
        if official_reg_url:
            a_rec["source_url"] = official_reg_url

    evidence_items: list[dict[str, Any]] = []
    claims: list[dict[str, Any]] = []

    # 1. Collect evidence items
    for mod, rec in ev.items():
        if not isinstance(rec, dict):
            continue
        ev_id = f"ev-{mod}-{org}"
        raw_sha = str(rec.get("content_sha256") or "")
        src_url = str(rec.get("source_url") or "")
        if not re.fullmatch(r"[0-9a-fA-F]{64}", raw_sha):
            # An absent or malformed digest cannot prove the source content.
            continue
        item = {
            "id": ev_id,
            "source_url": src_url,
            "source_class": str(rec.get("source_class") or rec.get("source_type") or "official"),
            "retrieved_at": rec.get("retrieved_at") or completed_at,
            "content_sha256": raw_sha,
        }
        if rec.get("source_fields"):
            item["source_fields"] = rec["source_fields"]
        if mod == "registered_address" and rec.get("content_sha256") == reg_live_rec.get("content_sha256"):
            raw_address = (reg_live_rec.get("value") or {}).get("forretningsadresse")
            if raw_address:
                item["source_fields"] = [{"source_pointer": "/forretningsadresse", "source_value": raw_address}]
        if rec.get("source_row_key"):
            item["source_record_key"] = rec["source_row_key"]
        if rec.get("literal_excerpt"):
            item["literal_excerpt"] = rec["literal_excerpt"]
        if mod == "website" and isinstance(rec.get("value"), dict):
            value = rec["value"]
            identity = " ".join(str(value.get("identity_text_excerpt") or "").split())
            visible = " ".join(str(value.get("visible_text_excerpt") or "").split())
            if identity and identity in visible:
                item["literal_excerpt"] = identity[:500]
        evidence_items.append(item)

    # 2. Legal Identity Claim (prefers live sovereign registry response body with exact SHA-256)
    if reg_live_rec.get("status") == "available" and reg_live_rec.get("content_sha256"):
        reg_val = reg_live_rec.get("value") or {}
        name = reg_val.get("name") or reg_val.get("navn") or profile.get("name") or f"Organisation {org}"
        legal_form = reg_val.get("legal_form") or (reg_val.get("organisasjonsform") or {}).get("kode") or profile.get("legal_form") or "AS"
        claims.append({
            "field": "legal_identity",
            "value": {"name": name, "legal_form": legal_form, "organisation_number": org},
            "availability": "available",
            "confidence": 1.0,
            "evidence_ids": [f"ev-registry_live-{org}"],
        })
    elif reg_bulk_rec.get("status") == "available" and reg_bulk_rec.get("content_sha256"):
        reg_val = reg_bulk_rec.get("value") or {}
        name = profile.get("name") or reg_val.get("navn") or f"Organisation {org}"
        legal_form = profile.get("legal_form") or (reg_val.get("organisasjonsform") or {}).get("kode") or "AS"
        claims.append({
            "field": "legal_identity",
            "value": {"name": name, "legal_form": legal_form, "organisation_number": org},
            "availability": "available",
            "confidence": 1.0,
            "evidence_ids": [f"ev-registry-{org}"],
        })
    else:
        claims.append({
            "field": "legal_identity",
            "value": None,
            "availability": _to_availability_state(reg_live_rec.get("status") or reg_bulk_rec.get("status")),
            "confidence": 0.0,
            "evidence_ids": [f"ev-registry_live-{org}"] if "registry_live" in ev else ([f"ev-registry-{org}"] if "registry" in ev else []),
        })

    if reg_live_rec.get("status") == "available" and reg_live_rec.get("content_sha256"):
        live_value = reg_live_rec.get("value") or {}
        if str(live_value.get("organisasjonsnummer") or live_value.get("organisation_number")) == org:
            status, status_fields = registry_company_status(live_value, completed_at)
            registry_evidence = next((item for item in evidence_items if item["id"] == f"ev-registry_live-{org}"), None)
            if status and registry_evidence:
                status_id = f"ev-company-status-{org}"
                evidence_items.append({**registry_evidence, "id": status_id, "source_fields": [
                    {"source_pointer": f"/{field}", "source_value": live_value[field]} for field in status_fields
                ]})
                claims.append({"field": "company_status", "value": status, "availability": "available",
                               "confidence": 1.0, "evidence_ids": [status_id]})
        industry = live_value.get("naeringskode1") or live_value.get("industry") or {}
        if isinstance(industry, dict) and industry.get("beskrivelse"):
            claims.append({"field": "registered_industry", "value": {"code": industry.get("kode"),
                           "description": industry["beskrivelse"]}, "availability": "available",
                           "confidence": 1.0, "evidence_ids": [f"ev-registry_live-{org}"]})
        raw_purpose = live_value.get("aktivitet") or live_value.get("vedtektsfestetFormaal")
        purpose = " ".join(str(part).strip() for part in raw_purpose if part) if isinstance(raw_purpose, list) else str(raw_purpose or "").strip()
        if purpose:
            claims.append({"field": "registered_business_purpose", "value": purpose,
                           "availability": "available", "confidence": 1.0,
                           "evidence_ids": [f"ev-registry_live-{org}"]})
        current_name = str(live_value.get("name") or live_value.get("navn") or profile.get("name") or "").strip()
        history = []
        for entry in live_value.get("historiskeNavn") or []:
            if not isinstance(entry, dict):
                continue
            old_name = str(entry.get("navn") or "").strip()
            if old_name and old_name != current_name:
                history.append({"name": old_name, "from": entry.get("fraDato"), "until": entry.get("tilDato")})
        if history:
            claims.append({"field": "legal_name_history", "value": history,
                           "availability": "available", "confidence": 1.0,
                           "evidence_ids": [f"ev-registry_live-{org}"]})
        registry_events = [{"type": event, "date": date_value} for key, event in (
            ("stiftelsesdato", "company_formed"),
            ("registreringsdatoEnhetsregisteret", "entity_register_registration"),
            ("registreringsdatoForetaksregisteret", "business_register_registration"),
        ) if isinstance((date_value := live_value.get(key)), str)
            and re.fullmatch(r"\d{4}-\d{2}-\d{2}", date_value) and date_value <= completed_at[:10]]
        if registry_events and reg_live_rec.get("source_type") == "official_registry_live":
            claims.append({"field": "registry_events", "value": registry_events,
                           "availability": "available", "confidence": 1.0,
                           "evidence_ids": [f"ev-registry_live-{org}"]})

    # The registry's declared address is a registry fact, independent of crawl verification.
    registry_module = "registry_live" if reg_live_rec.get("status") == "available" else "registry"
    registry_rec = ev.get(registry_module) or {}
    registry_value = registry_rec.get("value") or {}
    registry_org = str(registry_value.get("organisasjonsnummer") or registry_value.get("organisation_number") or "")
    declared = str(registry_value.get("hjemmeside") or registry_value.get("website") or "").strip()
    registry_id = f"ev-{registry_module}-{org}"
    registry_evidence = next((item for item in evidence_items if item["id"] == registry_id), None)
    if registry_org == org and declared and registry_rec.get("status") == "available" and registry_evidence:
        declared_id = f"ev-registry-declared-website-{org}"
        if registry_module == "registry_live" and registry_value.get("hjemmeside"):
            evidence_items.append({**registry_evidence, "id": declared_id, "source_fields": [{
                "source_pointer": "/hjemmeside", "source_value": registry_value["hjemmeside"],
            }]})
        else:
            evidence_items.append({**registry_evidence, "id": declared_id, "source_record_key": org})
        claims.append({"field": "registry_declared_website", "value": declared,
                       "availability": "available", "confidence": 1.0, "evidence_ids": [declared_id]})
        if require_legal_operator and not published_website_url(w_rec):
            from .connectors.scoped_company_sources import assess_group_site_presence

            for page in w_val.get("pages") or []:
                presence = assess_group_site_presence(page, declared_url=declared, org=org,
                                                      company=str(registry_value.get("navn") or registry_value.get("name") or ""))
                if not presence:
                    continue
                group_site_id = f"ev-group-company-page-{org}"
                evidence_items.append({"id": group_site_id,
                    "retrieved_at": presence.get("retrieved_at") or w_rec.get("retrieved_at") or completed_at,
                    **{key: presence[key] for key in (
                    "source_url", "content_sha256", "source_class", "literal_excerpt",
                )}})
                claims.append({"field": "group_website_company_page", "value": presence["value"],
                               "availability": "available", "confidence": 1.0,
                               "evidence_ids": [declared_id, group_site_id]})
                break

    presence = ev.get("norwegian_website_presence") or {}
    if presence:
        status = presence.get("status")
        claims.append({"field": "norwegian_website_presence", "value": presence.get("value"),
                       "availability": _to_availability_state(status), "confidence": 1.0 if status == "available" else 0.0,
                       "evidence_ids": [f"ev-norwegian_website_presence-{org}"]})

    group = ev.get("group") or {}
    relationships = group.get("value") or {}
    group_id = f"ev-group-{org}"
    if (group.get("status") == "available" and relationships.get("organisation_number") == org
            and (relationships.get("parent") or relationships.get("subsidiaries"))
            and any(item["id"] == group_id and item.get("source_fields") for item in evidence_items)):
        claims.append({"field": "group_membership", "value": relationships, "availability": "available",
                       "confidence": 1.0, "evidence_ids": [group_id]})

    # 3. Official Website Claim
    w_status = w_rec.get("status")
    raw_web_url = str(w_val.get("final_url") or w_rec.get("source_url") or profile.get("website") or "").strip()
    if raw_web_url and not (raw_web_url.startswith("http://") or raw_web_url.startswith("https://")):
        raw_web_url = f"https://{raw_web_url}"
    website_url = raw_web_url or None
    is_publishable = bool((w_val.get("identity_assessment") or {}).get("publishable", False))
    site_ev_id = f"ev-website-{org}"
    verified_url = published_website_url(w_rec, website_url)
    has_valid_site_ev = bool(verified_url)
    if declared and registry_evidence and w_rec.get("requested_url") and w_status == "available":
        initial = str(w_rec["requested_url"])
        final = str(w_val.get("final_url") or "")
        try:
            initial_domain = registered_domain(initial if "://" in initial else "https://" + initial)
            declared_domain = registered_domain(declared if "://" in declared else "https://" + declared)
            final_domain = registered_domain(final)
        except ValueError:
            initial_domain = declared_domain = final_domain = ""
        if initial_domain and initial_domain == declared_domain and final_domain and final_domain != declared_domain:
            claims.append({"field": "registry_website_redirect", "value": {"declared_url": declared, "destination_url": final,
                           "relationship": "legacy_redirect"}, "availability": "available",
                           "confidence": 1.0, "evidence_ids": [registry_id, site_ev_id]})
    if has_valid_site_ev:
        website_url = verified_url
        site_evidence_ids = [site_ev_id]
        operator_proof = w_val.get("operator_proof") or {}
        if (operator_proof.get("literal_excerpt") and operator_proof.get("source_url")
                and re.fullmatch(r"[a-f0-9]{64}", str(operator_proof.get("content_sha256") or ""))):
            operator_id = f"ev-website-legal-operator-{org}"
            evidence_items.append({**operator_proof, "id": operator_id, "source_class": "company_site",
                                   "retrieved_at": w_rec.get("retrieved_at") or completed_at})
            site_evidence_ids.append(operator_id)
        description = " ".join(str(w_val.get("description") or "").split())
        if 30 <= len(description) <= 500:
            claims.append({"field": "website_description", "value": description, "availability": "available",
                           "confidence": 0.95, "evidence_ids": site_evidence_ids})
        claims.append({
            "field": "official_website",
            "value": website_url,
            "relationship": w_val.get("relationship_label") or "verified_company_site",
            "availability": "available",
            "confidence": 0.99,
            "evidence_ids": site_evidence_ids,
        })
        claims.append({
            "field": "website",
            "value": website_url,
            "availability": "available",
            "confidence": 0.99,
            "evidence_ids": site_evidence_ids,
        })
    elif w_status == "blocked":
        claims.append({
            "field": "official_website",
            "value": None,
            "availability": "blocked",
            "confidence": 0.0,
            "evidence_ids": [site_ev_id],
        })
        claims.append({
            "field": "website",
            "value": None,
            "availability": "blocked",
            "confidence": 0.0,
            "evidence_ids": [site_ev_id],
        })
    elif (w_status == "available" and not is_publishable) or w_status == "ambiguous":
        website_evidence_ids = [site_ev_id]
        conflict = (w_val.get("entity_identity_assessment") or {}).get("operator_conflict") or {}
        conflict_sha = str(conflict.get("content_sha256") or "")
        if conflict.get("source_url") and conflict.get("claim_span") and re.fullmatch(r"[0-9a-fA-F]{64}", conflict_sha):
            conflict_id = f"ev-website-operator-conflict-{org}"
            evidence_items.append({"id": conflict_id, "source_url": conflict["source_url"],
                                   "source_class": "company_site", "content_sha256": conflict_sha,
                                   "retrieved_at": w_rec.get("retrieved_at") or completed_at,
                                   "literal_excerpt": conflict["claim_span"]})
            website_evidence_ids.append(conflict_id)
        claims.append({
            "field": "official_website",
            "value": None,
            "availability": "ambiguous",
            "confidence": 0.0,
            "evidence_ids": website_evidence_ids,
        })
        claims.append({
            "field": "website",
            "value": None,
            "availability": "ambiguous",
            "confidence": 0.0,
            "evidence_ids": website_evidence_ids,
        })
    else:
        claims.append({
            "field": "official_website",
            "value": None,
            "availability": "not_available",
            "confidence": 0.0,
            "evidence_ids": [site_ev_id],
        })
        claims.append({
            "field": "website",
            "value": None,
            "availability": "not_available",
            "confidence": 0.0,
            "evidence_ids": [site_ev_id],
        })

    workforce = []
    workforce_ids = []
    registered_count = registry_value.get("antallAnsatte")
    if registered_count is None:
        registered_count = registry_value.get("employees")
    if registry_org == org and registry_evidence and isinstance(registered_count, int) and registered_count >= 0:
        workforce.append({"source": "BRREG", "employees": registered_count, "source_url": registry_evidence["source_url"],
                          "retrieved_at": registry_evidence["retrieved_at"],
                          "effective_at": registry_value.get("registreringsdatoAntallAnsatteEnhetsregisteret")})
        workforce_ids.append(registry_id)
    if has_valid_site_ev:
        workforce_pattern = re.compile(r"\b(?:vi er(?: nå)?|vi har(?: nå)?|we (?:are|have)(?: now)?)\s+(\d{1,6})\s+(?:ansatte|employees)\b", re.I)
        site_host = (urllib.parse.urlsplit(website_url).hostname or "").removeprefix("www.")
        for page in w_val.get("pages") or []:
            url = page.get("url")
            digest = str(page.get("content_sha256") or "")
            if (not url or (urllib.parse.urlsplit(url).hostname or "").removeprefix("www.") != site_host
                    or not re.fullmatch(r"[a-fA-F0-9]{64}", digest)):
                continue
            match = workforce_pattern.search(str(page.get("main_text_excerpt") or page.get("visible_text_excerpt") or ""))
            if match:
                observed_at = page.get("retrieved_at") or w_rec.get("retrieved_at") or completed_at
                evidence_id = f"ev-website-workforce-{org}"
                evidence_items.append({"id": evidence_id, "source_url": url, "content_sha256": digest,
                                       "source_class": "company_site", "retrieved_at": observed_at,
                                       "literal_excerpt": match[0]})
                workforce.append({"source": "Company website", "employees": int(match[1]), "source_url": url,
                                  "retrieved_at": observed_at, "effective_at": None})
                workforce_ids.append(evidence_id)
                break
    profile["workforce_source_values"] = workforce
    if workforce:
        claims.append({"field": "workforce_source_counts", "value": workforce, "availability": "available",
                       "confidence": 0.95, "evidence_ids": workforce_ids})

    # 3b. Company-Owned Social Profiles Claim
    site_quarantined = bool(w_status == "available" and not is_publishable)
    raw_social: list[dict[str, Any]] = []
    if is_publishable and w_val.get("social_links"):
        raw_social.extend(w_val.get("social_links") or [])
    if profile.get("social_profiles"):
        for item in profile.get("social_profiles") or []:
            if is_publishable or (not site_quarantined and isinstance(item, dict) and item.get("content_sha256")):
                raw_social.append(item)
    soc_ev_items = ((ev.get("social_profiles") or {}).get("value") or {}).get("items") or []
    if soc_ev_items:
        for item in soc_ev_items:
            if is_publishable or (not site_quarantined and isinstance(item, dict) and item.get("content_sha256")):
                raw_social.append(item)

    social_links: list[dict[str, str]] = []
    seen_social: set[tuple[str, str]] = set()
    primary_social_sha: str | None = None
    primary_social_src: str | None = None
    primary_social_span: str | None = None
    for s in raw_social:
        if not isinstance(s, dict):
            continue
        plat = str(s.get("platform") or "").strip().lower()
        s_url = str(s.get("url") or s.get("source_url") or "").strip()
        if not plat or not (s_url.startswith("http://") or s_url.startswith("https://")):
            continue
        parsed_social = urllib.parse.urlsplit(s_url)
        social_host = (parsed_social.hostname or "").casefold().removeprefix("www.")
        key = (plat, social_host + parsed_social.path.casefold().rstrip("/"))
        if key not in seen_social:
            seen_social.add(key)
            social_links.append({"platform": plat, "url": s_url,
                                 "relationship_scope": "company_site_linked_brand_channel" if is_publishable else "source_linked_channel"})
            if not primary_social_sha and s.get("content_sha256"):
                primary_social_sha = str(s.get("content_sha256"))
                primary_social_src = str(s.get("source_url") or s_url)
            if not primary_social_span and s.get("evidence_span"):
                primary_social_span = str(s.get("evidence_span"))

    if social_links and (has_valid_site_ev or primary_social_sha):
        social_ev_id = f"ev-social-{org}"
        soc_ev_ids = [site_ev_id, social_ev_id] if has_valid_site_ev else [social_ev_id]
        claims.append({
            "field": "social_profiles",
            "value": social_links,
            "availability": "available",
            "confidence": 0.95,
            "evidence_ids": soc_ev_ids,
        })
        primary_soc = social_links[0].get("url") if social_links else None
        for s in social_links:
            if s.get("platform") == "linkedin":
                primary_soc = s.get("url")
                break
        claims.append({
            "field": "social_profile",
            "value": primary_soc,
            "availability": "available",
            "confidence": 0.95,
            "evidence_ids": soc_ev_ids,
        })
        if not any(item["id"] == social_ev_id for item in evidence_items):
            social_sha = str(w_rec.get("content_sha256") or primary_social_sha or "")
            social_src = str(w_rec.get("source_url") or website_url or primary_social_src or primary_soc or "")
            evidence_items.append({
                "id": social_ev_id,
                "source_url": social_src,
                "source_class": "company_owned",
                "retrieved_at": w_rec.get("retrieved_at") or completed_at,
                "content_sha256": social_sha,
                "claim_span": primary_social_span or f"Verified company-owned social profiles: {', '.join(s.get('platform', '') for s in social_links)}",
            })
    else:
        has_site_ev = any(item["id"] == site_ev_id for item in evidence_items)
        claims.append({
            "field": "social_profiles",
            "value": None,
            "availability": "not_available",
            "confidence": 0.0,
            "evidence_ids": [site_ev_id] if has_site_ev else [],
        })
        claims.append({
            "field": "social_profile",
            "value": None,
            "availability": "not_available",
            "confidence": 0.0,
            "evidence_ids": [site_ev_id] if has_site_ev else [],
        })

    # 4. Annual Accounts Claim
    f_rec = ev.get("financials") or {}
    f_status = f_rec.get("status")
    f_val = f_rec.get("value") or {}
    records = normalize_retained_accounts(f_rec) if f_status == "available" else []
    profile["latest_account_year"] = fiscal_year_of(latest_account(records))
    if f_status == "available" and records and f_rec.get("content_sha256"):
        for record in records:
            record.pop("wage_per_employee", None)
        (f_val.get("financial_trends") or {}).pop("wage_per_employee", None)
        latest_rec = latest_account(records)
        if latest_rec.get("balance_sheet_issues"):
            trends = f_val.get("financial_trends") or {}
            for metric in ("equity_ratio", "liquidity_ratio", "current_ratio", "debt_to_equity", "solvency_status"):
                if latest_rec.get(metric) is None:
                    trends.pop(metric, None)
        statutory_values = {key: value for key, value in latest_rec.items() if key not in {"solvency_status", "derived_assessment"}}
        assessment = latest_rec.get("derived_assessment")
        claims.append({
            "field": "annual_accounts",
            "value": {**statutory_values, "fiscal_year": fiscal_year_of(latest_rec)},
            "availability": "available",
            "confidence": 1.0,
            "evidence_ids": [f"ev-financials-{org}"],
        })
        if comparison := compare_annual_accounts(records):
            claims.append({"field": "annual_accounts_comparison", "value": comparison, "availability": "available",
                           "confidence": 1.0, "evidence_ids": [f"ev-financials-{org}"]})
        if assessment:
            claims.append({"field": "financial_assessment", "value": assessment, "availability": "available",
                           "derivation": {"input_evidence_ids": [f"ev-financials-{org}"], "kind": "derived"},
                           "confidence": 1.0, "evidence_ids": [f"ev-financials-{org}"]})
    else:
        filing = (reg_live_rec.get("value") or {}).get("latest_submitted_accounts")
        if f_status in {"not_found", "absent"}:
            if filing:
                account_note = f"BRREG lists accounts through {filing}. The accounts endpoint returned no record."
            elif reg_live_rec.get("status") == "available":
                account_note = "The accounts endpoint returned no record. BRREG did not list a filing year."
                founded = str((reg_live_rec.get("value") or {}).get("stiftelsesdato") or "")
                if re.fullmatch(r"\d{4}-\d{2}-\d{2}", founded) and founded[:4] == completed_at[:4]:
                    account_note = "BRREG records incorporation this year. The company did not exist in the prior calendar year."
                    claims.append({"field": "annual_accounts_status", "value": "not_yet_expected",
                                   "availability": "available", "confidence": 1.0,
                                   "evidence_ids": [f"ev-registry_live-{org}"]})
            else:
                account_note = "The accounts endpoint returned no record. The live filing status is unknown."
        else:
            account_note = str(f_rec.get("note") or "Usable annual accounts were not retrieved.")
        claims.append({
            "field": "annual_accounts",
            "value": None,
            "availability": "not_available" if f_status in {"available", "not_found", "absent", None} else _to_availability_state(f_status),
            "confidence": 0.0,
            "reason": account_note,
            "evidence_ids": [f"ev-financials-{org}"] if "financials" in ev else [],
        })

    approval = ev.get("construction_approval") or {}
    approval_value = approval.get("value") or {}
    if approval.get("status") == "available" and approval_value.get("organisation_number") == org:
        claims.append({"field": "construction_approval", "value": approval_value,
                       "availability": "available", "confidence": 1.0,
                       "evidence_ids": [f"ev-construction_approval-{org}"]})

    # 5. Management & Board Roles Claim
    r_rec = ev.get("roles") or {}
    r_status = r_rec.get("status")
    r_val = r_rec.get("value") or {}
    roles_list = r_val.get("roles") or []
    if r_status == "available" and roles_list and r_rec.get("content_sha256"):
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
            "availability": "not_available" if r_status in {"available", "not_found", "absent", None} else _to_availability_state(r_status),
            "confidence": 0.0,
            "evidence_ids": [f"ev-roles-{org}"] if "roles" in ev else [],
        })

    # 6. Operating Locations (Subunits) Claim
    l_rec = ev.get("locations") or {}
    l_status = l_rec.get("status")
    l_val = l_rec.get("value") or {}
    locs_list = l_val.get("locations") or []
    if l_status == "available" and locs_list and l_rec.get("content_sha256"):
        claims.append({
            "field": "operating_locations",
            "value": locs_list,
            "availability": "available",
            "confidence": 1.0,
            "evidence_ids": [f"ev-locations-{org}"],
        })
        legal_name = " ".join(str(profile.get("name") or "").casefold().split())
        trading_names = [{"name": name, "subunit_organisation_number": unit["organisation_number"],
                          "relationship": "registered_subunit"}
                         for unit in locs_list if unit.get("organisation_number")
                         and (name := str(unit.get("name") or "").strip())
                         and " ".join(name.casefold().split()) != legal_name]
        if trading_names and l_rec.get("source_fields"):
            claims.append({"field": "registered_subunit_trading_names", "value": trading_names,
                           "availability": "available", "confidence": 1.0, "evidence_ids": [f"ev-locations-{org}"]})
    else:
        claims.append({
            "field": "operating_locations",
            "value": None,
            "availability": "not_available" if l_status in {"available", "not_available", "not_found", "absent", None} else _to_availability_state(l_status),
            "confidence": 0.0,
            "evidence_ids": [f"ev-locations-{org}"] if "locations" in ev else [],
        })
    address_rec = ev.get("registered_address") or {}
    if address_rec.get("status") == "available" and address_rec.get("content_sha256"):
        claims.append({
            "field": "registered_address",
            "value": address_rec.get("value"),
            "availability": "available",
            "confidence": 1.0,
            "evidence_ids": [f"ev-registered_address-{org}"],
        })

    # 7. Hiring / Recruitment Status Claim (and hiring_signal alias)
    from .hiring import extract_website_hiring_signals

    hiring = profile.get("hiring") or (ev.get("hiring") or {}).get("value") or {}
    career_pages = extract_website_hiring_signals(w_val if is_publishable else None).get("career_evidence") or []
    career_page = next((page for page in career_pages if re.fullmatch(r"[0-9a-fA-F]{64}", str(page.get("content_sha256") or ""))), None)
    careers_url = career_page["source_url"] if career_page and has_valid_site_ev else None
    career_ev_id = f"ev-careers-page-{org}"
    notice_id = f"ev-recruitment-notice-{org}"
    profile["recruitment_notice"] = None
    if has_valid_site_ev:
        site_host = (urllib.parse.urlsplit(website_url).hostname or "").removeprefix("www.")
        for page in w_val.get("pages") or []:
            url, digest = str(page.get("url") or ""), str(page.get("content_sha256") or "")
            title = str(page.get("heading") or "").strip()
            body = str(page.get("main_text_excerpt") or "")
            if (not url or (urllib.parse.urlsplit(url).hostname or "").removeprefix("www.") != site_host
                    or not re.fullmatch(r"[a-fA-F0-9]{64}", digest)
                    or not re.fullmatch(r"(?:vi søker|we are hiring)\s+.{6,100}", title, re.I)
                    or not re.search(r"søknad med cv|send (?:inn )?(?:søknad|cv)|apply (?:here|now)|submit (?:your|an) application", body, re.I)
                    or re.search(r"søknadsfristen er utløpt|stillingen er besatt|no longer accepting|position has closed", body, re.I)):
                continue
            observed_at = page.get("retrieved_at") or w_rec.get("retrieved_at") or completed_at
            evidence_items.append({"id": notice_id, "source_url": url, "content_sha256": digest,
                                   "source_class": "company_site", "retrieved_at": observed_at, "literal_excerpt": title})
            notice = {"title": title, "url": url, "retrieved_at": observed_at, "opening_status": "unverified"}
            profile["recruitment_notice"] = notice
            claims.append({"field": "first_party_recruitment_notice", "value": notice,
                           "availability": "available", "confidence": 0.95, "evidence_ids": [site_ev_id, notice_id]})
            break
    if careers_url:
        evidence_items.append({"id": career_ev_id, "source_url": careers_url, "source_class": "company_site",
                               "retrieved_at": w_rec.get("retrieved_at") or completed_at,
                               "content_sha256": career_page["content_sha256"], "claim_span": career_page["claim_span"]})
        claims.append({"field": "careers_page", "value": careers_url, "availability": "available",
                       "confidence": 0.95, "evidence_ids": [site_ev_id, career_ev_id]})

    # 8. Accounting Obligation Claim
    a_status = a_rec.get("status")
    a_val = a_rec.get("value") or {}
    reg_ev_id = (
        f"ev-registry_live-{org}"
        if reg_live_rec.get("status") == "available" and reg_live_rec.get("content_sha256")
        else (
            f"ev-registry-{org}"
            if reg_bulk_rec.get("status") == "available" and reg_bulk_rec.get("content_sha256")
            else None
        )
    )
    acc_ev_ids = [f"ev-accounting_obligation-{org}"]
    if reg_ev_id and reg_ev_id not in acc_ev_ids:
        acc_ev_ids.insert(0, reg_ev_id)
    if a_status == "available" and a_val.get("classification") and a_rec.get("content_sha256"):
        claims.append({
            "field": "accounting_obligation",
            "value": a_val.get("classification"),
            "availability": "available",
            "confidence": 1.0,
            "evidence_ids": acc_ev_ids,
        })
    else:
        claims.append({
            "field": "accounting_obligation",
            "value": None,
            "availability": _to_availability_state(a_status),
            "confidence": 0.0,
            "evidence_ids": acc_ev_ids if "accounting_obligation" in ev else [],
        })

    # 9. Scored Dated News Claim
    from .verified_items import cision_release_channel

    n_rec = ev.get("news") or {}
    n_status = n_rec.get("status")
    n_val = n_rec.get("value") or {}
    raw_news_candidates: list[dict[str, Any]] = []
    if is_publishable and w_val.get("news_items"):
        raw_news_candidates.extend(w_val.get("news_items") or [])
    if is_publishable and w_val.get("pages"):
        for p in w_val.get("pages", []):
            if p.get("news_items"):
                raw_news_candidates.extend(p["news_items"])
    if n_status == "available" and n_val.get("items"):
        raw_news_candidates.extend(n_val.get("items") or [])

    crawled_page_shas: dict[str, str] = {}
    crawled_news_pages: dict[str, dict[str, Any]] = {}
    if has_valid_site_ev:
        crawled_page_shas[str(w_rec.get("source_url") or website_url).rstrip("/")] = str(w_rec.get("content_sha256"))
        for p in w_val.get("pages", []) or []:
            if p.get("url") and p.get("content_sha256"):
                crawled_page_shas[str(p["url"]).rstrip("/")] = str(p["content_sha256"])
                crawled_news_pages[str(p["url"]).rstrip("/")] = p
    valid_page_sha_set = set(crawled_page_shas.values())

    generic_news_titles = {
        "nyheter", "aktuelt", "news", "presse", "pressemeldinger", "press releases",
        "siste nytt", "blogg", "artikler", "our purpose", "about us", "our brand",
        "our history", "contacting equinor", "ethics helpline", "careers in equinor",
        "contact us about careers", "meet the summer interns",
        "our summer internship programme",
    }
    editorial_news_items: list[dict[str, Any]] = []
    official_notice_items: list[dict[str, Any]] = []
    seen_news_keys: set[str] = set()
    captured_site_domain = registered_domain(str(w_val.get("final_url") or w_rec.get("source_url") or ""))
    for cand in raw_news_candidates:
        if not isinstance(cand, dict):
            continue
        c_title = str(cand.get("title") or cand.get("text") or "").strip()
        c_url = str(cand.get("url") or cand.get("source_url") or "").strip()
        c_src = str(cand.get("source") or "").lower()
        if not c_title or not c_url.startswith(("http://", "https://")):
            continue
        lower_t = c_title.casefold()
        lower_u = c_url.casefold()
        if any(k in lower_u for k in ["/career", "/karriere", "/jobb", "/stilling", "/vacanc", "/work-with-us", "/about-us", "/om-oss"]):
            continue
        if lower_t in generic_news_titles or any(
            skip in lower_t
            for skip in ["summer internship", "contact us about careers", "ethics helpline", "owner relations", "discover the stories"]
        ):
            continue
        norm_pub = _normalize_date_str(cand.get("published_at")) or _normalize_date_str(cand.get("excerpt")) or _normalize_date_str(c_url)
        if not norm_pub:
            continue
        dedup_k = c_url.rstrip("/").casefold()
        if dedup_k in seen_news_keys:
            continue
        seen_news_keys.add(dedup_k)

        cand_sha = str(cand.get("content_sha256") or "")
        site_reg_dom = registered_domain(website_url) if website_url else ""
        cand_reg_dom = registered_domain(c_url)
        is_site_news = bool(has_valid_site_ev and site_reg_dom and cand_reg_dom == site_reg_dom)
        is_official = "brreg" in c_src or "brreg.no" in lower_u or "offisiell registrering" in lower_t or "kunngjøring" in lower_t
        is_company_release = bool(cand.get("strategy") == "cision_company_press_release"
                                  and cision_release_channel(c_url)
                                  and c_url == cand.get("source_url")
                                  and (cand.get("literal_excerpt") or cand.get("claim_span")))
        scope_url = str((presence.get("value") or {}).get("url") or "")
        is_scoped_news = bool(presence.get("status") == "available" and scope_url and c_url.startswith(scope_url)
                              and cand.get("strategy") == "scoped_first_party_news"
                              and c_url == cand.get("source_url") and cand.get("claim_span"))
        first_party_or_official = is_site_news or is_official or is_company_release or is_scoped_news
        permitted_independent_news = (cand.get("strategy") == "independent_news_discovery"
                                      and cand_reg_dom != captured_site_domain)
        if ((first_party_activity_only and not first_party_or_official)
                or (require_legal_operator and not (first_party_or_official or permitted_independent_news))):
            continue
        if is_site_news:
            cand_src_url = str(cand.get("source_url") or (w_rec.get("source_url") if has_valid_site_ev else c_url))
            source_sha = crawled_page_shas.get(cand_src_url.rstrip("/"))
            if source_sha:
                cand_sha = source_sha
            elif cand.get("verification") in {"article_fetched", "inline_newsroom"}:
                # Publication needs the fetched article/newsroom response.
                continue
            source_page = crawled_news_pages.get(cand_src_url.rstrip("/")) or {}
            attribution_text = (cand.get("excerpt") if cand.get("verification") == "inline_newsroom" else
                                (cand.get("article_text_excerpt") or source_page.get("main_text_excerpt")))
            if not first_party_news_about_company(profile.get("name") or "", c_title,
                                                   str(attribution_text or cand.get("claim_span") or "")):
                continue
        else:
            cand_src_url = str(cand.get("source_url") or c_url)
        if not cand_sha:
            continue

        is_official = "brreg" in c_src or "brreg.no" in lower_u or "offisiell registrering" in lower_t or "kunngjøring" in lower_t
        item = {
            "title": c_title,
            "url": c_url,
            "source_url": cand_src_url,
            "published_at": norm_pub,
            "content_sha256": cand_sha,
            "verification": cand.get("verification") or ("site_listing" if is_site_news else ""),
            "name_in_article": (
                bool(cand.get("name_in_article"))
                or (is_site_news and cand.get("verification") == "article_fetched"
                    and name_in_text(profile.get("name") or "", str(cand.get("article_text_excerpt") or ""),
                                     allow_short_single_token=True))
                or (is_site_news and cand.get("verification") == "inline_newsroom"
                    and name_in_text(profile.get("name") or "", str(cand.get("excerpt") or ""),
                                     allow_short_single_token=True))
                or (not is_site_news and cand.get("verification") == "article_fetched"
                    and name_in_text(profile.get("name") or "", str(cand.get("article_text_excerpt") or "")))
            ),
            "_is_site": is_site_news,
            "_is_generic_prefix": lower_t.startswith(("nyheter ", "aktuelt ", "news ", "presse ")),
        }
        if is_company_release:
            item.update(strategy=cand["strategy"], literal_excerpt=cand.get("literal_excerpt") or cand.get("claim_span"),
                        retrieved_at=cand.get("retrieved_at"), content_class="press_release_distribution")
        elif is_scoped_news:
            item.update(literal_excerpt=cand["claim_span"], retrieved_at=cand.get("retrieved_at"),
                        content_class="first_party_announcement", relationship_scope="norwegian_company_presence")
        elif is_site_news:
            item.update(content_class="first_party_announcement", literal_excerpt=(
                str(cand.get("literal_excerpt") or cand.get("article_text_excerpt") or
                    (cand.get("excerpt") if cand.get("verification") == "inline_newsroom" else ""))[:500] or None
            ))
        else:
            item["content_class"] = "independent_editorial_news"
        if is_official:
            official_notice_items.append(item)
        else:
            editorial_news_items.append(item)

    editorial_news_items.sort(
        key=lambda x: (
            1 if x.get("_is_site") else 0,
            0 if x.get("_is_generic_prefix") else 1,
            str(x.get("published_at") or ""),
        ),
        reverse=True,
    )
    clean_news_items = []
    seen_story_keys = set()
    c_name = profile.get("name") or ""
    for item in editorial_news_items:
        clean_it = {k: v for k, v in item.items() if not k.startswith("_")}
        ok, _reason = validate_news_item(clean_it, company_name=c_name, site_url=website_url or "")
        if ok:
            page = crawled_news_pages.get(str(clean_it["source_url"]).rstrip("/")) or {}
            story_key = page.get("story_key") or clean_it["url"].rstrip("/")
            if story_key in seen_story_keys:
                continue
            seen_story_keys.add(story_key)
            clean_news_items.append(clean_it)
    # Only real dated journalism/news articles about the company are published as news;
    # official notices, trademark filings, inspections, and registry filings are NOT news.

    if is_publishable and isinstance(w_val, dict):
        w_val["news_items"] = clean_news_items
    if n_status == "available" and isinstance(n_val, dict) and "items" in n_val:
        n_val.update(items=clean_news_items, count=len(clean_news_items))
        if not clean_news_items:
            n_rec["status"] = "not_available"

    news_ev_id = f"ev-news-{org}"
    if clean_news_items:
        primary_news = clean_news_items[0]
        n_url = str(primary_news.get("url") or website_url or "")
        site_reg_dom = registered_domain(website_url) if website_url else ""
        is_primary_site = bool(site_reg_dom) and (registered_domain(n_url) == site_reg_dom)

        if is_primary_site and has_valid_site_ev:
            n_src_url = str(primary_news.get("source_url") or w_rec.get("source_url") or website_url)
            n_sha = str(primary_news.get("content_sha256") or "")
            news_ev_ids = [site_ev_id, news_ev_id]
            news_source_class = "company_site"
        else:
            n_src_url = str(primary_news.get("source_url") or n_url)
            n_sha = str(primary_news.get("content_sha256") or "")
            news_ev_ids = [news_ev_id]
            news_source_class = "public_news"

        if not n_sha:
            raise ValueError("Validated news item lacks its own source response hash")
        n_pub = primary_news.get("published_at")
        n_title = str(primary_news.get("title") or "Nyhet")
        for index, item in enumerate(clean_news_items):
            release_id = f"ev-news-article-{org}-{index + 1}"
            evidence_items.append({"id": release_id, "source_url": item["source_url"],
                                   "content_sha256": item["content_sha256"], "source_class": item["content_class"],
                                   "retrieved_at": item.get("retrieved_at") or completed_at,
                                   "literal_excerpt": item.get("literal_excerpt")})
            news_ev_ids.append(release_id)
        news_claim_val = {
            "title": n_title,
            "published_at": n_pub,
            "url": n_url,
            "source_url": n_src_url,
            "items": clean_news_items,
            "count": len(clean_news_items),
        }
        claims.append({
            "field": "dated_news",
            "value": news_claim_val,
            "availability": "available",
            "confidence": 0.95,
            "evidence_ids": news_ev_ids,
        })
        claims.append({
            "field": "news",
            "value": news_claim_val,
            "availability": "available",
            "confidence": 0.95,
            "evidence_ids": news_ev_ids,
        })
        existing_news = next((item for item in evidence_items if item["id"] == news_ev_id), None)
        if existing_news:
            existing_news["content_sha256"] = n_sha
            existing_news["source_url"] = n_src_url
            existing_news["source_class"] = news_source_class
        else:
            evidence_items.append({
                "id": news_ev_id,
                "source_url": n_src_url,
                "source_class": news_source_class,
                "retrieved_at": completed_at,
                "content_sha256": n_sha,
                "claim_span": f"Dated news: {n_title} (published {n_pub})",
            })
    else:
        news_avail = "not_available" if n_status in {"available", "not_available", "not_found", "absent", None} else _to_availability_state(n_status)
        claims.append({
            "field": "dated_news",
            "value": None,
            "availability": news_avail,
            "confidence": 0.0,
            "evidence_ids": [news_ev_id] if "news" in ev else [],
        })
        claims.append({
            "field": "news",
            "value": None,
            "availability": news_avail,
            "confidence": 0.0,
            "evidence_ids": [news_ev_id] if "news" in ev else [],
        })

    # 10. Scored Job Postings Claim
    j_rec = ev.get("jobs") or {}
    j_status = j_rec.get("status")
    j_val = j_rec.get("value") or {}
    raw_job_candidates: list[dict[str, Any]] = []
    if j_status == "available" and j_val.get("items"):
        raw_job_candidates.extend(j_val.get("items") or [])
    if is_publishable:
        if w_val.get("verified_job_adverts"):
            raw_job_candidates.extend(w_val.get("verified_job_adverts") or [])
        if w_val.get("job_postings"):
            raw_job_candidates.extend(w_val.get("job_postings") or [])
    if hiring.get("signals"):
        for sig in hiring.get("signals", []):
            if sig.get("type") in {"official_job_posting", "corporate_job_posting"} and sig.get("title"):
                if sig.get("source") == "nav_arbeidsplassen" and not sig.get("match_type"):
                    continue
                if sig.get("source") in {"nav_arbeidsplassen", "linkedin_jobs"} or is_publishable:
                    raw_job_candidates.append({
                        "title": sig.get("title"),
                        "url": sig.get("url"),
                        "published_at": sig.get("published_at"),
                        "content_sha256": sig.get("content_sha256"),
                        "kind": sig.get("kind"),
                        "employer_org": sig.get("employer_org"),
                        "employer_name": sig.get("employer_name"),
                        "match_type": sig.get("match_type"),
                        "valid_through": sig.get("valid_through"),
                        "nav_feed": sig.get("nav_feed"),
                        "hiring_org_match": sig.get("hiring_org_match"),
                    })

    generic_job_titles = {
        "vacancies", "offene stellen", "jobb hos oss", "jobbe hos oss", "ledige stillinger",
        "karriere", "careers", "careers in", "careers in equinor", "work with us",
        "join our team", "our purpose", "contact us about careers", "meet the summer interns",
        "discover the stories behind the headlines in our magazine", "åpen søknad",
        "open application",
    }
    job_items: list[dict[str, Any]] = []
    seen_job_keys: set[str] = set()
    for cand in raw_job_candidates:
        if not isinstance(cand, dict):
            continue
        if cand.get("requires_ad_verification"):
            continue
        raw_t = str(cand.get("title") or cand.get("job_title") or "").replace("\\&", "&").strip()
        j_u = str(cand.get("url") or cand.get("source_url") or "").strip()
        if not raw_t or not j_u.startswith(("http://", "https://")):
            continue
        lower_t = raw_t.casefold()
        lower_u = j_u.casefold()
        if lower_t in generic_job_titles or any(
            skip in lower_t
            for skip in [
                "our purpose", "discover the stories", "contact us about careers",
                "meet the summer interns", "ledige stillinger på ", "we are now accepting applications",
            ]
        ):
            continue
        if any(bad in lower_u for bad in ["/about-us", "/magazine", "/careers/contact", "/kontakt", "/om-oss/jobb-hos-oss"]):
            continue
        dedup_k = lower_u.rstrip("/")
        if dedup_k in seen_job_keys:
            continue
        seen_job_keys.add(dedup_k)

        is_verified_board = cand.get("kind") == "verified_career_ad" or "arbeidsplassen.nav.no/stillinger/stilling/" in lower_u or "linkedin.com/jobs/view/" in lower_u
        j_cand_sha = str(cand.get("content_sha256") or "")
        j_src_url = str(cand.get("source_url") or j_u)
        if not j_cand_sha or (not is_verified_board and j_cand_sha not in valid_page_sha_set):
            continue

        norm_j_pub = _normalize_date_str(cand.get("published_at")) or cand.get("published_at")
        job_items.append({
            "title": raw_t,
            "url": j_u,
            "source_url": j_src_url,
            "published_at": norm_j_pub,
            "content_sha256": j_cand_sha,
            "kind": cand.get("kind") or ("nav_ad" if "arbeidsplassen.nav.no" in lower_u else ""),
            "employer_org": cand.get("employer_org") or "",
            "employer_name": cand.get("employer_name") or "",
            "match_type": cand.get("match_type") or "",
            "hiring_org_match": cand.get("hiring_org_match"),
            "valid_through": cand.get("valid_through"),
            "application_due": cand.get("application_due"),
            "nav_feed": cand.get("nav_feed"),
            "career_source_url": cand.get("career_source_url"),
            "career_link_url": cand.get("career_link_url"),
            "active_listing": cand.get("active_listing"),
            "ad_text_excerpt": cand.get("ad_text_excerpt"),
            "_priority": 2 if "arbeidsplassen.nav.no" in lower_u else (1 if "linkedin.com/jobs/view/" in lower_u else 0),
        })

    job_items.sort(key=lambda x: (x.get("_priority", 0), str(x.get("published_at") or "")), reverse=True)
    clean_job_items = []
    c_name = profile.get("name") or ""
    subunit_orgs = [str(u.get("organisation_number")) for u in (l_val.get("locations") or []) if u.get("organisation_number")]
    for item in job_items:
        clean_it = {k: v for k, v in item.items() if not k.startswith("_")}
        ok, _reason = validate_job_item(clean_it, org=org, company_name=c_name, site_url=website_url or "", site_publishable=is_publishable, subunit_orgs=subunit_orgs)
        if ok:
            clean_job_items.append(clean_it)

    # Only single job advertisements posted by this company are published as job_postings;
    # listing pages, career hubs, open application placeholders, and other employers' ads are never published.

    if j_status == "available" and isinstance(j_val, dict) and "items" in j_val:
        j_val.update(items=clean_job_items, count=len(clean_job_items))
        if not clean_job_items:
            j_rec["status"] = "not_available"
    jobs_ev_id = f"ev-jobs-{org}"
    if clean_job_items:
        hiring_state = "actively_recruiting"
        hiring_sources = [jobs_ev_id]
        assessment = "At least one current individual vacancy was verified."
    elif profile["recruitment_notice"]:
        hiring_state = "recruitment_notice_without_verified_expiry"
        hiring_sources = [site_ev_id, notice_id]
        assessment = "A first-party recruitment notice was found. Vacancy freshness is unconfirmed."
    elif careers_url:
        hiring_state = "recruitment_page_present"
        hiring_sources = [site_ev_id, career_ev_id]
        assessment = "A company careers page was found. No current individual vacancy was verified."
    else:
        hiring_state = "unknown"
        hiring_sources = []
        assessment = "The checked sources did not establish current hiring status."
    hiring_value = {"active_job_postings_count": len(clean_job_items),
                    "appears_to_be_hiring": True if clean_job_items else None,
                    "status": hiring_state, "careers_url": careers_url, "url": careers_url}
    profile["hiring"] = {**hiring, **hiring_value, "assessment": assessment,
                         "confidence": "high" if clean_job_items else "limited"}
    claims.append({"field": "hiring_status", "value": hiring_value if hiring_sources else None,
                   "availability": "available" if hiring_sources else "not_available",
                   "confidence": 0.95 if hiring_sources else 0.0, "evidence_ids": hiring_sources})

    if clean_job_items:
        primary_job = clean_job_items[0]
        j_url = str(primary_job.get("url") or website_url or "")
        j_src_url = str(primary_job.get("source_url") or j_url)
        j_sha = str(primary_job.get("content_sha256") or "")
        j_title = str(primary_job.get("title") or "Ledig stilling")
        j_pub = primary_job.get("published_at")
        job_claim_val = {
            "title": j_title,
            "url": j_url,
            "published_at": j_pub,
            "items": clean_job_items,
            "count": len(clean_job_items),
        }
        claims.append({
            "field": "job_postings",
            "value": job_claim_val,
            "availability": "available",
            "confidence": 0.95,
            "evidence_ids": [jobs_ev_id],
        })
        claims.append({
            "field": "jobs",
            "value": job_claim_val,
            "availability": "available",
            "confidence": 0.95,
            "evidence_ids": [jobs_ev_id],
        })
        if primary_job.get("kind") in {"verified_career_ad", "company_site_ad"}:
            job_evidence_ids = [jobs_ev_id]
            for index, item in enumerate(clean_job_items):
                listing = item.get("active_listing")
                if listing:
                    listing_id = f"{jobs_ev_id}-listing-{index + 1}"
                    evidence_items.append({
                        "id": listing_id,
                        "source_url": listing["source_url"],
                        "source_class": "company_site" if item.get("kind") == "company_site_ad" else "job_board",
                        "retrieved_at": listing["retrieved_at"],
                        "content_sha256": listing["content_sha256"],
                        "claim_span": f"Published job listing includes {item['url']}",
                    })
                    job_evidence_ids.append(listing_id)
                career_url = str(item.get("career_source_url") or "")
                career_sha = crawled_page_shas.get(career_url.rstrip("/"))
                if index:
                    advert_id = f"{jobs_ev_id}-{index + 1}"
                    evidence_items.append({
                        "id": advert_id,
                        "source_url": item["url"],
                        "source_class": "company_site" if item.get("kind") == "company_site_ad" else "job_board",
                        "retrieved_at": j_rec.get("retrieved_at") or completed_at,
                        "content_sha256": item["content_sha256"],
                        "literal_excerpt": item.get("ad_text_excerpt"),
                    })
                    job_evidence_ids.append(advert_id)
                if career_sha and career_url and not any(
                    ev_item.get("source_url") == career_url and ev_item.get("content_sha256") == career_sha
                    and ev_item["id"] in job_evidence_ids for ev_item in evidence_items
                ):
                    career_id = f"{jobs_ev_id}-career-{index + 1}"
                    evidence_items.append({
                        "id": career_id,
                        "source_url": career_url,
                        "source_class": "company_site",
                        "retrieved_at": w_rec.get("retrieved_at") or completed_at,
                        "content_sha256": career_sha,
                        "claim_span": f"Official recruitment link: {item.get('career_link_url') or item['url']}",
                    })
                    job_evidence_ids.append(career_id)
            for claim in claims[-2:]:
                claim["evidence_ids"] = job_evidence_ids
        existing_job = next((item for item in evidence_items if item["id"] == jobs_ev_id), None)
        if existing_job:
            existing_job["content_sha256"] = j_sha
            existing_job["source_url"] = j_src_url
        else:
            evidence_items.append({
                "id": jobs_ev_id,
                "source_url": j_src_url,
                "source_class": "official" if "arbeidsplassen.nav.no" in j_url.lower() else ("company_site" if has_valid_site_ev else "job_board"),
                "retrieved_at": completed_at,
                "content_sha256": j_sha,
                "claim_span": f"Active job vacancy: {j_title} at {j_url}",
            })
    else:
        jobs_avail = "not_available" if j_status in {"available", "not_available", "not_found", "absent", None} else _to_availability_state(j_status)
        claims.append({
            "field": "job_postings",
            "value": None,
            "availability": jobs_avail,
            "confidence": 0.0,
            "evidence_ids": [jobs_ev_id] if "jobs" in ev else [],
        })
        claims.append({
            "field": "jobs",
            "value": None,
            "availability": jobs_avail,
            "confidence": 0.0,
            "evidence_ids": [jobs_ev_id] if "jobs" in ev else [],
        })

    claims.append({
        "field": "hiring_signal",
        "value": clean_job_items[0]["url"] if clean_job_items else None,
        "availability": "available" if clean_job_items else "not_available",
        "confidence": 0.95 if clean_job_items else 0.0,
        "evidence_ids": [jobs_ev_id] if clean_job_items else [],
    })

    # Final Evidence Provenance & Cryptographic Grounding Verification
    ev_map = {item["id"]: item for item in evidence_items}
    for item in evidence_items:
        legacy_description = item.pop("claim_span", None)
        excerpt = item.get("literal_excerpt")
        if excerpt:
            item["evidence_kind"] = "literal_excerpt"
            item["literal_excerpt"] = " ".join(str(excerpt).split())[:500]
            item["claim_span"] = item["literal_excerpt"]
        elif item.get("source_fields"):
            item["evidence_kind"] = "structured_pointer"
        else:
            item["evidence_kind"] = "source_record"
            if legacy_description:
                item["description"] = legacy_description
    for claim in claims:
        claim["evidence_ids"] = [eid for eid in claim.get("evidence_ids", []) if eid in ev_map]
        if claim.get("availability") == "available" and not claim["evidence_ids"]:
            claim.update(value=None, availability="ambiguous", confidence=0.0)

    return claims, evidence_items


def terminal_envelope(
    profile: dict[str, Any],
    *,
    run_id: str,
    modules: Iterable[str],
    started_at: str,
    completed_at: str,
    first_party_activity_only: bool = False,
    require_legal_operator: bool = False,
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
    entity_state = "failed" if any(item["state"] == "failed" for item in module_states.values()) else "available"

    # Canonical company-level result state according to Builderr specification
    reg_status = (profile.get("evidence", {}).get("registry") or {}).get("status")
    reg_live_status = (profile.get("evidence", {}).get("registry_live") or {}).get("status")
    reg_module_state = module_states.get("registry", {}).get("state")

    core_registry_ok = (
        reg_status == "available"
        or reg_live_status == "available"
        or (profile.get("name") and not str(profile.get("name")).startswith("Organisation "))
    )

    if not core_registry_ok and (
        reg_status in {"submission_error", "source_error"}
        or reg_live_status in {"submission_error", "source_error"}
    ):
        company_availability = "failed"
        entity_state = "failed"
    elif any(s.get("state") == "failed" for s in module_states.values()):
        # If core official identity is verified and healthy, do not drop the envelope
        # over an optional or secondary module
        if core_registry_ok and not any(
            module_states.get(m, {}).get("state") == "failed"
            for m in ("registry", "registry_live")
        ):
            company_availability = "available"
            entity_state = "available"
        else:
            company_availability = "failed"
            entity_state = "failed"
    elif reg_status == "not_found" and reg_live_status in {None, "not_found", "absent"}:
        company_availability = "not_available"
    elif reg_status == "blocked" or reg_live_status == "blocked":
        company_availability = "blocked"
    elif reg_status == "not_applicable":
        company_availability = "not_applicable"
    elif reg_status == "ambiguous":
        company_availability = "ambiguous"
    elif core_registry_ok:
        company_availability = "available"
    else:
        company_availability = "not_available"

    if not profile.get("hiring"):
        try:
            from .hiring import evaluate_company_hiring
            hiring_block, _ = evaluate_company_hiring(profile, [])
            profile["hiring"] = hiring_block
        except Exception:
            pass
    claims, evidence_list = build_contract_claims_and_evidence(
        profile, completed_at, first_party_activity_only=first_party_activity_only,
        require_legal_operator=require_legal_operator,
    )
    website_fields = {"official_website": "exact_operator", "registry_declared_website": "registry_declared",
                      "norwegian_website_presence": "norwegian_company_presence", "registry_website_redirect": "legacy_redirect",
                      "group_website_company_page": "legal_entity_on_group_domain"}
    profile["website_relationships"] = [
        {"scope": website_fields[claim["field"]], "value": claim["value"], "evidence_ids": claim["evidence_ids"]}
        for claim in claims if claim["field"] in website_fields and claim["availability"] == "available"
    ]
    if first_party_activity_only or require_legal_operator:
        profile["website"] = next((item["value"] for item in profile["website_relationships"] if item["scope"] == "exact_operator"), None)
    run_metrics = profile.get("run_metrics") or {}
    evidence_by_id = {item["id"]: item for item in evidence_list}
    for claim in claims:
        supporting_records = [evidence_by_id[eid] for eid in claim["evidence_ids"]]
        captured_at = [record["retrieved_at"] for record in supporting_records if record.get("retrieved_at")]
        live = bool(profile.get("collection_mode") == "live" and captured_at
                    and all(started_at[:10] <= stamp[:10] <= completed_at[:10] for stamp in captured_at)
                    and all("bulk" not in record["source_class"] for record in supporting_records))
        claim["last_verified_at"] = min(captured_at, default=None)
        claim["revalidated_this_run"] = live
        claim["freshness_class"] = "live" if live else ("snapshot" if captured_at else "unknown")

    from .synthesis import synthesize_published_claims

    synth = synthesize_published_claims(claims, evidence_list)
    profile["synthesis"] = synth
    profile["summary"] = synth["summary"]

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
            "terminal_status": "completed" if entity_state != "failed" and company_availability != "failed" else "failed",
            "availability": company_availability,
        },
        "legal_identity": {
            "organisation_number": org,
            "name": profile.get("name"),
            "legal_form": profile.get("legal_form"),
            "municipality": profile.get("municipality"),
        },
        "synthesis": synth,
        "summary": (synth.get("summary") if isinstance(synth, dict) else "") or "",
        "claims": claims,
        "evidence": evidence_list,
        "changes": profile.get("change_history") or [],
        "errors": [
            {"module": module, "status": record.get("status"),
             "source_url": record.get("source_url"), "reason": record.get("note")}
            for module, record in (profile.get("evidence") or {}).items()
            if isinstance(record, dict) and record.get("status") in {"source_error", "submission_error", "blocked"}
        ],
        "operations": {
            "requests": int(run_metrics.get("requests") or 0),
            "runtime_ms": int((run_metrics.get("elapsed_s") or 0) * 1000),
            # The shared concurrent budget records real run-level spend, but
            # cannot attribute it reliably to this single company.
            "third_party_cost_usd": run_metrics.get("third_party_cost_usd"),
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
    semantic_issues = []
    for item in envelopes:
        if "profile" not in item or "claims" not in item:
            continue
        profile = item["profile"]
        available = {claim["field"]: claim for claim in item["claims"] if claim.get("availability") == "available"}
        evidence_ids = {record["id"] for record in item.get("evidence") or []}
        issues = []
        if any(not claim.get("evidence_ids") or not set(claim["evidence_ids"]) <= evidence_ids for claim in available.values()):
            issues.append("published_claim_without_evidence")
        financials = (profile.get("evidence") or {}).get("financials") or {}
        records = (financials.get("value") or {}).get("records") or []
        if financials.get("status") == "available" and records:
            latest = latest_account(records)
            year = fiscal_year_of(latest)
            accounts = (available.get("annual_accounts") or {}).get("value") or {}
            if profile.get("latest_account_year") != year or (accounts and accounts.get("fiscal_year") != year):
                issues.append("stale_fiscal_year")
            if accounts and accounts.get("cash_and_bank_deposits") != latest.get("cash_and_bank_deposits"):
                issues.append("cash_differs_from_normalized_filing")
            for record in records:
                ratio, assets, debt = (record.get(field) for field in ("current_ratio", "current_assets", "short_term_debt"))
                if ratio is not None and (assets is None or assets < 0 or not debt or debt < 0 or abs(ratio - assets / debt) > 0.000051):
                    issues.append("invalid_current_ratio")
                    break
        record = (profile.get("evidence") or {}).get("website") or {}
        website = (available.get("official_website") or {}).get("value")
        if (website and website != published_website_url(record, profile.get("website"))) or (
                "website_relationships" in profile and
                any(link["scope"] == "exact_operator" for link in profile["website_relationships"]) != bool(website)):
            issues.append("contradictory_website_publication")
        if "job_postings" in available:
            hiring = (available.get("hiring_status") or {}).get("value") or {}
            if "hiring_signal" not in available or hiring.get("status") != "actively_recruiting":
                issues.append("verified_job_without_hiring_signal")
        semantic_issues.extend({"organisation_number": item["organisation_number"], "issue": issue} for issue in issues)
    checks = {
        "exact_expected_count": len(envelopes) == expected_count,
        "unique_organisation_numbers": len(orgs) == len(set(orgs)),
        "all_entity_states_terminal": all(item.get("state") in TERMINAL_STATES for item in envelopes),
        "all_module_states_terminal": not invalid_states,
        "zero_silent_drops": len(envelopes) == expected_count and len(orgs) == len(set(orgs)),
        "semantic_consistency": not semantic_issues,
    }
    return {"passed": all(checks.values()), "checks": checks, "invalid_states": invalid_states,
            "semantic_issues": semantic_issues}


def profile_complete_for_modules(profile: dict[str, Any], modules: Iterable[str]) -> bool:
    records = profile.get("evidence", {})
    return all(module in records and records[module].get("status") != "not_fetched" for module in modules)
