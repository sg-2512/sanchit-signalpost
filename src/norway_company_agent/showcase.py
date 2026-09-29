#!/usr/bin/env python3
"""
Signalpost Interactive Showcase Generator.
Produces a self-contained single-page web application matching the official
Product UX & Design reference at builderr.ai/signalpost.
"""
from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional

TEMPLATE_PATH = Path(__file__).resolve().parent / "templates" / "showcase.html"


def observation_meta(item: dict) -> dict:
    return {
        "source": item.get("source_url"),
        "retrievedAt": item.get("retrieved_at"),
        "publishedAt": item.get("published_at") or item.get("date_published"),
        "hash": item.get("content_sha256"),
        "rightsStatus": item.get("rights_status"),
        "sourceClass": item.get("source_class"),
    }


def compact_company_profile(row: dict, external_observations: Optional[List[dict]] = None) -> dict:
    evidence = row.get("evidence", {})
    financial = evidence.get("financials", {})
    financial_history = evidence.get("financial_history", {})
    roles = evidence.get("roles", {})
    locations = evidence.get("locations", {})
    website = evidence.get("website", {})
    website_value = dict(website.get("value") or {})
    if website.get("status") == "available" and not website_value.get("title"):
        website_value["title"] = "Company site fetched"

    identity_assessment = website_value.get("identity_assessment") or {}
    if website.get("status") == "available" and identity_assessment and not identity_assessment.get("publishable"):
        website_value["quarantined_title"] = website_value.get("title")
        website_value["quarantined_description"] = website_value.get("description")
        website_value["quarantined_social_count"] = len(website_value.get("discovered_social_links") or [])
        website_value["title"] = "Registry-linked site — identity not verified"
        website_value["description"] = "Fetched content is retained as discovered evidence but is not attributed to this legal entity."

    live = evidence.get("registry_live", {})
    accounting = evidence.get("accounting_obligation", {})
    external_observations = external_observations or []

    # 1. External LinkedIn & Profile signals
    linkedin_profiles = [
        item for item in external_observations
        if item.get("platform") == "linkedin" and item.get("signal_type") == "profile_metrics" and item.get("exact_entity")
    ]
    linkedin_workforce = [
        item for item in external_observations
        if item.get("platform") == "linkedin" and item.get("signal_type") == "workforce_snapshot" and item.get("exact_entity")
    ]

    # 2. Hiring signals: combine LinkedIn jobs + NAV Arbeidsplassen
    jobs_collected = []
    seen_job_keys = set()
    for item in external_observations:
        if item.get("signal_type") == "job_posting":
            metrics = item.get("metrics") or {}
            title = metrics.get("title") or item.get("title") or item.get("evidence_span")
            if not title:
                continue
            key = (str(title).strip().lower(), str(metrics.get("location") or "").strip().lower())
            if key in seen_job_keys:
                continue
            seen_job_keys.add(key)
            jobs_collected.append({
                "title": title,
                "location": metrics.get("location") or item.get("location") or row.get("municipality") or "Norge",
                "date_posted": item.get("date_published") or item.get("published_at") or (item.get("retrieved_at", "")[:10] if item.get("retrieved_at") else ""),
                "job_url": metrics.get("job_url") or item.get("source_url"),
                "source": item.get("source_url"),
                "sourceClass": item.get("source_class") or "job_board",
                "text": f"{title} — {row.get('name')} — {metrics.get('location') or row.get('municipality') or 'Norge'}",
                **observation_meta(item),
            })

    # 3. Public posts and news mentions
    posts_collected = []
    seen_post_keys = set()
    for item in external_observations:
        if item.get("signal_type") in {"public_post", "public_mention"}:
            metrics = item.get("metrics") or {}
            text = item.get("evidence_span") or metrics.get("headline") or metrics.get("title")
            if not text:
                continue
            key = str(text)[:80].strip().lower()
            if key in seen_post_keys:
                continue
            seen_post_keys.add(key)
            posts_collected.append({
                "text": text,
                "likes": metrics.get("likes", 0),
                "comments": metrics.get("comments", 0),
                "date_posted": item.get("date_published") or item.get("published_at") or (item.get("retrieved_at", "")[:10] if item.get("retrieved_at") else "Recent"),
                "source": item.get("source_url"),
                "sourceClass": item.get("source_class") or "public_media",
                **observation_meta(item),
            })

    # 4. Verified profile handles
    verified_handles = {}
    for item in external_observations:
        if item.get("signal_type") == "profile_handle" and item.get("exact_entity"):
            url = item.get("profile_url") or item.get("source_url")
            platform = item.get("platform")
            if url and platform:
                verified_handles[(platform, url)] = {
                    "platform": platform,
                    "url": url,
                    "rightsStatus": item.get("rights_status", "approved"),
                }

    linkedin_profile = linkedin_profiles[-1] if linkedin_profiles else None
    linkedin_headcount = linkedin_workforce[-1] if linkedin_workforce else None

    external = {
        "handles": list(verified_handles.values()),
        "linkedin": {
            "available": bool(linkedin_profile or jobs_collected or posts_collected),
            "profile": ({**(linkedin_profile.get("metrics") or {}), **observation_meta(linkedin_profile)} if linkedin_profile else {}),
            "workforce": ({**(linkedin_headcount.get("metrics") or {}), **observation_meta(linkedin_headcount)} if linkedin_headcount else {}),
            "posts": posts_collected[:10],
            "jobs": jobs_collected[:10],
        },
    }

    def meta(record: dict) -> dict:
        return {
            "status": record.get("status", "not_run"),
            "source": record.get("source_url"),
            "sourceClass": record.get("source_class") or record.get("source_type"),
            "retrievedAt": record.get("retrieved_at"),
            "effectiveAt": record.get("effective_at") or record.get("as_of"),
            "hash": record.get("content_sha256"),
            "rowKey": record.get("source_row_key"),
        }

    # Ensure location items has HQ fallback if empty
    raw_loc_items = (locations.get("value") or {}).get("locations", [])
    if not raw_loc_items:
        muni = row.get("municipality") or "Norge"
        raw_loc_items = [{
            "organisation_number": row.get("organisation_number"),
            "name": f"{row.get('name')} (Hovedkontor)",
            "address": {
                "kommune": muni,
                "poststed": muni,
                "adresse": [row.get("address", muni)] if row.get("address") else [],
            },
            "employees": row.get("employees"),
        }]

    # Compute 5 coverage dimensions
    company_available = True
    fin_records = (financial.get("value") or {}).get("records", [])
    fin_available = financial.get("status") == "available" and bool(fin_records)
    role_items = (roles.get("value") or {}).get("roles", [])
    people_places_available = bool(role_items) or bool(raw_loc_items)
    web_title = website_value.get("title")
    web_available = website.get("status") == "available" and bool(web_title) and web_title != "Company site fetched"
    footprint_available = bool(
        external.get("handles")
        or external.get("linkedin", {}).get("available")
        or jobs_collected
        or posts_collected
    )

    dimensions = [
        {"key": "company", "label": "Company record", "available": company_available},
        {"key": "financials", "label": "Financials", "available": fin_available},
        {"key": "people_places", "label": "People & locations", "available": people_places_available},
        {"key": "website", "label": "Company website", "available": web_available},
        {"key": "footprint", "label": "Public footprint", "available": footprint_available},
    ]
    coverage_score = sum(1 for d in dimensions if d["available"])
    coverage_label = "Data found in all five categories" if coverage_score == 5 else f"{coverage_score} of 5 data areas"

    richness = (
        coverage_score * 8
        + len(fin_records[:3]) * 4
        + min(len(role_items), 8) * 2
        + min(len(raw_loc_items), 6) * 2
        + len(website_value.get("pages", []))
        + len(external.get("handles", [])) * 2
        + len(posts_collected[:5]) * 2
        + len(jobs_collected[:5]) * 3
    )

    return {
        "org": str(row["organisation_number"]),
        "name": row.get("name", ""),
        "form": row.get("legal_form", ""),
        "employees": row.get("employees"),
        "municipality": row.get("municipality", ""),
        "industryCode": row.get("industry_code", ""),
        "industry": row.get("industry_label", ""),
        "website": row.get("website"),
        "adverse": bool(row.get("bankrupt") or row.get("liquidating")),
        "slice": row.get("sample_slice", "preserved_eligible"),
        "split": row.get("evaluation_split", "development"),
        "latestAccounts": row.get("latest_submitted_accounts"),
        "registrySource": evidence.get("registry", {}).get("source_url") or f"https://data.brreg.no/enhetsregisteret/api/enheter/{row.get('organisation_number')}",
        "registry": meta(evidence.get("registry", {})),
        "accounting": {**meta(accounting), "value": accounting.get("value") or {}},
        "financial": {**meta(financial), "records": fin_records[:3]},
        "financialHistory": {**meta(financial_history), "pdfs": (financial_history.get("value") or {}).get("pdfs", [])},
        "roles": {**meta(roles), "items": role_items[:30]},
        "locations": {**meta(locations), "items": raw_loc_items[:30]},
        "web": {**meta(website), "value": website_value},
        "liveStatus": live.get("status", "available" if row.get("name") else "not_run"),
        "changes": row.get("change_history") or [],
        "external": external,
        "coverage": {
            "score": coverage_score,
            "total": 5,
            "label": coverage_label,
            "dimensions": dimensions,
        },
        "richness": richness,
    }


def render_showcase_html(
    rows: List[dict],
    external_by_org: Optional[Dict[str, List[dict]]] = None,
    limit: Optional[int] = None,
) -> str:
    external_by_org = external_by_org or {}
    compacted = [compact_company_profile(row, external_by_org.get(str(row.get("organisation_number")), [])) for row in rows]
    # Sort with DIPS AS first (if present), then highest coverage, then richness, then name
    compacted.sort(
        key=lambda x: (
            0 if x["name"] == "DIPS AS" else 1,
            -x["coverage"]["score"],
            -x["richness"],
            x["name"],
        )
    )
    if limit and limit > 0:
        compacted = compacted[:limit]

    payload = json.dumps(compacted, ensure_ascii=False).replace("</", "<\\/")
    template = TEMPLATE_PATH.read_text(encoding="utf-8")
    return template.replace("__DATA_PAYLOAD__", payload)


def write_showcase_html(
    profiles: List[dict],
    observations: Optional[List[dict]] = None,
    output_path: Optional[Path] = None,
    limit: Optional[int] = None,
) -> Path:
    if output_path is None:
        output_path = Path("out/showcase.html")
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    external_by_org: Dict[str, List[dict]] = defaultdict(list)
    seen_ids = set()
    for obs in observations or []:
        obs_id = str(obs.get("id") or json.dumps(obs, sort_keys=True, ensure_ascii=False))
        if obs_id in seen_ids:
            continue
        seen_ids.add(obs_id)
        org = str(obs.get("organisation_number") or "")
        if org:
            external_by_org[org].append(obs)

    html_content = render_showcase_html(profiles, external_by_org, limit=limit)
    output_path.write_text(html_content, encoding="utf-8")
    return output_path
