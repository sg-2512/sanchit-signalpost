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

    # 2. Hiring signals: combine LinkedIn jobs + NAV Arbeidsplassen + website jobs
    jobs_collected = []
    seen_job_keys = set()
    for item in external_observations:
        if item.get("signal_type") == "job_posting":
            metrics = item.get("metrics") or {}
            title = metrics.get("title") or metrics.get("job_title") or item.get("title") or item.get("job_title") or item.get("evidence_span")
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
                "job_url": metrics.get("job_url") or item.get("url") or item.get("source_url"),
                "source": item.get("source_url"),
                "sourceClass": item.get("source_class") or "job_board",
                "text": f"{title} — {row.get('name')} — {metrics.get('location') or row.get('municipality') or 'Norge'}",
                **observation_meta(item),
            })

    # 3. Public posts and news mentions
    posts_collected = []
    seen_post_keys = set()
    for item in external_observations:
        if item.get("signal_type") in {"public_post", "public_mention", "dated_news"}:
            metrics = item.get("metrics") or {}
            text = item.get("evidence_span") or metrics.get("headline") or metrics.get("title") or item.get("title")
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

    # 5. Patentstyret IP & Trademarks
    patents_collected = []
    seen_patents = set()
    for item in external_observations:
        if item.get("platform") == "patentstyret" or item.get("signal_type") == "patent_trademark_record":
            metrics = item.get("metrics") or {}
            title = metrics.get("title") or item.get("title") or ""
            app_num = metrics.get("application_number") or ""
            ip_type = metrics.get("ip_type") or "trademark"
            status = metrics.get("status") or "Registrert"
            key = (ip_type, app_num, str(title).lower())
            if key in seen_patents:
                continue
            seen_patents.add(key)
            patents_collected.append({
                "type": ip_type,
                "title": title,
                "application_number": app_num,
                "status": status,
                "filing_date": metrics.get("filing_date") or item.get("published_at"),
                "case_url": item.get("source_url"),
                "source": item.get("source_url"),
                **observation_meta(item),
            })

    for pat in (row.get("patents") or []):
        title = pat.get("title") or ""
        app_num = pat.get("application_number") or ""
        ip_type = pat.get("ip_type") or pat.get("type") or "trademark"
        status = pat.get("status") or "Registrert"
        key = (ip_type, app_num, str(title).lower())
        if key not in seen_patents and (title or app_num):
            seen_patents.add(key)
            patents_collected.append({
                "type": ip_type,
                "title": title,
                "application_number": app_num,
                "status": status,
                "filing_date": pat.get("filing_date") or pat.get("publicationDate"),
                "case_url": pat.get("case_url") or (f"https://services.patentstyret.no/search-details/{ip_type}/{app_num}" if app_num else "https://www.patentstyret.no"),
                "source": pat.get("source_url") or "https://www.patentstyret.no",
            })

    linkedin_profile = linkedin_profiles[-1] if linkedin_profiles else None
    linkedin_headcount = linkedin_workforce[-1] if linkedin_workforce else None

    # 6. Doffin Public Procurement
    procurement_collected = []
    seen_procurement = set()
    for item in external_observations:
        if item.get("platform") == "doffin" or item.get("signal_type") == "procurement_notice":
            metrics = item.get("metrics") or {}
            nid = metrics.get("notice_id") or ""
            title = metrics.get("title") or item.get("title") or ""
            key = (nid, str(title).lower())
            if key in seen_procurement:
                continue
            seen_procurement.add(key)
            procurement_collected.append({
                "notice_id": nid,
                "title": title,
                "status": metrics.get("status") or "Published",
                "published_date": metrics.get("published_date") or item.get("published_at"),
                "source": item.get("source_url") or metrics.get("url"),
                **observation_meta(item),
            })
    for p in (row.get("procurement") or []):
        nid = p.get("notice_id") or ""
        title = p.get("title") or ""
        key = (nid, str(title).lower())
        if key not in seen_procurement and (title or nid):
            seen_procurement.add(key)
            procurement_collected.append({
                "notice_id": nid,
                "title": title,
                "status": p.get("status") or "Published",
                "published_date": p.get("published_date") or p.get("publication_date"),
                "source": p.get("url") or f"https://doffin.no/Notice/Details/{nid}",
            })

    # 7. Mattilsynet Smilefjes (Food Safety)
    food_safety_collected = []
    seen_food = set()
    for item in external_observations:
        if item.get("platform") == "mattilsynet" or item.get("signal_type") == "food_safety_inspection":
            metrics = item.get("metrics") or {}
            prem = metrics.get("premises_name") or metrics.get("location_name") or ""
            dt = metrics.get("inspection_date") or metrics.get("date") or ""
            grade_val = metrics.get("total_grade") if metrics.get("total_grade") is not None else metrics.get("grade", 0)
            key = (prem, dt)
            if key in seen_food:
                continue
            seen_food.add(key)
            food_safety_collected.append({
                "premises_name": prem,
                "total_grade": int(grade_val) if str(grade_val).isdigit() else 0,
                "inspection_date": dt,
                "status": metrics.get("status") or "Inspected",
                "source": item.get("source_url") or metrics.get("url"),
                **observation_meta(item),
            })
    for f in (row.get("food_safety") or []):
        prem = f.get("premises_name") or f.get("location_name") or ""
        dt = f.get("inspection_date") or f.get("date") or ""
        grade_val = f.get("total_grade") if f.get("total_grade") is not None else f.get("grade", 0)
        key = (prem, dt)
        if key not in seen_food and (prem or dt):
            seen_food.add(key)
            food_safety_collected.append({
                "premises_name": prem,
                "total_grade": int(grade_val) if str(grade_val).isdigit() else 0,
                "inspection_date": dt,
                "status": f.get("status") or "Inspected",
                "source": f.get("url") or "https://smilefjes.mattilsynet.no",
            })

    # 8. Regulatory Licenses (Arbeidstilsynet & Finanstilsynet)
    regulatory_licenses_collected = []
    seen_lic = set()
    for item in external_observations:
        if item.get("platform") in {"arbeidstilsynet", "finanstilsynet"} or item.get("signal_type") == "regulatory_license":
            metrics = item.get("metrics") or {}
            reg = metrics.get("registry") or item.get("platform") or ""
            lic_t = metrics.get("license_type") or ""
            key = (reg, lic_t)
            if key in seen_lic:
                continue
            seen_lic.add(key)
            regulatory_licenses_collected.append({
                "registry": reg,
                "license_type": lic_t,
                "status": metrics.get("status") or "Active",
                "identifier": metrics.get("identifier"),
                "source": item.get("source_url") or metrics.get("url"),
                **observation_meta(item),
            })
    for l in (row.get("regulatory_licenses") or []):
        reg = l.get("registry") or ""
        lic_t = l.get("license_type") or ""
        key = (reg, lic_t)
        if key not in seen_lic and (reg or lic_t):
            seen_lic.add(key)
            regulatory_licenses_collected.append({
                "registry": reg,
                "license_type": lic_t,
                "status": l.get("status") or "Active",
                "identifier": l.get("identifier"),
                "source": l.get("url") or "https://www.arbeidstilsynet.no",
            })

    # 9. ESG & Miljøfyrtårn Certifications
    esg_collected = []
    seen_esg = set()
    for item in external_observations:
        if item.get("platform") == "miljofyrtarn" or item.get("signal_type") == "esg_certification":
            metrics = item.get("metrics") or {}
            c_name = metrics.get("certificate_name") or ""
            c_body = metrics.get("certification_body") or "Miljøfyrtårn"
            key = (c_body, c_name)
            if key in seen_esg:
                continue
            seen_esg.add(key)
            esg_collected.append({
                "certification_body": c_body,
                "certificate_name": c_name,
                "status": metrics.get("status") or "Certified",
                "valid_until": metrics.get("valid_until"),
                "source": item.get("source_url") or metrics.get("url"),
                **observation_meta(item),
            })
    for e in (row.get("esg") or []):
        c_name = e.get("certificate_name") or ""
        c_body = e.get("certification_body") or "Miljøfyrtårn"
        key = (c_body, c_name)
        if key not in seen_esg and (c_body or c_name):
            seen_esg.add(key)
            esg_collected.append({
                "certification_body": c_body,
                "certificate_name": c_name,
                "status": e.get("status") or "Certified",
                "valid_until": e.get("valid_until"),
                "source": e.get("url") or "https://www.miljofyrtarn.no",
            })

    # 10. Norid Domain Verification
    norid_verified = row.get("norid_verification") or {}
    for item in external_observations:
        if item.get("platform") == "norid" or item.get("signal_type") == "legal_domain_ownership":
            norid_verified = {**(item.get("metrics") or {}), "exact_entity": item.get("exact_entity", True)}
            break

    # 11. Løsøreregisteret (Mortgages & Liens)
    mortgages_collected = []
    seen_mortgages = set()
    for item in external_observations:
        if item.get("platform") == "losoreregisteret" or item.get("signal_type") == "registered_mortgage_lien":
            metrics = item.get("metrics") or {}
            doc_id = metrics.get("document_id") or ""
            m_type = metrics.get("mortgage_type") or ""
            key = (doc_id, m_type)
            if key in seen_mortgages:
                continue
            seen_mortgages.add(key)
            mortgages_collected.append({
                "document_id": doc_id,
                "mortgage_type": m_type,
                "amount": metrics.get("amount"),
                "currency": metrics.get("currency") or "NOK",
                "secured_party": metrics.get("secured_party"),
                "registered_date": metrics.get("registered_date"),
                "source": item.get("source_url"),
                **observation_meta(item),
            })
    for m in (row.get("mortgages") or []):
        doc_id = m.get("document_id") or ""
        m_type = m.get("mortgage_type") or ""
        key = (doc_id, m_type)
        if key not in seen_mortgages and (doc_id or m_type):
            seen_mortgages.add(key)
            mortgages_collected.append(m)

    # 12. Public Grants (Forskningsrådet & Innovasjon Norge)
    grants_collected = []
    seen_grants = set()
    for item in external_observations:
        if item.get("platform") in {"forskningsradet", "innovasjon_norge"} or item.get("signal_type") in {"public_rnd_grant", "innovation_grant_award"}:
            metrics = item.get("metrics") or {}
            title = metrics.get("title") or metrics.get("project_name") or ""
            prog = metrics.get("funding_scheme") or metrics.get("scheme") or item.get("platform") or ""
            key = (prog, title)
            if key in seen_grants:
                continue
            seen_grants.add(key)
            grants_collected.append({
                "source_agency": "Forskningsrådet" if item.get("platform") == "forskningsradet" else "Innovasjon Norge",
                "project_title": title,
                "program": prog,
                "amount_nok": metrics.get("amount_nok"),
                "year": metrics.get("start_year") or metrics.get("year"),
                "source": item.get("source_url"),
                **observation_meta(item),
            })
    for g in (row.get("rnd_grants") or []):
        title = g.get("title") or ""
        prog = g.get("funding_scheme") or "Forskningsrådet"
        key = (prog, title)
        if key not in seen_grants and title:
            seen_grants.add(key)
            grants_collected.append({
                "source_agency": "Forskningsrådet",
                "project_title": title,
                "program": prog,
                "amount_nok": g.get("amount_nok"),
                "year": g.get("start_year"),
            })
    for g in (row.get("innovation_grants") or []):
        title = g.get("project_name") or ""
        prog = g.get("scheme") or "Innovasjon Norge"
        key = (prog, title)
        if key not in seen_grants and title:
            seen_grants.add(key)
            grants_collected.append({
                "source_agency": "Innovasjon Norge",
                "project_title": title,
                "program": prog,
                "amount_nok": g.get("amount_nok"),
                "year": g.get("year"),
            })

    # 13. Oslo Børs Listed Disclosures
    stock_disclosures = []
    seen_bors = set()
    for item in external_observations:
        if item.get("platform") == "oslo_bors" or item.get("signal_type") == "stock_exchange_disclosure":
            metrics = item.get("metrics") or {}
            mid = metrics.get("message_id") or ""
            btitle = metrics.get("title") or ""
            key = (mid, btitle)
            if key in seen_bors:
                continue
            seen_bors.add(key)
            stock_disclosures.append({
                "message_id": mid,
                "title": btitle,
                "ticker": metrics.get("ticker"),
                "market": metrics.get("market"),
                "published_at": metrics.get("published_at"),
                "source": item.get("source_url"),
                **observation_meta(item),
            })
    for b in (row.get("stock_exchange") or []):
        mid = b.get("message_id") or ""
        btitle = b.get("title") or ""
        key = (mid, btitle)
        if key not in seen_bors and (mid or btitle):
            seen_bors.add(key)
            stock_disclosures.append(b)

    # 14. Macro Benchmark & LEI & Environmental Permits
    industry_benchmark = row.get("industry_benchmark") or {}
    lei_info = row.get("lei") or {}
    env_permits = row.get("environmental_permits") or []
    for item in external_observations:
        if item.get("platform") == "ssb" and not industry_benchmark:
            industry_benchmark = item.get("metrics") or {}
        elif item.get("platform") == "gleif" and not lei_info:
            lei_info = item.get("metrics") or {}
        elif item.get("platform") == "miljodirektoratet":
            m_p = item.get("metrics") or {}
            if m_p and m_p not in env_permits:
                env_permits.append(m_p)

    external = {
        "handles": list(verified_handles.values()),
        "patents": patents_collected,
        "procurement": procurement_collected,
        "food_safety": food_safety_collected,
        "regulatory_licenses": regulatory_licenses_collected,
        "esg": esg_collected,
        "norid": norid_verified,
        "mortgages": mortgages_collected,
        "grants": grants_collected,
        "stock_exchange": stock_disclosures,
        "industry_benchmark": industry_benchmark,
        "lei": lei_info,
        "environmental_permits": env_permits,
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
        or patents_collected
        or procurement_collected
        or food_safety_collected
        or regulatory_licenses_collected
        or esg_collected
        or mortgages_collected
        or grants_collected
        or stock_disclosures
        or env_permits
        or norid_verified.get("verified_ownership")
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
        + len(patents_collected[:5]) * 3
        + len(procurement_collected[:5]) * 3
        + len(food_safety_collected[:5]) * 2
        + len(regulatory_licenses_collected[:5]) * 2
        + len(esg_collected[:5]) * 3
        + len(mortgages_collected[:5]) * 2
        + len(grants_collected[:5]) * 3
        + len(stock_disclosures[:5]) * 3
        + (3 if norid_verified.get("verified_ownership") else 0)
        + (2 if lei_info.get("lei") else 0)
        + len(env_permits[:3]) * 2
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
        "financial": {**meta(financial), "records": fin_records[:5] if len(fin_records) > 3 else fin_records[:3], "trends": (financial.get("value") or {}).get("financial_trends") or {}},
        "financialHistory": {**meta(financial_history), "pdfs": (financial_history.get("value") or {}).get("pdfs", [])},
        "roles": {**meta(roles), "items": role_items[:30]},
        "locations": {**meta(locations), "items": raw_loc_items[:30]},
        "web": {**meta(website), "value": website_value},
        "liveStatus": live.get("status", "available" if row.get("name") else "not_run"),
        "changes": row.get("change_history") or [],
        "external": external,
        "synthesis": row.get("synthesis") or {},
        "hiring": row.get("hiring") or {},
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
