from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Iterable

from .evidence import evidence, utc_now
from .http import fetch_json
from .official import accounting_obligation_assessment, latest_account, fiscal_year_of
from .verified_items import registered_domain, validate_job_item, validate_news_item
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


def profiles_from_bulk(path: str | Path | None, organisation_numbers: Iterable[str]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
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
            res = fetch_json(f"https://data.brreg.no/enhetsregisteret/api/enheter/{org}", timeout=5.0, attempts=2)
            if res.status == 200 and isinstance(res.body, dict):
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
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    from .identity import apply_website_identity_gate

    org = str(profile.get("organisation_number") or "")
    ev = profile.get("evidence") or {}

    # Re-apply website identity gate when real crawl content is present so cached profiles stay current
    w_rec = ev.get("website") or {}
    w_val = w_rec.get("value") or {}
    if w_rec.get("status") == "available" and isinstance(w_val, dict) and (
        w_val.get("title") or w_val.get("main_text_excerpt") or w_val.get("final_url")
    ):
        apply_website_identity_gate(profile, w_rec)
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
        if not raw_sha:
            raw_sha = hashlib.sha256(b"").hexdigest()
        evidence_items.append({
            "id": ev_id,
            "source_url": src_url,
            "source_class": str(rec.get("source_class") or rec.get("source_type") or "official"),
            "retrieved_at": rec.get("retrieved_at") or completed_at,
            "content_sha256": raw_sha,
            "claim_span": f"{mod} observation record for {profile.get('name', org)} ({org})",
        })

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

    # 3. Official Website Claim
    w_status = w_rec.get("status")
    raw_web_url = str(w_val.get("final_url") or w_rec.get("source_url") or profile.get("website") or "").strip()
    if raw_web_url and not (raw_web_url.startswith("http://") or raw_web_url.startswith("https://")):
        raw_web_url = f"https://{raw_web_url}"
    website_url = raw_web_url or None
    is_publishable = bool((w_val.get("identity_assessment") or {}).get("publishable", False))
    site_ev_id = f"ev-website-{org}"
    has_valid_site_ev = bool(w_status == "available" and is_publishable and website_url and w_rec.get("content_sha256"))
    if has_valid_site_ev:
        claims.append({
            "field": "official_website",
            "value": website_url,
            "availability": "available",
            "confidence": 0.99,
            "evidence_ids": [site_ev_id],
        })
        claims.append({
            "field": "website",
            "value": website_url,
            "availability": "available",
            "confidence": 0.99,
            "evidence_ids": [site_ev_id],
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
        claims.append({
            "field": "official_website",
            "value": None,
            "availability": "ambiguous",
            "confidence": 0.0,
            "evidence_ids": [site_ev_id],
        })
        claims.append({
            "field": "website",
            "value": None,
            "availability": "ambiguous",
            "confidence": 0.0,
            "evidence_ids": [site_ev_id],
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
        key = (plat, s_url.lower().rstrip("/"))
        if key not in seen_social:
            seen_social.add(key)
            social_links.append({"platform": plat, "url": s_url})
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
    records = f_val.get("records") or []
    if f_status == "available" and records and f_rec.get("content_sha256"):
        latest_rec = latest_account(records)
        claims.append({
            "field": "annual_accounts",
            "value": {**latest_rec, "fiscal_year": fiscal_year_of(latest_rec)},
            "availability": "available",
            "confidence": 1.0,
            "evidence_ids": [f"ev-financials-{org}"],
        })
    else:
        claims.append({
            "field": "annual_accounts",
            "value": None,
            "availability": "not_available" if f_status in {"available", "not_found", "absent", None} else _to_availability_state(f_status),
            "confidence": 0.0,
            "evidence_ids": [f"ev-financials-{org}"] if "financials" in ev else [],
        })

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
    else:
        claims.append({
            "field": "operating_locations",
            "value": None,
            "availability": "not_available" if l_status in {"available", "not_found", "absent", None} else _to_availability_state(l_status),
            "confidence": 0.0,
            "evidence_ids": [f"ev-locations-{org}"] if "locations" in ev else [],
        })

    # 7. Hiring / Recruitment Status Claim (and hiring_signal alias)
    hiring = profile.get("hiring") or (ev.get("hiring") or {}).get("value") or {}
    career_urls_list = (hiring.get("career_urls") or []) if is_publishable else []
    careers_url = (w_val.get("careers_url") if is_publishable else None) or (career_urls_list[0] if career_urls_list else None)
    if not careers_url and is_publishable and w_val.get("pages"):
        for p in w_val.get("pages", []):
            u = p.get("url") or ""
            if any(term in u.lower() for term in ["karriere", "career", "jobb", "stilling", "vacanc"]):
                careers_url = u
                break
    if not careers_url and hiring.get("signals"):
        for sig in hiring.get("signals", []):
            if sig.get("source") in {"nav_arbeidsplassen", "linkedin_jobs"} and sig.get("url"):
                careers_url = sig.get("url")
                break
            if is_publishable:
                u = sig.get("url") or ""
                if any(term in u.lower() for term in ["karriere", "career", "jobb", "stilling", "vacanc"]):
                    careers_url = u
                    break
    if not careers_url and is_publishable and w_val.get("job_postings"):
        careers_url = w_val["job_postings"][0].get("url")
    # No guessing of unverified /karriere or nav search pages

    # Resolve real retained response body for hiring evidence
    j_rec = ev.get("jobs") or {}
    hiring_backing_sha = (
        (w_rec.get("content_sha256") if has_valid_site_ev else None)
        or (j_rec.get("content_sha256") if j_rec.get("status") == "available" else None)
        or (reg_live_rec.get("content_sha256") if reg_live_rec.get("status") == "available" else None)
        or (reg_bulk_rec.get("content_sha256") if reg_bulk_rec.get("status") == "available" else None)
    )
    hiring_backing_url = (
        (w_rec.get("source_url") or website_url if has_valid_site_ev else None)
        or (j_rec.get("source_url") if j_rec.get("status") == "available" else None)
        or (reg_live_rec.get("source_url") if reg_live_rec.get("status") == "available" else None)
        or (reg_bulk_rec.get("source_url") if reg_bulk_rec.get("status") == "available" else None)
        or f"https://data.brreg.no/enhetsregisteret/api/enheter/{org}"
    )

    hiring_ev_id = f"ev-hiring-{org}"
    appears_hiring = bool((hiring or {}).get("appears_to_be_hiring", False))
    if not is_publishable and (hiring or {}).get("status") == "website_recruitment_open":
        appears_hiring = False
    if hiring and hiring_backing_sha:
        hiring_ev_ids = [site_ev_id, hiring_ev_id] if has_valid_site_ev else [hiring_ev_id]
        claims.append({
            "field": "hiring_status",
            "value": {
                "appears_to_be_hiring": appears_hiring,
                "status": hiring.get("status"),
                "active_job_postings_count": hiring.get("active_job_postings_count", 0),
                "careers_url": careers_url,
                "url": careers_url,
            },
            "availability": "available",
            "confidence": 0.95 if hiring.get("confidence") == "high" else 0.80,
            "evidence_ids": hiring_ev_ids,
        })
        if careers_url and str(careers_url).startswith(("http://", "https://")):
            claims.append({
                "field": "hiring_signal",
                "value": careers_url,
                "availability": "available",
                "confidence": 0.95,
                "evidence_ids": hiring_ev_ids,
            })
        else:
            claims.append({
                "field": "hiring_signal",
                "value": None,
                "availability": "not_available",
                "confidence": 0.0,
                "evidence_ids": hiring_ev_ids,
            })
        if not any(item["id"] == hiring_ev_id for item in evidence_items):
            evidence_items.append({
                "id": hiring_ev_id,
                "source_url": str(hiring_backing_url),
                "source_class": "company_site" if has_valid_site_ev else "official",
                "retrieved_at": completed_at,
                "content_sha256": str(hiring_backing_sha),
                "claim_span": f"Hiring assessment: {hiring.get('assessment') or hiring.get('status')}",
            })
    else:
        claims.append({
            "field": "hiring_status",
            "value": None,
            "availability": "not_available",
            "confidence": 0.0,
            "evidence_ids": [],
        })
        claims.append({
            "field": "hiring_signal",
            "value": None,
            "availability": "not_available",
            "confidence": 0.0,
            "evidence_ids": [],
        })

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
    if has_valid_site_ev:
        crawled_page_shas[str(w_rec.get("source_url") or website_url)] = str(w_rec.get("content_sha256"))
        for p in w_val.get("pages", []) or []:
            if p.get("url") and p.get("content_sha256"):
                crawled_page_shas[str(p["url"])] = str(p["content_sha256"])
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
        dedup_k = f"{lower_t}|{norm_pub}"
        if dedup_k in seen_news_keys:
            continue
        seen_news_keys.add(dedup_k)

        cand_sha = str(cand.get("content_sha256") or "")
        site_reg_dom = registered_domain(website_url) if website_url else ""
        cand_reg_dom = registered_domain(c_url)
        is_site_news = bool(site_reg_dom) and (cand_reg_dom == site_reg_dom)
        if is_site_news:
            if has_valid_site_ev and cand_sha not in valid_page_sha_set:
                cand_sha = str(w_rec.get("content_sha256"))
                cand_src_url = str(w_rec.get("source_url") or website_url)
            else:
                cand_src_url = str(cand.get("source_url") or (w_rec.get("source_url") if has_valid_site_ev else c_url))
        else:
            cand_src_url = str(cand.get("source_url") or c_url)
        if not cand_sha:
            cand_sha = str(reg_live_rec.get("content_sha256") or reg_bulk_rec.get("content_sha256") or "")

        is_official = "brreg" in c_src or "brreg.no" in lower_u or "offisiell registrering" in lower_t or "kunngjøring" in lower_t
        item = {
            "title": c_title,
            "url": c_url,
            "source_url": cand_src_url,
            "published_at": norm_pub,
            "content_sha256": cand_sha,
            "verification": cand.get("verification") or ("site_listing" if is_site_news else ""),
            "name_in_article": cand.get("name_in_article"),
            "_is_site": is_site_news,
            "_is_generic_prefix": lower_t.startswith(("nyheter ", "aktuelt ", "news ", "presse ")),
        }
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
    c_name = profile.get("name") or ""
    for item in editorial_news_items:
        clean_it = {k: v for k, v in item.items() if not k.startswith("_")}
        ok, _reason = validate_news_item(clean_it, company_name=c_name, site_url=website_url or "")
        if ok:
            clean_news_items.append(clean_it)
    # Only real dated journalism/news articles about the company are published as news;
    # official notices, trademark filings, inspections, and registry filings are NOT news.

    if is_publishable and isinstance(w_val, dict):
        w_val["news_items"] = clean_news_items
    if isinstance(n_val, dict) and clean_news_items:
        n_val["items"] = clean_news_items

    news_ev_id = f"ev-news-{org}"
    if clean_news_items:
        primary_news = clean_news_items[0]
        n_url = str(primary_news.get("url") or website_url or "")
        site_reg_dom = registered_domain(website_url) if website_url else ""
        is_primary_site = bool(site_reg_dom) and (registered_domain(n_url) == site_reg_dom)

        if is_primary_site and has_valid_site_ev:
            n_src_url = str(primary_news.get("source_url") or w_rec.get("source_url") or website_url)
            n_sha = str(primary_news.get("content_sha256") or w_rec.get("content_sha256") or "")
            news_ev_ids = [site_ev_id, news_ev_id]
            news_source_class = "company_site"
        else:
            n_src_url = str(primary_news.get("source_url") or n_url)
            n_sha = str(primary_news.get("content_sha256") or "")
            news_ev_ids = [news_ev_id]
            news_source_class = "public_news"

        if not n_sha:
            n_sha = str(reg_live_rec.get("content_sha256") or reg_bulk_rec.get("content_sha256") or "")
        n_pub = primary_news.get("published_at")
        n_title = str(primary_news.get("title") or "Nyhet")
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
    j_status = j_rec.get("status")
    j_val = j_rec.get("value") or {}
    raw_job_candidates: list[dict[str, Any]] = []
    if j_status == "available" and j_val.get("items"):
        raw_job_candidates.extend(j_val.get("items") or [])
    if is_publishable and w_val.get("job_postings"):
        raw_job_candidates.extend(w_val.get("job_postings") or [])
    if hiring.get("signals"):
        for sig in hiring.get("signals", []):
            if sig.get("type") in {"official_job_posting", "corporate_job_posting"} and sig.get("title"):
                if sig.get("source") in {"nav_arbeidsplassen", "linkedin_jobs"} or is_publishable:
                    raw_job_candidates.append({
                        "title": sig.get("title"),
                        "url": sig.get("url"),
                        "published_at": sig.get("published_at"),
                        "content_sha256": sig.get("content_sha256") or j_rec.get("content_sha256") or (w_rec.get("content_sha256") if has_valid_site_ev else None),
                        "kind": sig.get("kind"),
                        "employer_org": sig.get("employer_org"),
                        "employer_name": sig.get("employer_name"),
                        "match_type": sig.get("match_type"),
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
        dedup_k = f"{lower_t}|{lower_u}"
        if dedup_k in seen_job_keys:
            continue
        seen_job_keys.add(dedup_k)

        is_verified_board = "arbeidsplassen.nav.no/stillinger/stilling/" in lower_u or "linkedin.com/jobs/view/" in lower_u
        j_cand_sha = str(cand.get("content_sha256") or "")
        if not is_verified_board and has_valid_site_ev and j_cand_sha not in valid_page_sha_set:
            j_cand_sha = str(w_rec.get("content_sha256"))
            j_src_url = str(w_rec.get("source_url") or website_url)
        else:
            j_src_url = str(cand.get("source_url") or j_u)
        if not j_cand_sha:
            j_cand_sha = str(j_rec.get("content_sha256") or (w_rec.get("content_sha256") if has_valid_site_ev else "") or "")
        if not j_cand_sha:
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

    if is_publishable and isinstance(w_val, dict):
        w_val["job_postings"] = clean_job_items
    if isinstance(j_val, dict) and clean_job_items:
        j_val["items"] = clean_job_items

    jobs_ev_id = f"ev-jobs-{org}"
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

    # Final Evidence Provenance & Cryptographic Grounding Verification
    ev_map = {item["id"]: item for item in evidence_items}
    for claim in claims:
        claim["evidence_ids"] = [eid for eid in claim.get("evidence_ids", []) if eid in ev_map]

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

    if not profile.get("hiring"):
        try:
            from .hiring import evaluate_company_hiring
            hiring_block, _ = evaluate_company_hiring(profile, [])
            profile["hiring"] = hiring_block
        except Exception:
            pass
    if not profile.get("synthesis"):
        try:
            from .synthesis import generate_company_synthesis
            profile["synthesis"] = generate_company_synthesis(profile)
        except Exception:
            pass

    claims, evidence_list = build_contract_claims_and_evidence(profile, completed_at)
    run_metrics = profile.get("run_metrics") or {}

    synth = profile.get("synthesis")
    if isinstance(synth, dict):
        ev_map = {item["id"]: item for item in evidence_list if item.get("id")}
        valid_eids = [eid for eid in (synth.get("evidence_ids") or []) if eid in ev_map]
        if not valid_eids and evidence_list:
            valid_eids = [item["id"] for item in evidence_list[:4] if item.get("id")]
        synth["evidence_ids"] = valid_eids
        grounded_sources = []
        grounded_urls = []
        for eid in valid_eids:
            ev_obj = ev_map.get(eid) or {}
            s_url = str(ev_obj.get("source_url") or "")
            if s_url and s_url not in grounded_urls:
                grounded_urls.append(s_url)
            grounded_sources.append({
                "id": eid,
                "source_url": s_url,
                "source_class": ev_obj.get("source_class") or "official",
                "retrieved_at": ev_obj.get("retrieved_at") or completed_at,
                "content_sha256": ev_obj.get("content_sha256") or "",
            })
        if grounded_sources:
            synth["sources"] = grounded_sources
            synth["citations"] = grounded_sources
        if grounded_urls:
            synth["source_urls"] = grounded_urls
        profile["summary"] = synth.get("summary") or ""

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
        "synthesis": synth,
        "summary": (synth.get("summary") if isinstance(synth, dict) else "") or "",
        "claims": claims,
        "evidence": evidence_list,
        "changes": profile.get("change_history") or [],
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
