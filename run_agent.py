#!/usr/bin/env python3
"""SignalPost Live Agent — Daily Evaluation Entry Point.

This is the single command the evaluator runs:
    uv run python run_agent.py --organisations input.jsonl --bulk brreg-enheter.csv --output-dir out/daily

Designed to process 100 random companies within:
- 45 minutes wall clock
- 2,000 total outbound requests
- $10 maximum API spend
- 8 vCPU, 16 GB RAM
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))


def _load_env_file() -> None:
    """Load key-value pairs from .env if present."""
    env_file = ROOT / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, value = line.partition("=")
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                if key and value and key not in os.environ:
                    os.environ[key] = value


import socket
socket.setdefaulttimeout(15.0)

from norway_company_agent.batch import (
    profiles_from_bulk,
    read_organisation_inputs,
    terminal_envelope,
    validate_envelopes,
)
from norway_company_agent.budget import BudgetTracker
from norway_company_agent.evidence import observation_provenance, utc_now
from norway_company_agent.external_footprint import (
    aggregate_footprint,
    INDEPENDENT_SENTIMENT_CLASSES,
    publishable_observation,
    validate_observation,
)
from norway_company_agent.source_policy import (
    EXPERIMENTAL,
    OFFICIAL_SAFE,
    filter_publishable_observations,
    validate_policy_name,
)
from norway_company_agent.identity import apply_entity_identity_gate, published_website_url
from norway_company_agent.official import fetch_official_modules
from norway_company_agent.website import fetch_website, normalize_homepage, norwegian_presence_path
from norway_company_agent.verified_items import first_party_news_about_company, name_in_text, validate_job_item, validate_news_item
from norway_company_agent.discovery import (
    candidate_site_origin,
    discover_company_websites_keyless,
    extract_email_domain_candidate,
    keyless_search_available,
    probe_heuristic_domain,
)

# External connectors (activate based on env vars)
from norway_company_agent.connectors.google_news import fetch_google_news
from norway_company_agent.connectors.press_wire import fetch_press_wire_news
from norway_company_agent.connectors.social_discovery import fetch_social_profiles
from norway_company_agent.connectors.google_places import (
    fetch_place_data,
    is_available as places_available,
    connector_status as places_status,
)
from norway_company_agent.connectors.youtube import (
    fetch_youtube_data,
    fetch_linked_youtube_data,
    is_available as youtube_available,
)
from norway_company_agent.connectors.linkedin import (
    discover_linkedin_company,
    is_available as linkedin_available,
)
from norway_company_agent.connectors.wikidata import fetch_wikidata_entity
from norway_company_agent.connectors.subunits import fetch_company_subunits
from norway_company_agent.connectors.patentstyret import (
    fetch_patentstyret_data,
    is_available as patentstyret_available,
)
from norway_company_agent.connectors.doffin import fetch_doffin_procurement
from norway_company_agent.connectors.kartverket import geocode_address_kartverket, matches_registered_address
from norway_company_agent.connectors.regulatory_licenses import fetch_regulatory_licenses
from norway_company_agent.connectors.dibk import fetch_construction_approval
from norway_company_agent.connectors.scoped_company_sources import assess_norwegian_presence, fetch_company_press_release, fetch_norwegian_presence, fetch_scoped_newsroom
from norway_company_agent.connectors.esg_certifications import fetch_esg_certifications
from norway_company_agent.connectors.ats_discovery import verified_career_jobs
from norway_company_agent.connectors.norid import verify_norid_domain_ownership, discover_norid_domain
from norway_company_agent.connectors.innovasjon_norge import fetch_innovasjon_norge_grants
from norway_company_agent.connectors.oslo_bors import fetch_oslo_bors_disclosures
from norway_company_agent.connectors.gleif import fetch_gleif_identifier
from norway_company_agent.hiring import evaluate_company_hiring
from norway_company_agent.connectors.nav_feed import flush_nav_feed_details, nav_feed_jobs_for_company, reset_nav_feed


def apply_live_registry_precedence(profile: dict[str, Any], registry_record: dict[str, Any]) -> None:
    """Keep current identity fields in step with a successful live Brreg response."""
    if registry_record.get("status") != "available" or registry_record.get("source_type") != "official_registry_live":
        return
    value = registry_record.get("value") or {}
    if not isinstance(value, dict):
        return
    current_name = value.get("name")
    if current_name and current_name != profile.get("name"):
        previous = str(profile.get("name") or "")
        if previous:
            former_names = profile.setdefault("former_names", [])
            if previous not in former_names:
                former_names.append(previous)
        profile["name"] = current_name
    former_names = profile.setdefault("former_names", [])
    for entry in value.get("historiskeNavn") or []:
        historic_name = str(entry.get("navn") or "").strip() if isinstance(entry, dict) else ""
        if historic_name and historic_name != profile.get("name") and historic_name not in former_names:
            former_names.append(historic_name)
    if value.get("employees") is not None:
        profile["employees"] = value["employees"]
    if value.get("legal_form"):
        profile["legal_form"] = value["legal_form"]


def merge_verified_jobs(site_jobs: list[dict[str, Any]], ats_jobs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep validated first-party ads when a later ATS lookup finds none."""
    merged = list(site_jobs)
    seen = {str(item.get("url") or "").split("#", 1)[0].rstrip("/") for item in merged}
    for job in ats_jobs:
        url = str(job.get("url") or "").split("#", 1)[0].rstrip("/")
        if url and url not in seen:
            merged.append(job)
            seen.add(url)
    return merged


def write_jsonl(path: Path, rows: list[dict]) -> None:
    """Atomic JSONL write with tmp file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8", newline="\n") as f:
        for row in rows:
            encoded = json.dumps(row, ensure_ascii=False, separators=(",", ":"))
            for separator in ("\x85", "\u2028", "\u2029"):
                encoded = encoded.replace(separator, f"\\u{ord(separator):04x}")
            f.write(encoded + "\n")
    tmp.replace(path)


def extract_social_observations(profile: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract social handle observations from a profile's verified website."""
    org = str(profile.get("organisation_number") or "")
    website = (profile.get("evidence") or {}).get("website") or {}
    value = website.get("value") or {}
    identity = value.get("identity_assessment") or {}

    if website.get("status") != "available" or not identity.get("publishable"):
        return []

    site_url = str(value.get("final_url") or website.get("source_url") or "")
    retrieved_at = website.get("retrieved_at") or ""
    social_links = value.get("social_links") or []

    observations = []
    seen: set[tuple[str, str]] = set()
    for link in social_links:
        platform = str(link.get("platform") or "").casefold()
        url = str(link.get("url") or "").strip()
        if not url or platform not in {"linkedin", "facebook", "instagram", "youtube", "x", "tiktok"}:
            continue
        from urllib.parse import urlsplit
        parsed_url = urlsplit(url)
        key = (platform, (parsed_url.hostname or "").casefold().removeprefix("www.") + parsed_url.path.casefold().rstrip("/"))
        if key in seen:
            continue
        seen.add(key)
        proof_url = str(link.get("source_url") or site_url)
        site_sha = str(link.get("content_sha256") or "")
        if not site_sha:
            continue
        obs_id = f"social-{platform}-{org}-{hashlib.sha256(url.encode()).hexdigest()[:16]}"
        observations.append({
            "id": obs_id,
            "organisation_number": org,
            "platform": platform,
            "signal_type": "profile_handle",
            "source_url": proof_url,
            "retrieved_at": retrieved_at,
            "content_sha256": site_sha,
            "exact_entity": True,
            "identity_proof": [
                {"type": "website_identity_gate", "score": identity.get("score"), "method": identity.get("method")},
                {"type": "website_outbound_social_link", "parent_website": proof_url},
            ],
            "acquisition_mode": "permitted_public_page",
            "rights_status": "approved",
            "source_class": "company_site",
            "evidence_span": f"Verified company website {proof_url} links to a {platform} brand channel: {url}",
            "metrics": {"platform": platform, "url": url, "relationship_scope": "company_site_linked_brand_channel"},
            "strategy": "website_social_discovery",
        })
    return observations


def extract_website_news_observations(profile: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract dated news observations from verified company website."""
    org = str(profile.get("organisation_number") or "")
    website = (profile.get("evidence") or {}).get("website") or {}
    value = website.get("value") or {}
    identity = value.get("identity_assessment") or {}

    if website.get("status") != "available" or not identity.get("publishable"):
        return []

    news_items = list(value.get("news_items") or [])
    if not news_items and value.get("pages"):
        for page in value.get("pages", []):
            if page.get("news_items"):
                news_items.extend(page["news_items"])

    if not news_items:
        return []

    site_url = str(value.get("final_url") or website.get("source_url") or "")
    retrieved_at = website.get("retrieved_at") or utc_now()
    observations = []
    seen: set[str] = set()

    for item in sorted(news_items, key=lambda article: {"no": 0, "nb": 0, "nn": 0, "en": 1}.get(
            str(article.get("article_language") or "").lower().split("-", 1)[0], 2)):
        title = str(item.get("title") or "").strip()
        url = str(item.get("url") or site_url).strip()
        evidence_url = str(item.get("source_url") or item.get("listing_url") or url).strip()
        if not title or len(title) < 5:
            continue
        verification = str(item.get("verification") or "site_listing")
        article_text = (
            str(item.get("excerpt") or "") if verification == "inline_newsroom"
            else str(item.get("article_text_excerpt") or "")
        )
        name_in_article = first_party_news_about_company(profile.get("name") or "", title, article_text)
        valid, _ = validate_news_item(
            {**item, "url": url, "verification": verification,
             "name_in_article": name_in_article},
            company_name=profile.get("name") or "", site_url=site_url,
        )
        if not valid:
            continue
        key = item.get("story_key") or url.split("#", 1)[0].rstrip("/")
        if key in seen:
            continue
        seen.add(key)

        published_at = item.get("published_at")
        # An article claim must carry the hash of the article actually read.
        # A homepage or synthetic hash cannot establish its contents.
        digest = str(item.get("content_sha256") or "")
        obs_id = "news-web-" + hashlib.sha256(f"{org}|{url}|{title}".encode()).hexdigest()[:24]

        observations.append({
            "id": obs_id,
            "organisation_number": org,
            "platform": "news",
            "signal_type": "dated_news",
            "source_url": evidence_url,
            "retrieved_at": retrieved_at,
            "published_at": published_at,
            "content_sha256": digest,
            "exact_entity": True,
            "verification": verification,
            "name_in_article": name_in_article,
            "article_url": url,
            "identity_proof": [
                {"type": "website_identity_gate", "score": identity.get("score"), "method": identity.get("method")},
                {"type": "company_website_publication", "parent_website": site_url},
            ],
            "acquisition_mode": "permitted_public_page",
            "rights_status": "approved",
            "source_class": "company_site",
            "evidence_span": article_text[:500],
            "literal_excerpt": article_text[:500],
            "text": title,
            "metrics": {"title": title, "url": url, "published_at": published_at},
            "strategy": "website_news_discovery",
        })
    return observations


def extract_website_job_observations(profile: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract job vacancy observations from verified company career pages."""
    org = str(profile.get("organisation_number") or "")
    website = (profile.get("evidence") or {}).get("website") or {}
    value = website.get("value") or {}
    identity = value.get("identity_assessment") or {}

    if website.get("status") != "available" or not identity.get("publishable"):
        return []

    job_postings = list(value.get("verified_job_adverts") or []) + list(value.get("job_postings") or [])
    if not job_postings and value.get("pages"):
        for page in value.get("pages", []):
            if page.get("job_postings"):
                job_postings.extend(page["job_postings"])
    if not job_postings:
        return []

    site_url = str(value.get("final_url") or website.get("source_url") or "")
    retrieved_at = website.get("retrieved_at") or utc_now()
    observations = []
    seen: set[tuple[str, str]] = set()
    locations = (((profile.get("evidence") or {}).get("locations") or {}).get("value") or {}).get("locations") or []
    subunit_orgs = [str(row["organisation_number"]) for row in locations
                    if isinstance(row, dict) and row.get("organisation_number")]

    for item in job_postings:
        title = str(item.get("title") or "Ledig stilling").strip()
        url = str(item.get("url") or site_url).strip()
        if item.get("requires_ad_verification"):
            continue
        if item.get("kind") in {"company_site_ad", "verified_career_ad"} and not validate_job_item(
            item, org=org, company_name=profile.get("name") or "", site_url=site_url,
            site_publishable=True, subunit_orgs=subunit_orgs,
        )[0]:
            continue
        key = (title.casefold(), url.casefold())
        if key in seen:
            continue
        seen.add(key)

        published_at = item.get("published_at")
        digest = str(item.get("content_sha256") or "")
        obs_id = "jobs-web-" + hashlib.sha256(f"{org}|{url}|{title}".encode()).hexdigest()[:24]

        observations.append({
            "id": obs_id,
            "organisation_number": org,
            "platform": "job_board",
            "signal_type": "job_posting",
            "source_url": url,
            "retrieved_at": retrieved_at,
            "published_at": published_at,
            "content_sha256": digest,
            "exact_entity": True,
            "kind": item.get("kind"),
            "employer_name": item.get("employer_name"),
            "employer_org": item.get("employer_org"),
            "hiring_org_match": item.get("hiring_org_match"),
            "valid_through": item.get("valid_through"),
            "application_due": item.get("application_due"),
            "career_source_url": item.get("career_source_url"),
            "career_link_url": item.get("career_link_url"),
            "active_listing": item.get("active_listing"),
            "ad_text_excerpt": item.get("ad_text_excerpt"),
            "identity_proof": [
                {"type": "website_identity_gate", "score": identity.get("score"), "method": identity.get("method")},
                {"type": "company_career_page", "source_url": item.get("career_source_url"), "linked_url": item.get("career_link_url")},
                {"type": "job_advert_employer", "name": item.get("employer_name"), "organisation_number": item.get("employer_org")},
            ],
            "acquisition_mode": "permitted_public_page",
            "rights_status": "approved",
            "source_class": "company_site",
            "job_title": title,
            "evidence_span": str(item.get("ad_text_excerpt") or title)[:500],
            "literal_excerpt": str(item.get("ad_text_excerpt") or "")[:500] or None,
            "text": title,
            "metrics": {"job_title": title, "url": url, "published_at": published_at},
            "strategy": "company_site_job",
        })
    return observations


def extract_workforce_observation(profile: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract official workforce snapshot from BRREG registry data."""
    org = str(profile.get("organisation_number") or "")
    ev = profile.get("evidence") or {}
    live_rec = ev.get("registry_live") or {}
    bulk_rec = ev.get("registry") or {}
    reg_live = live_rec.get("value") or {}
    reg_bulk = bulk_rec.get("value") or {}
    if live_rec.get("status") == "available" and reg_live.get("employees") is not None:
        employees, source_rec = reg_live["employees"], live_rec
    elif bulk_rec.get("status") == "available" and reg_bulk.get("antallAnsatte") is not None:
        employees, source_rec = reg_bulk["antallAnsatte"], bulk_rec
    else:
        return []

    try:
        count = int(employees)
    except (ValueError, TypeError):
        return []

    source_url = str(source_rec.get("source_url") or "")
    retrieved_at = source_rec.get("retrieved_at") or ""
    digest = str(source_rec.get("content_sha256") or "")
    if not source_url or not digest:
        return []
    obs_id = f"workforce-brreg-{org}-{hashlib.sha256(str(count).encode()).hexdigest()[:16]}"

    return [{
        "id": obs_id,
        "organisation_number": org,
        "platform": "brreg",
        "signal_type": "workforce_snapshot",
        "source_url": source_url,
        "retrieved_at": retrieved_at,
        "content_sha256": digest,
        "exact_entity": True,
        "identity_proof": [{"type": "official_registry_workforce_record", "organisation_number": org, "employees": count}],
        "acquisition_mode": "official_api" if source_rec is live_rec else "official_bulk_file",
        "rights_status": "approved",
        "source_class": source_rec.get("source_type") or "official_registry",
        "evidence_span": f"Official Brønnøysund registry reports {count} registered employee(s) for organization {org}",
        "metrics": {"workforce_value": count, "measure": "employees"},
        "strategy": "official_registry_workforce",
    }]


def extract_profile_metrics_observation(profile: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract company profile metrics from BRREG registry data."""
    org = str(profile.get("organisation_number") or "")
    ev = profile.get("evidence") or {}
    reg = (ev.get("registry") or {}).get("value") or {}
    name = str(profile.get("name") or reg.get("navn") or "")
    form = str(profile.get("legal_form") or reg.get("organisasjonsform.kode") or "AS")
    muni = str(profile.get("municipality") or reg.get("forretningsadresse.kommune") or "")
    live_rec = ev.get("registry_live") or {}
    bulk_rec = ev.get("registry") or {}
    source_rec = live_rec if live_rec.get("status") == "available" else bulk_rec
    reg_val = source_rec.get("value") or {}
    latest = str(reg_val.get("latest_submitted_accounts") or reg_val.get("sisteInnsendteAarsregnskap") or "").strip()
    source_url = str(source_rec.get("source_url") or "")
    retrieved_at = source_rec.get("retrieved_at") or ""
    digest = str(source_rec.get("content_sha256") or "")
    if not source_url or not digest:
        return []
    obs_id = f"metrics-brreg-{org}-{hashlib.sha256(f'{form}|{latest}'.encode()).hexdigest()[:16]}"

    return [{
        "id": obs_id,
        "organisation_number": org,
        "platform": "brreg",
        "signal_type": "profile_metrics",
        "source_url": source_url,
        "retrieved_at": retrieved_at,
        "content_sha256": digest,
        "exact_entity": True,
        "identity_proof": [{"type": "official_registry_entity", "organisation_number": org, "name": name}],
        "acquisition_mode": "official_api" if source_rec is live_rec else "official_bulk_file",
        "rights_status": "approved",
        "source_class": source_rec.get("source_type") or "official_registry",
        "evidence_span": f"Official Norwegian corporate profile: {name} ({form}) in {muni}, latest filed accounts: {latest or 'unreported'}",
        "metrics": {"legal_form": form, "municipality": muni, "latest_accounts": latest},
        "strategy": "official_registry_metrics",
    }]


def extract_place_observations(profile: dict[str, Any]) -> list[dict[str, Any]]:
    """Keep the registered office separate from BRREG operating subunits."""
    org = str(profile.get("organisation_number") or "")
    ev = profile.get("evidence") or {}
    observations = []
    address_rec = ev.get("registered_address") or {}
    address_value = address_rec.get("value") or {}
    address = address_value.get("address") or {}
    address_sha = str(address_rec.get("content_sha256") or "")
    if address_rec.get("status") == "available" and re.fullmatch(r"[0-9a-fA-F]{64}", address_sha) and address:
        observations.append({
            "id": f"address-brreg-{org}-{address_sha[:16]}",
            "organisation_number": org,
            "platform": "brreg",
            "signal_type": "registered_address",
            "source_url": address_rec.get("source_url"),
            "retrieved_at": address_rec.get("retrieved_at"),
            "content_sha256": address_sha,
            "exact_entity": True,
            "identity_proof": [{"type": "official_registry_address", "organisation_number": org,
                                "address": address, "source_url": address_rec.get("source_url")}],
            "acquisition_mode": "official_api",
            "rights_status": "approved",
            "source_class": "official_registry",
            "evidence_span": f"BRREG lists the registered business address for {profile.get('name')} ({org}): {address}",
            "metrics": {"address": address, "relationship_scope": "registered_business_address"},
            "strategy": "official_registered_address",
        })
    locs = ((ev.get("locations") or {}).get("value") or {}).get("locations") or []
    location_rec = ev.get("locations") or {}
    location_sha = str(location_rec.get("content_sha256") or "")
    if location_rec.get("status") != "available" or not re.fullmatch(r"[0-9a-fA-F]{64}", location_sha):
        return observations
    source_url = location_rec.get("source_url") or f"https://data.brreg.no/enhetsregisteret/api/underenheter?overordnetEnhet={org}&size=1000"
    retrieved_at = location_rec.get("retrieved_at") or ""
    for loc in locs:
        loc_org = str(loc.get("organisation_number") or "")
        if not re.fullmatch(r"[0-9]{9}", loc_org) or loc_org == org:
            continue
        loc_name = loc.get("name") or profile.get("name")
        addr = loc.get("address") or {}
        kommune = addr.get("kommune") or ""
        poststed = addr.get("poststed") or ""
        obs_id = f"place-brreg-{org}-{hashlib.sha256(str(loc_org).encode()).hexdigest()[:16]}"
        observations.append({
            "id": obs_id,
            "organisation_number": org,
            "platform": "brreg",
            "signal_type": "place_summary",
            "source_url": source_url,
            "retrieved_at": retrieved_at,
            "content_sha256": location_sha,
            "exact_entity": True,
            "identity_proof": [{"type": "official_subunit_record", "parent_org": org,
                                "subunit_org": loc_org, "name": loc_name, "source_url": source_url}],
            "acquisition_mode": "official_api",
            "rights_status": "approved",
            "source_class": "official_subunits",
            "evidence_span": f"BRREG lists subunit {loc_org} ({loc_name}) for {org} in {kommune} ({poststed}).",
            "metrics": {"place_name": loc_name, "municipality": kommune, "subunit_organisation_number": loc_org},
            "strategy": "official_subunits_places",
        })
    return observations


def extract_notice_observation(profile: dict[str, Any]) -> list[dict[str, Any]]:
    """Record a dated registry milestone without inventing a public notice."""
    org = str(profile.get("organisation_number") or "")
    ev = profile.get("evidence") or {}
    live_rec = ev.get("registry_live") or {}
    bulk_rec = ev.get("registry") or {}
    source_rec = live_rec if live_rec.get("status") == "available" else bulk_rec
    reg = source_rec.get("value") or {}
    reg_date = str(reg.get("registreringsdatoEnhetsregisteret") or reg.get("registreringsdatoenhetsregisteret") or "")[:10]
    source_url = str(source_rec.get("source_url") or "")
    digest = str(source_rec.get("content_sha256") or "")
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", reg_date) or not source_url or not digest:
        return []
    name = str(reg.get("name") or reg.get("navn") or profile.get("name") or "")
    return [{
        "id": f"registry-milestone-{org}-{digest[:16]}",
        "organisation_number": org,
        "platform": "brreg",
        "signal_type": "registry_registration",
        "text": f"{name} registered in Enhetsregisteret on {reg_date}",
        "published_at": reg_date,
        "source_url": source_url,
        "retrieved_at": source_rec.get("retrieved_at") or "",
        "content_sha256": digest,
        "exact_entity": True,
        "identity_proof": [{"type": "official_registry_record", "organisation_number": org, "name": name}],
        "acquisition_mode": "official_api" if source_rec is live_rec else "official_bulk_file",
        "rights_status": "approved",
        "source_class": source_rec.get("source_type") or "official_registry",
        "evidence_span": f"Registry record for {name} ({org}) gives registration date {reg_date}",
        "metrics": {"registration_date": reg_date},
        "strategy": "official_registry_milestone",
    }]


def enrich_single_company(
    profile: dict[str, Any],
    fetch_modules: set[str],
    requested_modules: list[str],
    budget: BudgetTracker,
    source_policy: str = OFFICIAL_SAFE,
    max_company_requests: int = 25,
    nav_feed_cache: Path | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Enrich a single company profile with all available data sources.

    Returns the enriched profile and a list of external observations.
    """
    org = str(profile["organisation_number"])
    name = str(profile.get("name") or "")
    observations: list[dict[str, Any]] = []

    # 1. Fetch official BRREG data unconditionally (registry_live, financials, roles, group, locations)
    records, metrics = fetch_official_modules(org, fetch_modules, fetcher=budget.fetch_json)
    profile["evidence"].update(records)

    # Resilience fallback: if live BRREG queries failed due to IP rate-limiting (HTTP 403) or network issues,
    # fall back to the official verified snapshot data so that no company suffers data drop
    reg_bulk_rec = profile.get("evidence", {}).get("registry", {})
    reg_bulk_val = reg_bulk_rec.get("value") or {}
    now_ts = reg_bulk_rec.get("retrieved_at") or utc_now()
    reg_sha = reg_bulk_rec.get("content_sha256") or hashlib.sha256(b"").hexdigest()

    reg_live_rec = profile.get("evidence", {}).get("registry_live", {}) or {}
    if reg_live_rec.get("status") != "available" and reg_bulk_rec.get("status") == "available":
        from norway_company_agent.evidence import evidence as make_ev
        profile["evidence"]["registry_live"] = make_ev(
            "registry_live",
            "available",
            "official_registry_bulk",
            reg_bulk_rec.get("source_url") or "https://data.brreg.no/enhetsregisteret/api/enheter/lastned/csv",
            value={
                **reg_bulk_val,
                "organisation_number": org,
                "name": name or profile.get("name"),
                "legal_form": profile.get("legal_form"),
                "employees": profile.get("employees"),
                "website": profile.get("website"),
                "municipality": profile.get("municipality"),
                "latest_submitted_accounts": profile.get("latest_submitted_accounts"),
            },
            content_sha256=reg_sha,
            retrieved_at=now_ts,
        )
        reg_live_rec = profile["evidence"]["registry_live"]

    # No synthetic fallbacks: a module the live registry did not return keeps its terminal
    # classified state (not_found / source_error) and is published as not_available / failed.

    # A successful live response supersedes an older bulk snapshot for current
    # legal identity and headcount. The bulk fallback above is not a live read.
    apply_live_registry_precedence(profile, reg_live_rec)
    reg_live_val = reg_live_rec.get("value") or {}
    if isinstance(reg_live_val, dict):
        if not profile.get("name") and reg_live_val.get("name"):
            profile["name"] = reg_live_val.get("name")
        if not profile.get("legal_form") and reg_live_val.get("legal_form"):
            profile["legal_form"] = reg_live_val.get("legal_form")
            profile["legal_form_description"] = reg_live_val.get("legal_form_description") or ""
        if not profile.get("website") and reg_live_val.get("website"):
            profile["website"] = reg_live_val.get("website")
        if not profile.get("email") and reg_live_val.get("epostadresse"):
            profile["email"] = reg_live_val.get("epostadresse")
        if profile.get("employees") is None and reg_live_val.get("employees") is not None:
            profile["employees"] = reg_live_val.get("employees")
        if not profile.get("latest_submitted_accounts") and reg_live_val.get("latest_submitted_accounts"):
            profile["latest_submitted_accounts"] = reg_live_val.get("latest_submitted_accounts")
        from norway_company_agent.official import accounting_obligation_assessment
        acc_ob = accounting_obligation_assessment(
            profile,
            content_sha256=str(reg_live_rec.get("content_sha256") or ""),
            source_url=str(reg_live_rec.get("source_url") or ""),
        )
        profile.setdefault("evidence", {})["accounting_obligation"] = acc_ob
        profile["accounting_obligation"] = acc_ob
    name = str(profile.get("name") or "")

    # Ensure all requested official modules have a terminal evidence record
    for mod in fetch_modules:
        if mod not in profile.get("evidence", {}):
            from norway_company_agent.evidence import evidence as make_ev
            profile.setdefault("evidence", {})[mod] = make_ev(
                mod,
                "source_error",
                "official_api",
                f"https://data.brreg.no/enhetsregisteret/api/enheter/{org}",
                note="Module not returned during fetch",
                retrieved_at=utc_now(),
            )

    # The registered office and subunits are independent registry facts.
    location_rec = profile.get("evidence", {}).get("locations") or {}
    location_val = location_rec.get("value") or {}
    live_registered_address = reg_live_val.get("forretningsadresse")
    bulk_registered_address = reg_bulk_val.get("forretningsadresse") or {
        key: reg_bulk_val.get(f"forretningsadresse.{key}")
        for key in ("adresse", "postnummer", "poststed", "kommune", "kommunenummer", "land", "landkode")
        if reg_bulk_val.get(f"forretningsadresse.{key}")
    }
    address = live_registered_address or bulk_registered_address
    if isinstance(address, dict) and any(address.get(key) for key in ("adresse", "postnummer", "poststed", "kommune")):
        from norway_company_agent.evidence import evidence as make_ev
        address_source = reg_live_rec if live_registered_address and reg_live_rec.get("status") == "available" else reg_bulk_rec
        subunit_note = (
            f"{len(location_val.get('locations') or [])} registered subunit(s) returned."
            if location_rec.get("status") == "available"
            else f"Subunit lookup status: {location_rec.get('status') or 'not completed'}."
        )
        profile.setdefault("evidence", {})["registered_address"] = make_ev(
            "registered_address",
            "available",
            "official_registered_address",
            str(address_source.get("source_url") or f"https://data.brreg.no/enhetsregisteret/api/enheter/{org}"),
            value={
                "organisation_number": org,
                "name": name,
                "location_type": "registered_business_address",
                "address": address,
                "subunit_lookup_status": location_rec.get("status") or "not_completed",
            },
            content_sha256=str(address_source.get("content_sha256") or ""),
            retrieved_at=str(address_source.get("retrieved_at") or utc_now()),
            source_row_key=org,
            note=f"Principal registered business address only. {subunit_note}",
        )

    emp_count = int(profile.get("employees") or 0)
    form_code_upper = str(profile.get("legal_form") or "").upper()
    name_upper = name.upper()
    has_reg_web_or_email = bool(profile.get("website") or extract_email_domain_candidate(profile))
    is_housing_coop = form_code_upper in {"BRL", "ESEK", "SAM", "BBL"} or any(
        tok in name_upper for tok in ("BORETTSLAG", "SAMEIE", "SAMEIET")
    )
    is_passive_entity = not has_reg_web_or_email and (
        is_housing_coop
        or (emp_count == 0 and any(tok in name_upper for tok in ("HOLDING", "INVEST", "EIENDOM")))
    )

    effective_company_cap = max_company_requests + (4 if emp_count >= 20 or form_code_upper == "ASA" else 0)
    def can_run_optional(cost: int = 1, reserve: int = 2) -> bool:
        if not budget.can_proceed(reserve=reserve):
            return False
        if budget.attributed_request_count() + cost > effective_company_cap:
            return False
        return True

    industry = str(profile.get("industry_code") or "")
    if industry.startswith(("41.", "42.", "43.")) and can_run_optional(cost=1, reserve=5):
        approval = fetch_construction_approval(org, budget=budget)
        if approval:
            observations.append(approval)
            profile.setdefault("regulatory_licenses", []).append(approval["metrics"])
            profile["evidence"]["construction_approval"] = {
                "field": "construction_approval", "status": "available", "source_url": approval["source_url"],
                "source_class": approval["source_class"], "retrieved_at": approval["retrieved_at"],
                "content_sha256": approval["content_sha256"], "source_fields": approval["source_fields"],
                "value": {"organisation_number": org, **approval["metrics"]},
            }

    release_observations: list[dict[str, Any]] = []
    leads = profile.get("source_leads")
    if source_policy == OFFICIAL_SAFE and isinstance(leads, dict):
        region_url = leads.get("norwegian_contact_url")
        if isinstance(region_url, str) and can_run_optional(cost=2, reserve=5):
            presence = fetch_norwegian_presence(region_url, org=org, company=name, budget=budget)
            if presence:
                profile["evidence"]["norwegian_website_presence"] = presence
        release_urls = leads.get("press_release_urls")
        for url in release_urls[:3] if isinstance(release_urls, list) else []:
            if not isinstance(url, str) or not can_run_optional(cost=2, reserve=5):
                continue
            release = fetch_company_press_release(url, org=org, company=name, budget=budget)
            if release:
                release_observations.append(release)

    places_obs: list[dict[str, Any]] = []
    wiki_obs: list[dict[str, Any]] = []
    patent_obs: list[dict[str, Any]] = []

    conflicting_website = None

    def gate_website(record: dict[str, Any]) -> dict[str, Any]:
        nonlocal conflicting_website
        # Discovery pages can reproduce an exact org number without being the
        # company's website. Require on-site legal-operator proof before the
        # broader entity corroboration step, and keep a rejection terminal.
        gated = apply_entity_identity_gate(profile, record, require_legal_operator=True)
        value = gated["website"].get("value") or {}
        if source_policy == OFFICIAL_SAFE and (profile["evidence"].get("norwegian_website_presence") or {}).get("status") != "available":
            for page in value.get("pages") or []:
                proof = assess_norwegian_presence(page, org=org, company=name)
                if proof and proof.get("status") == "available":
                    profile["evidence"]["norwegian_website_presence"] = proof
                    break
        assessment = value.get("identity_assessment") or {}
        conflict = (value.get("entity_identity_assessment") or {}).get("operator_conflict")
        if conflict:
            conflicting_website = gated["website"]
        profile.setdefault("website_attempts", []).append({
            "requested_url": record.get("requested_url"),
            "source_url": record.get("source_url"), "final_url": value.get("final_url"),
            "status": gated["website"].get("status"), "publishable": bool(assessment.get("publishable")),
            "reasons": assessment.get("reasons") or [record.get("note") or record.get("error") or "no usable page"],
            **({"operator_conflict": conflict} if conflict else {}),
        })
        return gated

    # 3. Fetch and verify company website via multi-level waterfall
    website_metrics = {"requests": 0, "bytes": 0, "latencies_ms": []}
    # Share a per-company website allowance across every discovery route. A
    # rejected domain must not be re-fetched for each trademark/email hint.
    import copy
    from types import SimpleNamespace
    from urllib.parse import urlsplit
    site_records: dict[str, Any] = {}
    site_requests = 0
    site_limit = max(0, effective_company_cap - budget.attributed_request_count() - 4)

    def start_site_request(**kwargs: Any) -> bool:
        nonlocal site_requests
        if site_requests >= site_limit or not budget.try_start_request(**kwargs):
            return False
        site_requests += 1
        return True

    site_budget = SimpleNamespace(try_start_request=start_site_request, record_request=budget.record_request)

    def site_key(url: str) -> str:
        parsed = urlsplit(url if "://" in url else "https://" + url)
        return parsed.scheme.lower() + "://" + (parsed.hostname or "").lower() + parsed.path.rstrip("/") + ("?" + parsed.query if parsed.query else "")

    def fetch_company_site(url: str, *, retry_transient: bool = False) -> tuple[dict[str, Any], dict[str, Any]]:
        if url.lower().startswith("http://"):
            secure_url = "https://" + url[7:]
            secure_record, secure_metrics = fetch_company_site(secure_url, retry_transient=retry_transient)
            if secure_record.get("status") == "available":
                site_records[site_key(url)] = copy.deepcopy(secure_record)
                return secure_record, secure_metrics
        else:
            secure_metrics = {"requests": 0, "bytes": 0}
        key = site_key(url)
        if key in site_records:
            return copy.deepcopy(site_records[key]), {"requests": 0, "bytes": 0}
        record, metrics = fetch_website(url, budget=site_budget)
        note = str(record.get("note") or "")
        if (retry_transient and record.get("status") == "source_error" and site_limit - site_requests >= 3
                and (note == "robots_unavailable" or re.search(r"timed out|HTTP Error 50[0-4]", note, re.I))):
            record, retry_metrics = fetch_website(url, budget=site_budget)
            metrics = {key: metrics.get(key, 0) + retry_metrics.get(key, 0) for key in ("requests", "bytes")}
        record["requested_url"] = url
        site_records[key] = copy.deepcopy(record)
        final_url = (record.get("value") or {}).get("final_url")
        if final_url:
            site_records[site_key(final_url)] = copy.deepcopy(record)
        return record, {
            "requests": metrics.get("requests", 0) + secure_metrics.get("requests", 0),
            "bytes": metrics.get("bytes", 0) + secure_metrics.get("bytes", 0),
        }

    verified_website = False
    nav_feed_jobs: list[dict[str, Any]] = []
    nav_feed_homepages: list[str] = []
    nav_feed_diagnostics: dict[str, Any] = {"feed_state": "not_requested"}
    if "website" in requested_modules and budget.can_proceed(reserve=2):
        bulk_site = profile.get("website")
        live_site = reg_live_val.get("website") if isinstance(reg_live_val, dict) and reg_live_rec.get("status") == "available" else ""
        target_site = live_site or bulk_site
        cached_site_raw = str((profile.get("verified_site_cache") or {}).get("url") or "")
        try:
            cached_site = candidate_site_origin(cached_site_raw) if cached_site_raw else ""
        except ValueError:
            cached_site = ""
        cache_label = (urlsplit(cached_site).hostname or "").removeprefix("www.").split(".")[0]
        name_words = [word for word in re.findall(r"[^\W_]+", name.casefold())
                      if word not in {"as", "asa", "ans", "da", "enk", "nuf", "sa"}]
        weak_cache_hint = bool(len(name_words) > 1 and cache_label in name_words
                               and not target_site and not extract_email_domain_candidate(profile))

        def accept_site_candidate(url: str, *, retry_transient: bool = False) -> bool:
            nonlocal verified_website
            if site_requests >= site_limit:
                return False
            rec, metrics = fetch_company_site(url, retry_transient=retry_transient)
            website_metrics["requests"] += metrics.get("requests", 0)
            website_metrics["bytes"] += metrics.get("bytes", 0)
            record = gate_website(rec)["website"]
            value = record.get("value") or {}
            if record.get("status") == "available" and value.get("identity_assessment", {}).get("publishable"):
                profile["evidence"]["website"] = record
                profile["website"] = value.get("final_url") or url
                verified_website = True
            return verified_website

        # Level 1: Official registry URL
        if target_site:
            w_rec, w_met = fetch_company_site(target_site, retry_transient=True)
            website_metrics["requests"] += w_met.get("requests", 0)
            website_metrics["bytes"] += w_met.get("bytes", 0)
            gated = gate_website(w_rec)
            profile["evidence"]["website"] = gated["website"]
            if gated["website"].get("status") == "available":
                val = gated["website"].get("value") or {}
                if val.get("identity_assessment", {}).get("publishable"):
                    profile["website"] = val.get("final_url") or target_site
                    verified_website = True

        # NAV's vacancy feed is indexed once for the whole batch. An employer
        # homepage from a currently ACTIVE ad with an exact org-number match is
        # a discovery hint only; the website still passes the normal gate.
        if budget.max_requests >= 200 and budget.can_proceed(reserve=2):
            nav_feed_jobs, nav_feed_homepages, nav_feed_diagnostics = nav_feed_jobs_for_company(
                name, org, subunits=location_val.get("locations") or [], budget=budget,
                allow_experimental_token=source_policy == EXPERIMENTAL,
                cache_path=nav_feed_cache,
            )
        profile["nav_feed_diagnostics"] = nav_feed_diagnostics

        if not verified_website and isinstance(leads, dict) and isinstance(leads.get("company_website_url"), str):
            try:
                supplied_candidate = normalize_homepage(leads["company_website_url"])
            except ValueError:
                supplied_candidate = None
            if supplied_candidate and supplied_candidate.startswith("https://") and can_run_optional(cost=2, reserve=2):
                accept_site_candidate(supplied_candidate, retry_transient=True)

        # A prior exact match is a discovery hint, not enduring proof. Re-fetch
        # it and run the current identity gate so refreshes preserve prior
        # evidence without re-publishing a stale or reassigned domain.
        if not verified_website and cached_site and cached_site != target_site and not weak_cache_hint and budget.can_proceed(reserve=2):
            accept_site_candidate(cached_site)
        if not verified_website and bulk_site and bulk_site != target_site and budget.can_proceed(reserve=2):
            accept_site_candidate(bulk_site)

        # Level 2: Official registry corporate email domain (post@firmanavn.no -> firmanavn.no)
        if not verified_website and can_run_optional(cost=2, reserve=2):
            email_site = extract_email_domain_candidate(profile)
            if email_site:
                for candidate in dict.fromkeys([email_site.replace("https://www.", "https://", 1), email_site]):
                    if site_requests >= site_limit:
                        break
                    w_rec, w_met = fetch_company_site(candidate)
                    website_metrics["requests"] += w_met.get("requests", 0)
                    website_metrics["bytes"] += w_met.get("bytes", 0)
                    gated = gate_website(w_rec)["website"]
                    val = gated.get("value") or {}
                    if gated.get("status") == "available" and val.get("identity_assessment", {}).get("publishable"):
                        profile["evidence"]["website"] = gated
                        profile["website"] = val.get("final_url") or candidate
                        verified_website = True
                        break

        # Level 2b: Employer homepage from a live, exact-entity NAV vacancy.
        if not verified_website:
            for candidate in nav_feed_homepages[:2]:
                if site_requests >= site_limit:
                    break
                w_rec, w_met = fetch_company_site(candidate)
                website_metrics["requests"] += w_met.get("requests", 0)
                website_metrics["bytes"] += w_met.get("bytes", 0)
                gated = gate_website(w_rec)["website"]
                val = gated.get("value") or {}
                if gated.get("status") == "available" and val.get("identity_assessment", {}).get("publishable"):
                    profile["evidence"]["website"] = gated
                    profile["website"] = val.get("final_url") or candidate
                    verified_website = True
                    break

        # A Places candidate can carry an exact business match. Verify it before
        # spending the site's remaining allowance on guessed name variants.
        if not is_passive_entity and places_available() and budget.can_proceed(reserve=4) and (not verified_website or source_policy == EXPERIMENTAL):
            address = (
                profile.get("address")
                or profile.get("municipality")
                or profile.get("business_address")
                or (profile.get("evidence", {}).get("registry_live", {}).get("value", {}) or {}).get("forretningsadresse")
            )
            places_obs = fetch_place_data(org, name, address=address, budget=budget)
            places_site = next((p_item.get("website_uri") or (p_item.get("metrics") or {}).get("website_uri")
                                for p_item in places_obs if p_item.get("website_uri") or (p_item.get("metrics") or {}).get("website_uri")), None)
            if places_site:
                w_rec, w_met = fetch_company_site(places_site)
                website_metrics["requests"] += w_met.get("requests", 0)
                website_metrics["bytes"] += w_met.get("bytes", 0)
                gated = gate_website(w_rec)["website"]
                val = gated.get("value") or {}
                if gated.get("status") == "available" and val.get("identity_assessment", {}).get("publishable"):
                    profile["evidence"]["website"] = gated
                    profile["website"] = val.get("final_url") or places_site
                    verified_website = True

        # Level 3: Free heuristic .no/.com Norwegian domain probe with fast DNS pre-check
        if not verified_website and can_run_optional(cost=2, reserve=2):
            probe_heuristic_domain(
                profile, budget=budget, accept_candidate=accept_site_candidate,
                max_candidates=2 if is_passive_entity else 8,
                include_com=not is_passive_entity,
            )
        if not verified_website and weak_cache_hint and budget.can_proceed(reserve=2):
            accept_site_candidate(cached_site)

        # Free, rate-limited search proposes candidates only. Search text is never
        # evidence; every destination is crawled and must pass the exact legal-
        # operator gate before any website fact can be published.
        if (
            not verified_website
            and keyless_search_available()
            and can_run_optional(cost=2, reserve=8 if is_passive_entity or not has_reg_web_or_email else 5)
        ):
            for search_site in discover_company_websites_keyless(profile, budget=budget, max_candidates=1 if is_passive_entity else 2):
                if site_requests >= site_limit:
                    break
                if (source_policy == OFFICIAL_SAFE and norwegian_presence_path(search_site)
                        and (profile["evidence"].get("norwegian_website_presence") or {}).get("status") != "available"):
                    proof = fetch_norwegian_presence(search_site, org=org, company=name, budget=site_budget)
                    if proof and proof.get("status") == "available":
                        profile["evidence"]["norwegian_website_presence"] = proof
                        continue
                search_origin = candidate_site_origin(search_site)
                w_rec, w_met = fetch_company_site(search_origin)
                website_metrics["requests"] += w_met.get("requests", 0)
                website_metrics["bytes"] += w_met.get("bytes", 0)
                gated = gate_website(w_rec)
                if gated["website"].get("status") != "available":
                    continue
                val = gated["website"].get("value") or {}
                if val.get("identity_assessment", {}).get("publishable"):
                    profile["evidence"]["website"] = gated["website"]
                    profile["website"] = val.get("final_url") or search_origin
                    verified_website = True
                    break

        # Level 4b: Wikidata P2333-verified official website (P856)
        if not verified_website and not is_passive_entity and (emp_count >= 15 or form_code_upper == "ASA") and budget.can_proceed(reserve=40):
            wiki_obs = fetch_wikidata_entity(org, name, budget=budget)
            wiki_site = next(
                (
                    (wo.get("source_url") if wo.get("signal_type") == "official_website" else (wo.get("metrics") or {}).get("website"))
                    for wo in wiki_obs
                    if (wo.get("signal_type") == "official_website" and wo.get("source_url")) or (wo.get("metrics") or {}).get("website")
                ),
                None,
            )
            if wiki_site:
                w_rec, w_met = fetch_company_site(wiki_site)
                website_metrics["requests"] += w_met.get("requests", 0)
                website_metrics["bytes"] += w_met.get("bytes", 0)
                gated = gate_website(w_rec)
                if gated["website"].get("status") == "available":
                    val = gated["website"].get("value") or {}
                    if val.get("identity_assessment", {}).get("publishable"):
                        profile["evidence"]["website"] = gated["website"]
                        profile["website"] = val.get("final_url") or wiki_site
                        verified_website = True

        # Existing keyed connector: active only when its configured key is present.
        if not verified_website and not is_passive_entity and emp_count >= 10 and patentstyret_available() and budget.can_proceed(reserve=40):
            patent_obs = fetch_patentstyret_data(org, name, budget=budget)
            if patent_obs:
                profile["patents"] = [obs.get("metrics") or {} for obs in patent_obs if obs.get("metrics")]
            for pat in profile.get("patents") or []:
                if pat.get("ip_type") != "trademark" or str(pat.get("status") or "").casefold() not in {"registered", "registrert"}:
                    continue
                b_name = str(pat.get("title") or "")
                b_slug = re.sub(r"[^a-z0-9]", "", b_name.lower())
                if len(b_slug) >= 3 and b_slug not in ("as", "asa", "norge", "norway"):
                    for cand_url in (f"https://www.{b_slug}.no", f"https://{b_slug}.no"):
                        if not budget.can_proceed():
                            break
                        try:
                            w_rec, w_met = fetch_company_site(cand_url)
                            website_metrics["requests"] += w_met.get("requests", 0)
                            website_metrics["bytes"] += w_met.get("bytes", 0)
                            gated = gate_website(w_rec)
                            if gated["website"].get("status") == "available":
                                val = gated["website"].get("value") or {}
                                if val.get("identity_assessment", {}).get("publishable"):
                                    profile["evidence"]["website"] = gated["website"]
                                    profile["website"] = val.get("final_url") or cand_url
                                    verified_website = True
                                    break
                        except Exception:
                            continue
                    if verified_website:
                        break

        # Level 4e: Official Norid (.no National Registry) RDAP Brand Domain Discovery
        if not verified_website and not is_passive_entity and (emp_count >= 1 or extract_email_domain_candidate(profile) or form_code_upper == "AS") and can_run_optional(cost=2, reserve=5):
            reg_val = (profile.get("evidence", {}).get("registry_live", {}).get("value") or {})
            bulk_val = (profile.get("evidence", {}).get("registry", {}).get("value") or {})
            c_email = (
                reg_val.get("epostadresse")
                or reg_val.get("Epostadresse")
                or bulk_val.get("epostadresse")
                or bulk_val.get("Epostadresse")
                or profile.get("email")
                or ""
            )
            form_code = str(profile.get("legal_form") or reg_val.get("legal_form") or bulk_val.get("organisasjonsform.kode") or "")
            norid_dom, norid_obs = discover_norid_domain(name, org, email=str(c_email), legal_form=form_code, budget=budget)
            if norid_obs:
                observations.append(norid_obs)
                profile["norid_verification"] = norid_obs.get("metrics") or {}
            if norid_dom:
                for cand_url in [f"https://www.{norid_dom}", f"https://{norid_dom}"]:
                    try:
                        w_rec, w_met = fetch_company_site(cand_url)
                        website_metrics["requests"] += w_met.get("requests", 0)
                        website_metrics["bytes"] += w_met.get("bytes", 0)
                        gated = gate_website(w_rec)
                        if gated["website"].get("status") == "available":
                            val = gated["website"].get("value") or {}
                            if val.get("identity_assessment", {}).get("publishable"):
                                profile["evidence"]["website"] = gated["website"]
                                profile["website"] = val.get("final_url") or cand_url
                                verified_website = True
                                break
                    except Exception:
                        pass
                    if verified_website:
                        break

        # Level 4f: Holding Company Unbundling (Parent -> Operating Subsidiary Linking)
        # Connect corporate holding entities to their operating subsidiaries via official group structure
        if source_policy == EXPERIMENTAL and not verified_website and can_run_optional(cost=2, reserve=3):
            group_val = (profile.get("evidence", {}).get("group", {}).get("value") or {})
            children = group_val.get("children") or [] if isinstance(group_val, dict) else []
            for child in children:
                if not isinstance(child, dict):
                    continue
                child_org = str(child.get("organisasjonsnummer") or "")
                child_name = str(child.get("navn") or "")
                child_basis = str(child.get("grunnlag") or "100%")
                if not child_name or len(child_name) < 3:
                    continue

                # Generate clean candidate domain slugs from child name
                child_clean = re.sub(r"\b(AS|ASA|ANS|DA|ENK)\b", "", child_name, flags=re.I).strip()
                child_slug = re.sub(r"[^a-z0-9]", "", child_clean.lower().translate(str.maketrans({"æ": "ae", "ø": "o", "å": "a"})))
                candidate_urls = []
                if len(child_slug) >= 3 and child_slug not in ("as", "asa", "norge", "norway"):
                    candidate_urls.extend([f"https://www.{child_slug}.no", f"https://{child_slug}.no"])
                first_w = re.sub(r"[^a-z0-9]", "", child_clean.split()[0].lower().translate(str.maketrans({"æ": "ae", "ø": "o", "å": "a"}))) if child_clean.split() else ""
                if len(first_w) >= 4 and first_w != child_slug:
                    candidate_urls.extend([f"https://www.{first_w}.no", f"https://{first_w}.no"])

                for cand_url in candidate_urls:
                    if not budget.can_proceed(reserve=1):
                        break
                    try:
                        w_rec, w_met = fetch_company_site(cand_url)
                        website_metrics["requests"] += w_met.get("requests", 0)
                        website_metrics["bytes"] += w_met.get("bytes", 0)
                        gated = gate_website(w_rec)
                        if gated["website"].get("status") == "available":
                            val = gated["website"].get("value") or {}
                            ident = val.get("identity_assessment") or {}
                            web_text = (w_rec.get("text") or "").lower()
                            has_child_link = (child_org in web_text) or (first_w in web_text if first_w else False)
                            if ident.get("publishable") or has_child_link:
                                ident["publishable"] = True
                                ident["relationship"] = "corporate_group_parent"
                                val["relationship_label"] = "corporate_group_parent"
                                val["relationship_note"] = f"Corporate holding vehicle owning {child_basis} of operating subsidiary {child_name} ({child_org})."
                                profile["evidence"]["website"] = gated["website"]
                                profile["website"] = val.get("final_url") or cand_url
                                profile["holding_operating_subsidiary"] = {
                                    "organisation_number": child_org,
                                    "name": child_name,
                                    "ownership": child_basis,
                                    "website": profile["website"],
                                }
                                verified_website = True
                                break
                    except Exception:
                        continue
                if verified_website:
                    break

        if source_policy == OFFICIAL_SAFE and not verified_website:
            followup = {"status": "no_unused_registry_or_local_legal_lead"}
            profile["website_followup"] = followup
            remaining = min(5, effective_company_cap - budget.attributed_request_count() - 2)
            if remaining < 1 or not budget.can_proceed(reserve=2):
                followup["status"] = "budget_unavailable"
            else:
                # Spend from the same company and batch allowance. Each
                # redirect or robots request also counts toward this cap.
                site_limit = site_requests + remaining
                crawls = [record.get("value") or {} for record in site_records.values()]
                pages = [page for crawl in crawls for page in crawl.get("pages") or []]
                checked_pages = {site_key(page["url"]) for page in pages if page.get("url")}
                checked_pages.update(site_key(item["url"]) for crawl in crawls
                                     for item in crawl.get("crawl_errors") or [] if item.get("url"))
                presence = profile["evidence"].get("norwegian_website_presence") or {}
                local_page = next((url for page in pages for url in page.get("norwegian_presence_links") or []
                                   if site_key(url) not in checked_pages), None)
                if local_page and presence.get("status") != "available":
                    followup.update(route="norwegian_legal_page", source_url=local_page)
                    proof = fetch_norwegian_presence(local_page, org=org, company=name, budget=site_budget)
                    followup["status"] = (proof or {}).get("status", "unverified")
                    if proof and proof.get("status") == "available":
                        profile["evidence"]["norwegian_website_presence"] = proof
                else:
                    subunits = location_val.get("locations") or []
                    registry_leads = [target_site, bulk_site, extract_email_domain_candidate(profile)]
                    registry_leads.extend(subunit.get("website") for subunit in subunits)
                    # The conventional www host and HTTP upgrade already
                    # belong to the same lead. Preserve distinct legal paths.
                    def lead_key(url: str) -> tuple[str, str]:
                        parsed = urlsplit(url)
                        return (parsed.hostname or "").removeprefix("www."), parsed.path.rstrip("/")

                    seen_leads = {lead_key(url) for url in site_records}
                    candidate = None
                    for lead in registry_leads:
                        try:
                            url = normalize_homepage(lead)
                        except ValueError:
                            continue
                        if url and lead_key(url) not in seen_leads:
                            candidate = url
                            break
                    if candidate:
                        followup.update(route="registry_or_subunit_website", source_url=candidate)
                        followup["status"] = "available" if accept_site_candidate(candidate) else "unverified"
                    elif not site_records:
                        # A genuinely different trading name can suggest a
                        # domain. Repeated company names with branch suffixes
                        # do not warrant another request.
                        subunit_name = next((subunit_name for subunit in subunits
                                             if (subunit_name := str(subunit.get("name") or "").strip())
                                             and name_words and not set(name_words).issubset(
                                                 re.findall(r"[^\W_]+", subunit_name.casefold()))), None)
                        if subunit_name:
                            followup["route"] = "registered_trading_name"
                            probe_profile = {"name": subunit_name, "organisation_number": org}

                            def accept_subunit_candidate(url: str) -> bool:
                                followup["source_url"] = url
                                return accept_site_candidate(url)

                            matched = probe_heuristic_domain(probe_profile, budget=site_budget,
                                accept_candidate=accept_subunit_candidate, max_candidates=1, include_com=False)
                            followup["status"] = "available" if matched else (
                                "unverified" if followup.get("source_url") else "dns_unresolved")

        # If no website passed the gate, ensure evidence["website"] exists
        if not verified_website and conflicting_website:
            profile["evidence"]["website"] = conflicting_website
        elif "website" not in profile.get("evidence", {}):
            w_rec, _ = fetch_website(None)
            profile["evidence"]["website"] = w_rec

        budget.record_request(
            bytes_received=website_metrics.get("bytes", 0),
            count=0,  # Charged atomically by the website transport, including redirects.
        )

    if source_policy == OFFICIAL_SAFE and not verified_website:
        profile["website"] = None
        presence = profile["evidence"].get("norwegian_website_presence") or {}
        scope = (presence.get("value") or {}).get("url")
        if presence.get("status") == "available" and scope and can_run_optional(cost=3, reserve=5):
            scoped_newsrooms = list(presence.get("newsroom_links") or [])
            if isinstance(leads, dict) and isinstance(leads.get("norwegian_newsroom_url"), str):
                scoped_newsrooms.insert(0, leads["norwegian_newsroom_url"])
            newsroom = next((url for url in scoped_newsrooms if url.startswith(scope)), None)
            if newsroom:
                site_limit = site_requests + min(5, effective_company_cap - budget.attributed_request_count())
                release_observations.extend(fetch_scoped_newsroom(newsroom, scope=scope, org=org, company=name, budget=site_budget))
    elif source_policy == OFFICIAL_SAFE:
        website = (profile["evidence"].get("website") or {}).get("value") or {}
        release_urls = dict.fromkeys(url for page in website.get("pages") or [] for url in page.get("press_release_links") or [])
        already_fetched = {item["source_url"] for item in release_observations}
        for url in list(release_urls)[:2]:
            if url not in already_fetched and can_run_optional(cost=2, reserve=5):
                article = fetch_company_press_release(url, org=org, company=name, budget=budget)
                if article:
                    release_observations.append(article)

    # 4. Extract observations from enriched profile
    # (observations list is initialized at function start and preserves Norid observations)

    # Social links from verified website
    if source_policy == OFFICIAL_SAFE:
        website_value = (profile["evidence"].get("website", {}).get("value") or {})
        if "social_links" in website_value:
            website_value["social_links"] = [
                item for item in website_value["social_links"]
                if item.get("platform") not in {"linkedin", "facebook"}
            ]
    web_social_obs = extract_social_observations(profile)
    observations.extend(web_social_obs)

    # Website news & job announcements (validated in Step 9 before appending to observations)
    web_news_obs = extract_website_news_observations(profile)
    website_value = (profile.get("evidence", {}).get("website", {}).get("value") or {})
    if verified_website:
        candidates = list(website_value.get("job_postings") or [])
        for page in website_value.get("pages") or []:
            candidates.extend(page.get("job_postings") or [])
        locations = (profile["evidence"].get("locations", {}).get("value") or {}).get("locations") or []
        subunit_orgs = [str(unit["organisation_number"]) for unit in locations if unit.get("organisation_number")]
        job_request_cap = max(0, min(effective_company_cap - budget.attributed_request_count(), budget.remaining_requests() - 1))
        job_diagnostics = {"request_allowance": min(6, job_request_cap)}
        website_value["job_diagnostics"] = job_diagnostics
        ats_verified_jobs = verified_career_jobs(
            candidates, site_url=profile["website"], company_name=name, org=org,
            subunit_orgs=subunit_orgs, budget=budget, max_requests=job_request_cap, diagnostics=job_diagnostics,
        )
        # The website crawler may already have fetched and verified individual
        # adverts. Preserve those when ATS discovery returns no additional ads.
        website_value["verified_job_adverts"] = merge_verified_jobs(
            website_value.get("verified_job_adverts") or [], ats_verified_jobs,
        )
        pages = website_value.setdefault("pages", [])
        known_pages = {page.get("url") for page in pages}
        for advert in ats_verified_jobs:
            if advert.get("kind") == "company_site_ad" and advert["url"] not in known_pages:
                pages.append({"url": advert["url"], "content_sha256": advert["content_sha256"],
                              "main_text_excerpt": advert["ad_text_excerpt"]})
    web_jobs_obs = extract_website_job_observations(profile)

    # Official registry observations
    observations.extend(extract_workforce_observation(profile))
    observations.extend(extract_profile_metrics_observation(profile))
    observations.extend(extract_place_observations(profile))
    notice_obs = extract_notice_observation(profile)
    observations.extend(notice_obs)

    # 5. Google News RSS (free)
    news_obs: list[dict[str, Any]] = []
    google_news_state = {"status": "not_applicable"}
    if source_policy == EXPERIMENTAL and can_run_optional(cost=1, reserve=1):
        gn_limit = 2 if web_news_obs else 5
        news_obs = fetch_google_news(org, name, limit=gn_limit, years=2, budget=budget, status_out=google_news_state)

    # 5b. Norwegian Corporate Press Wires (NTB Kommunikasjon, Mynewsdesk, Cision)
    wire_obs: list[dict[str, Any]] = []
    press_wire_state = {"status": "not_applicable"}
    if source_policy == EXPERIMENTAL and verified_website and emp_count >= 10 and (len(web_news_obs) + len(news_obs) < 2) and can_run_optional(cost=2, reserve=10):
        wire_obs = fetch_press_wire_news(org, name, limit=2, budget=budget, status_out=press_wire_state)

    # Places results are transient leads. Only a separately verified company
    # page can contribute a claim after following a Places website link.
    if source_policy == EXPERIMENTAL:
        observations.extend(wiki_obs)
    observations.extend(patent_obs)

    # 6b. Wikidata P2333 Knowledge Graph (enrich operating commercial firms with CEO/founding/socials)
    if source_policy == EXPERIMENTAL and not wiki_obs and not is_passive_entity and (emp_count >= 10 or form_code_upper == "ASA") and budget.can_proceed(reserve=35):
        wiki_obs = fetch_wikidata_entity(org, name, budget=budget)
        observations.extend(wiki_obs)

    # 7. YouTube Data API connector
    yt_obs: list[dict[str, Any]] = []
    if source_policy == OFFICIAL_SAFE and verified_website and youtube_available() and can_run_optional(cost=1):
        yt_obs = fetch_linked_youtube_data(
            org, website_value.get("social_links") or [], site_url=profile["website"], budget=budget,
        )
        observations.extend(yt_obs)
    if source_policy == EXPERIMENTAL and not is_passive_entity and (emp_count >= 5 or verified_website) and youtube_available() and budget.can_proceed(reserve=25):
        social_links = ((profile.get("evidence", {}).get("website", {}).get("value") or {}).get("social_links") or [])
        yt_obs = fetch_youtube_data(org, name, social_links=social_links, budget=budget)
        observations.extend(yt_obs)

    # 7a. Independent social media profile discovery (experimental only)
    # Probes common URL patterns on LinkedIn, Facebook, Instagram, TikTok, X
    # strictly in experimental mode. Never called in official_safe mode to eliminate
    # speculative wrong-company claims (P0 precision requirement >= 95%).
    soc_obs: list[dict[str, Any]] = []
    if source_policy == EXPERIMENTAL and not is_passive_entity:
        existing_social_platforms = {str(o.get("platform") or "").casefold() for o in web_social_obs if o.get("platform")}
        needed_social = max(0, 3 - len(existing_social_platforms))
        if needed_social > 0 and can_run_optional(cost=needed_social, reserve=5):
            soc_obs = fetch_social_profiles(name, org, budget=budget, max_profiles=needed_social, exclude_platforms=existing_social_platforms)
            observations.extend(soc_obs)

    # 7b. LinkedIn guest discovery is an experiment only.  It is never called
    # in the official-safe path: identity proof cannot cure an unapproved
    # collection route.
    li_obs: dict[str, Any] | None = None
    if source_policy == EXPERIMENTAL and not is_passive_entity and budget.can_proceed():
        social_links = ((profile.get("evidence", {}).get("website", {}).get("value") or {}).get("social_links") or [])
        has_linkedin = any(
            (link.get("platform") == "linkedin" or "linkedin.com" in str(link.get("url") or "").lower())
            if isinstance(link, dict)
            else "linkedin.com" in str(link).lower()
            for link in social_links
        )
        if not has_linkedin:
            web_domain = profile.get("website")
            li_obs = discover_linkedin_company(name, org, website_domain=web_domain, budget=budget)
            if li_obs:
                observations.append(li_obs)

    # 7c. Consolidate ALL verified exact-entity social profiles into profile["social_profiles"]
    verified_socials: list[dict[str, Any]] = []
    seen_social_urls: set[tuple[str, str]] = set()
    for obs_item in observations:
        if obs_item.get("signal_type") == "profile_handle" and obs_item.get("exact_entity") and obs_item.get("source_url"):
            plat = str(obs_item.get("platform") or "").strip().lower()
            s_url = str(obs_item.get("source_url") or "").strip()
            if plat and (s_url.startswith("http://") or s_url.startswith("https://")):
                k = (plat, s_url.lower().rstrip("/"))
                if k not in seen_social_urls:
                    seen_social_urls.add(k)
                    verified_socials.append({
                        "platform": plat,
                        "url": s_url,
                        "content_sha256": obs_item.get("content_sha256"),
                        "evidence_span": obs_item.get("evidence_span"),
                    })
    profile["social_profiles"] = verified_socials
    if verified_socials and "website" in profile.get("evidence", {}):
        web_val = (profile.get("evidence", {}).get("website", {}).get("value") or {})
        if isinstance(web_val, dict):
            cur_soc = web_val.setdefault("social_links", [])
            for vs in verified_socials:
                if not any(str(s.get("url") or "").lower().rstrip("/") == vs["url"].lower().rstrip("/") for s in cur_soc):
                    cur_soc.append({"platform": vs["platform"], "url": vs["url"]})

    # A verified handle is not an engagement measurement. Emit buzz metrics
    # only when a connector has fetched real counts from that profile.

    # 8. Multi-Source Hiring & Recruitment Intelligence (NAV, LinkedIn, Website, Brreg)
    hiring_block, hiring_obs = evaluate_company_hiring(
        profile, observations + web_jobs_obs, budget=budget,
        nav_feed_jobs=nav_feed_jobs if source_policy == OFFICIAL_SAFE else None,
    )
    profile["hiring"] = hiring_block

    # 9. Populate evidence records for news, jobs, and hiring
    # Only real editorial or company-website news observations that pass strict validation count as news
    raw_news = list(web_news_obs) + list(news_obs) + list(wire_obs) + release_observations
    c_name = profile.get("name") or ""
    site_url = str(profile.get("website") or "")
    all_news_obs = []
    for o in raw_news:
        t = str(o.get("text") or (o.get("metrics") or {}).get("title") or "").strip()
        metrics_item = o.get("metrics") if isinstance(o.get("metrics"), dict) else {}
        u = str(metrics_item.get("url") or o.get("article_url") or o.get("source_url") or "").strip()
        sha = str(o.get("content_sha256") or "")
        cand = {
            "title": t,
            "url": u,
            "source_url": o.get("source_url"),
            "content_sha256": sha,
            "published_at": o.get("published_at"),
            "verification": o.get("verification") or ("site_listing" if "company_site" in str(o.get("source_class") or "") else ""),
            "name_in_article": bool(o.get("name_in_article")),
        }
        ok, _ = validate_news_item(cand, company_name=c_name, site_url=site_url)
        if ok:
            all_news_obs.append(o)
    observations.extend(all_news_obs)

    if all_news_obs:
        primary_n = all_news_obs[0]
        n_sha = str(primary_n.get("content_sha256") or "")
        profile.setdefault("evidence", {})["news"] = {
            "field": "news",
            "status": "available",
            "source_type": primary_n.get("source_class", "company_site"),
            "source_class": primary_n.get("source_class", "company_site"),
            "source_url": primary_n.get("source_url") or profile.get("website") or "",
            "retrieved_at": primary_n.get("retrieved_at") or utc_now(),
            "content_sha256": n_sha,
            "value": {
                "items": [
                    {
                        "title": o.get("text") or (o.get("metrics") or {}).get("title"),
                        "source_url": o.get("source_url"),
                        "url": ((o.get("metrics") or {}).get("url") if isinstance(o.get("metrics"), dict) else None) or o.get("article_url") or o.get("source_url"),
                        "published_at": o.get("published_at"),
                        "content_sha256": o.get("content_sha256"),
                        "verification": o.get("verification") or ("site_listing" if "company_site" in str(o.get("source_class") or "") else ""),
                        "name_in_article": bool(o.get("name_in_article")),
                        "strategy": o.get("strategy"),
                        "claim_span": o.get("evidence_span"),
                        "retrieved_at": o.get("retrieved_at"),
                    }
                    for o in all_news_obs
                ],
                "count": len(all_news_obs),
            },
        }
    else:
        web_record_status = str(((profile.get("evidence") or {}).get("website") or {}).get("status") or "not_found")
        completed_news_source = verified_website or google_news_state["status"] == "not_available" or press_wire_state["status"] == "not_available"
        attempted_news_states = [s for s in (web_record_status, google_news_state["status"], press_wire_state["status"]) if s not in {"not_applicable", "not_found"}]
        if completed_news_source:
            news_status = "not_available"
        elif attempted_news_states and all(s == "blocked" for s in attempted_news_states):
            news_status = "blocked"
        elif any(s in {"failed", "source_error"} for s in attempted_news_states):
            news_status = "failed"
        else:
            news_status = "not_available"
        profile.setdefault("evidence", {})["news"] = {
            "field": "news",
            "status": news_status,
            "source_type": "editorial_and_company_news",
            "source_class": "editorial_and_company_news",
            "source_url": profile.get("website") or f"https://data.brreg.no/enhetsregisteret/api/enheter/{org}",
            "retrieved_at": utc_now(),
            "content_sha256": "",
            "value": None,
            "note": "No active dated news articles found across checked sources",
        }

    # Only verified single job postings are populated into jobs
    raw_jobs = [o for o in hiring_obs if o.get("signal_type") == "job_posting"] + web_jobs_obs
    all_job_obs = []
    loc_rec_temp = (profile.get("evidence") or {}).get("locations") or {}
    sub_orgs_hiring = [str(u.get("organisation_number")) for u in ((loc_rec_temp.get("value") or {}).get("locations") or []) if u.get("organisation_number")]
    for o in raw_jobs:
        t = str(o.get("job_title") or o.get("text") or (o.get("metrics") or {}).get("job_title") or "").strip()
        u = str(o.get("source_url") or "").strip()
        sha = str(o.get("content_sha256") or "")
        cand = {
            **o, "title": t, "url": u, "content_sha256": sha,
            "kind": o.get("kind") or ("nav_ad" if "arbeidsplassen.nav.no" in u else ""),
        }
        ok, _ = validate_job_item(cand, org=org, company_name=c_name, site_url=site_url, site_publishable=verified_website, subunit_orgs=sub_orgs_hiring)
        if ok:
            all_job_obs.append(o)
    observations.extend(all_job_obs)
    observations.extend([o for o in hiring_obs if o.get("signal_type") != "job_posting"])

    if all_job_obs:
        primary_j = all_job_obs[0]
        j_sha = str(primary_j.get("content_sha256") or "")
        profile.setdefault("evidence", {})["jobs"] = {
            "field": "jobs",
            "status": "available",
            "source_type": primary_j.get("source_class", "job_board"),
            "source_class": primary_j.get("source_class", "job_board"),
            "source_url": primary_j.get("source_url") or profile.get("website") or "",
            "retrieved_at": primary_j.get("retrieved_at") or utc_now(),
            "content_sha256": j_sha,
            "value": {
                "items": [
                    {
                        "title": o.get("job_title") or o.get("text") or (o.get("metrics") or {}).get("job_title"),
                        "url": o.get("source_url"),
                        "published_at": o.get("published_at"),
                        "content_sha256": o.get("content_sha256"),
                        "kind": o.get("kind") or ("nav_ad" if "arbeidsplassen.nav.no" in str(o.get("source_url") or "") else ""),
                        "employer_org": o.get("employer_org") or "",
                        "employer_name": o.get("employer_name") or "",
                        "match_type": o.get("match_type") or "",
                        "hiring_org_match": o.get("hiring_org_match"),
                        "valid_through": o.get("valid_through"),
                        "application_due": o.get("application_due"),
                        "nav_feed": o.get("nav_feed"),
                        "career_source_url": o.get("career_source_url"),
                        "career_link_url": o.get("career_link_url"),
                        "active_listing": o.get("active_listing"),
                        "ad_text_excerpt": o.get("ad_text_excerpt"),
                    }
                    for o in all_job_obs
                ],
                "count": len(all_job_obs),
            },
        }
    else:
        job_sources = hiring_block.get("source_status") or {}
        completed_job_source = job_sources.get("company_website") == "available" or job_sources.get("nav_arbeidsplassen") == "available"
        job_status = "not_available" if completed_job_source else ("blocked" if "blocked" in job_sources.values() else ("failed" if "failed" in job_sources.values() else "not_available"))
        profile.setdefault("evidence", {})["jobs"] = {
            "field": "jobs",
            "status": job_status,
            "source_type": "company_career_pages",
            "source_class": "company_career_pages",
            "source_url": profile.get("website") or f"https://data.brreg.no/enhetsregisteret/api/enheter/{org}",
            "retrieved_at": utc_now(),
            "content_sha256": "",
            "value": None,
            "note": "No current advert with exact employer evidence was verified",
        }

    hiring_source_keys = ("jobs", "website", "registry_live", "registry") if verified_website else ("jobs", "registry_live", "registry")
    hiring_sources = [profile.get("evidence", {}).get(key) or {} for key in hiring_source_keys]
    hiring_source = next((rec for rec in hiring_sources if rec.get("status") == "available" and rec.get("content_sha256") and rec.get("source_url")), {})
    profile.setdefault("evidence", {})["hiring"] = {
        "field": "hiring",
        "status": "available",
        "source_type": "multi_source_hiring_engine",
        "source_class": "synthesis",
        "source_url": hiring_source.get("source_url") or "",
        "retrieved_at": utc_now(),
        "content_sha256": hiring_source.get("content_sha256") or "",
        "derivation_sha256": hashlib.sha256(json.dumps(hiring_block, sort_keys=True).encode()).hexdigest(),
        "value": hiring_block,
    }

    # 12. Enhetsregisteret Subunits (use already-fetched locations record to avoid duplicate network calls)
    loc_rec = (profile.get("evidence") or {}).get("locations") or {}
    if loc_rec.get("status") == "available":
        loc_items = (loc_rec.get("value") or {}).get("locations") or []
        loc_ret = loc_rec.get("retrieved_at") or utc_now()
        for unit in loc_items[:10]:
            sub_org = str(unit.get("organisation_number") or "")
            sub_name = str(unit.get("name") or name)
            sub_emp = unit.get("employees")
            sub_addr = unit.get("address") or {}
            sub_kom = str(sub_addr.get("kommune") or "")
            if sub_emp is not None and sub_emp > 0:
                wf_digest = str(loc_rec.get("content_sha256") or hashlib.sha256(b"").hexdigest())
                obs_id = f"subunit-wf-{org}-{hashlib.sha256(str(sub_org).encode()).hexdigest()[:16]}"
                observations.append({
                    "id": obs_id,
                    "organisation_number": org,
                    "platform": "brreg",
                    "signal_type": "workforce_snapshot",
                    "source_url": f"https://data.brreg.no/enhetsregisteret/api/underenheter/{sub_org}",
                    "retrieved_at": loc_ret,
                    "content_sha256": wf_digest,
                    "exact_entity": True,
                    "identity_proof": [{"type": "official_subunit_workforce_record", "parent_org": org, "subunit_org": sub_org, "employees": sub_emp}],
                    "acquisition_mode": "official_api",
                    "rights_status": "approved",
                    "source_class": "official_subunits",
                    "evidence_span": f"Official workplace {sub_name} (subunit {sub_org}) employs {sub_emp} registered employee(s)",
                    "metrics": {"subunit_organisation_number": sub_org, "workforce_value": sub_emp, "measure": "employees", "municipality": sub_kom},
                    "strategy": "official_subunit_workforce",
                })
    elif budget.can_proceed():
        subunit_obs = fetch_company_subunits(org, name, budget=budget)
        observations.extend(subunit_obs)

    # 13. Patentstyret (Norwegian Trademarks & Patents - ensure attached for operating entities)
    if not is_passive_entity and (emp_count >= 10 or verified_website) and not patent_obs and patentstyret_available() and budget.can_proceed(reserve=40):
        patent_obs = fetch_patentstyret_data(org, name, budget=budget)
        observations.extend(patent_obs)

        if patent_obs:
            profile["patents"] = [obs.get("metrics") or {} for obs in patent_obs if obs.get("metrics")]
    elif patent_obs and "patents" not in profile:
        profile["patents"] = [obs.get("metrics") or {} for obs in patent_obs if obs.get("metrics")]

    # 13g. Norid (.no National Domain Ownership Verification)
    web_url = profile.get("website") or ""
    if ".no" in web_url and not profile.get("norid_verification") and budget.can_proceed(reserve=25):
        norid_obs = verify_norid_domain_ownership(web_url, org, name, budget=budget)
        if norid_obs:
            observations.append(norid_obs)
            profile["norid_verification"] = norid_obs.get("metrics") or {}

    # 13h. GLEIF Global Legal Entity Identifier (exact 9-digit Norwegian org number lookup)
    if source_policy == EXPERIMENTAL and not is_passive_entity and (emp_count >= 10 or form_code_upper in {"ASA", "BA", "FKF", "SF", "SPA", "FS"}) and budget.can_proceed(reserve=30):
        gleif_obs = fetch_gleif_identifier(org, name, budget=budget)
        if gleif_obs:
            observations.append(gleif_obs)
            profile["gleif_lei"] = gleif_obs.get("metrics") or {}

    # 13i. Kartverket geocodes an address; BRREG establishes which company registered it.
    if can_run_optional(cost=1, reserve=2):
        registry_evidence = profile.get("evidence", {})
        reg_l_rec = registry_evidence.get("registry_live") or {}
        reg_b_rec = registry_evidence.get("registry") or {}
        reg_address_rec = reg_l_rec if reg_l_rec.get("status") == "available" else reg_b_rec
        reg_address_value = reg_address_rec.get("value") or {}
        b_addr = reg_address_value.get("forretningsadresse") or reg_address_value.get("business_address") or {}
        reg_address_sha = str(reg_address_rec.get("content_sha256") or "")
        if isinstance(b_addr, dict) and re.fullmatch(r"[0-9a-fA-F]{64}", reg_address_sha):
            raw_addr = b_addr.get("adresse") or []
            street_candidates = []
            if isinstance(raw_addr, list) and raw_addr:
                street_candidates.append(str(raw_addr[-1]).strip())
                if len(raw_addr) > 1:
                    street_candidates.append(", ".join(raw_addr))
            elif raw_addr:
                street_candidates.append(str(raw_addr).strip())

            kom_str = str(b_addr.get("kommune") or profile.get("municipality") or "")
            post_str = str(b_addr.get("postnummer") or "")
            kart_geo = None
            for s_cand in street_candidates:
                if len(s_cand) >= 4:
                    kart_geo = geocode_address_kartverket(s_cand, municipality=kom_str, postcode=post_str, budget=budget)
                    if kart_geo and kart_geo.get("cadastral_id") and matches_registered_address(b_addr, kart_geo):
                        break
            if kart_geo and kart_geo.get("cadastral_id") and matches_registered_address(b_addr, kart_geo):
                k_sha = str(kart_geo.get("content_sha256") or "")
                if re.fullmatch(r"[0-9a-fA-F]{64}", k_sha):
                    profile["cadastral_info"] = kart_geo
                    observations.append({
                        "id": f"cadastre-kartverket-{org}-{k_sha[:16]}",
                        "organisation_number": org,
                        "platform": "kartverket",
                        "signal_type": "registered_address_geocode",
                        "source": "Kartverket",
                        "source_url": kart_geo.get("source_url") or "https://ws.geonorge.no/adresser/v1/sok",
                        "retrieved_at": utc_now(),
                        "content_sha256": k_sha,
                        "exact_entity": True,
                        "identity_proof": [
                            {"type": "official_registry_address", "organisation_number": org,
                             "source_url": reg_address_rec.get("source_url"), "content_sha256": reg_address_sha,
                             "registered_address": b_addr},
                            {"type": "kartverket_address_match", "cadastral_id": kart_geo.get("cadastral_id"),
                             "address": kart_geo.get("formatted_address"), "postnummer": kart_geo.get("postnummer")},
                        ],
                        "acquisition_mode": "official_api",
                        "rights_status": "approved",
                        "source_class": "official_address_geocode",
                        "evidence_span": f"BRREG registers {name} ({org}) at {b_addr}; Kartverket maps the matching address to {kart_geo.get('cadastral_id')}.",
                        "metrics": {**kart_geo, "relationship_scope": "registered_business_address",
                                    "registry_source_url": reg_address_rec.get("source_url")},
                        "strategy": "kartverket_address_geocode",
                    })

    # 13j. Official Supervisory & Labour Inspection Licenses (Arbeidstilsynet & Finanstilsynet)
    if source_policy == EXPERIMENTAL and not is_passive_entity and can_run_optional(cost=1, reserve=5):
        try:
            n_val = (profile.get("evidence", {}).get("registry_live", {}).get("value") or {})
            n_raw = n_val.get("naeringskode1") or profile.get("industry_code") or ""
            n_code = n_raw.get("kode") if isinstance(n_raw, dict) else str(n_raw or "")
            reg_lics = fetch_regulatory_licenses(org, name, industry_code=n_code, budget=budget)
            if reg_lics:
                observations.extend(reg_lics)
                profile.setdefault("regulatory_licenses", []).extend(obs["metrics"] for obs in reg_lics if obs.get("metrics"))
        except Exception:
            pass

    # 13k. Miljøfyrtårn (Eco-Lighthouse) ESG Sustainability Certification
    if source_policy == EXPERIMENTAL and not is_passive_entity and (emp_count >= 5 or verified_website) and can_run_optional(cost=1, reserve=5):
        try:
            esg_obs = fetch_esg_certifications(org, name, budget=budget)
            if esg_obs:
                observations.extend(esg_obs)
                profile["esg_certifications"] = [obs.get("metrics") or {} for obs in esg_obs if obs.get("metrics")]
        except Exception:
            pass

    # 13l. Doffin (Norwegian Public Procurement Contracts & Tender Awards - B2G performance signals)
    if source_policy == EXPERIMENTAL and not is_passive_entity and can_run_optional(cost=1, reserve=4):
        try:
            doffin_obs = fetch_doffin_procurement(org, name, budget=budget, max_records=3)
            if doffin_obs:
                observations.extend(doffin_obs)
                profile["public_procurement"] = [obs.get("metrics") or {} for obs in doffin_obs if obs.get("metrics")]
        except Exception:
            pass

    # 13m. Oslo Børs / Euronext Oslo Disclosures (Stock Exchange Filings for Listed ASAs)
    if source_policy == EXPERIMENTAL and form_code_upper == "ASA" and can_run_optional(cost=1, reserve=4):
        try:
            bors_obs = fetch_oslo_bors_disclosures(org, name, budget=budget, max_records=3)
            if bors_obs:
                observations.extend(bors_obs)
                profile["stock_exchange_disclosures"] = [obs.get("metrics") or {} for obs in bors_obs if obs.get("metrics")]
        except Exception:
            pass

    # 13n. Innovasjon Norge Grants (Public Commercialization & Innovation Subsidies)
    if source_policy == EXPERIMENTAL and not is_passive_entity and (emp_count >= 5 or verified_website) and can_run_optional(cost=1, reserve=4):
        try:
            in_obs = fetch_innovasjon_norge_grants(org, name, budget=budget, max_records=3)
            if in_obs:
                observations.extend(in_obs)
                profile["innovation_grants"] = [obs.get("metrics") or {} for obs in in_obs if obs.get("metrics")]
        except Exception:
            pass

    return profile, observations


def main() -> int:
    parser = argparse.ArgumentParser(description="SignalPost Live Agent — Daily Evaluation")
    parser.add_argument("--organisations", "--input", "--company-file", dest="organisations", default="smoke-companies.jsonl", help="JSONL batch of organisation numbers")
    parser.add_argument("--org", "--organisation-number", dest="single_org", default=None, help="Directly evaluate a single company by its 9-digit organisation number without needing a file")
    parser.add_argument("--bulk", default="brreg-enheter.csv", help="Frozen BRREG bulk CSV snapshot")
    parser.add_argument("--output-dir", "--out", dest="output_dir", default="out/daily", help="Output directory")
    parser.add_argument("--output", default=None, help="Optional explicit path for envelopes.jsonl")
    parser.add_argument("--profiles-output", default=None, help="Optional explicit path for profiles.jsonl")
    parser.add_argument("--report", default=None, help="Optional explicit path for batch-report.json")
    parser.add_argument(
        "--audit-labels",
        default=None,
        help="Optional completed independent-review JSONL. Without it, the run exports an unreviewed audit queue and reports audit status as not measured.",
    )
    parser.add_argument("--run-id", default="daily-001", help="Unique run identifier")
    parser.add_argument("--expected-count", type=int, default=None, help="Expected number of companies")
    parser.add_argument("--max-orgs", "--limit", dest="max_orgs", type=int, default=None, help="Optional limit on number of companies")
    parser.add_argument("--workers", "--concurrency", dest="workers", type=int, default=16, help="Worker threads")
    parser.add_argument("--max-requests", type=int, default=None, help="Request budget (defaults to Builderr's 2,000 requests per 100 companies)")
    parser.add_argument(
        "--max-cost", type=float,
        help="Optional lower API spending limit in USD. The run is capped at $10 per 100 companies.",
    )
    parser.add_argument("--max-time", type=float, help="Max wall clock seconds (defaults to a batch-size-based allowance)")
    parser.add_argument("--resume", action="store_true", help="Resume from existing completed profiles")
    parser.add_argument("--resume-mode", choices=("reuse", "revalidate", "refresh"), default="reuse",
                        help="Reuse saved profiles, re-fetch using saved website hints, or collect fresh without seed hints")
    parser.add_argument("--previous-profiles", type=Path, help="Earlier profiles.jsonl to compare with a fresh run. Use a separate output directory.")
    parser.add_argument("--fast-bulk", action="store_true", help="Enable high-throughput bulk enrichment for 5,000+ company stress runs")
    parser.add_argument("--seed-dirs", default="", help="Comma-separated prior output directories to reuse verified live profiles from")
    parser.add_argument("--nav-feed-cache", default="out/nav-feed-index.json", help="Local checkpoint for a registered NAV feed")
    parser.add_argument(
        "--verified-site-cache",
        default="",
        help="Optional JSONL cache created by scripts/build_verified_site_cache.py. Cached URLs are re-fetched and re-gated; they are never trusted blindly.",
    )
    parser.add_argument(
        "--load-env",
        action="store_true",
        help="Explicitly load local .env credentials (off by default for reproducible evaluator runs).",
    )
    parser.add_argument(
        "--source-policy",
        choices=(OFFICIAL_SAFE, EXPERIMENTAL),
        default=OFFICIAL_SAFE,
        help="Publication route: official-safe by default; experimental is private measurement only.",
    )
    args = parser.parse_args()
    reset_nav_feed()
    validate_policy_name(args.source_policy)
    if args.load_env:
        _load_env_file()

    started_at = utc_now()
    wall_start = time.monotonic()

    # Read input first so budget scales cleanly for 300-company, 1,000-company, or 10,000-company evaluation batches
    if getattr(args, "single_org", None):
        clean_org = "".join(c for c in str(args.single_org) if c.isdigit())
        if len(clean_org) != 9:
            raise SystemExit(f"Invalid Norwegian organisation number: '{args.single_org}' (must be 9 digits)")
        org_inputs = [{"organisation_number": clean_org}]
    else:
        org_inputs = read_organisation_inputs(args.organisations)
    if args.max_orgs and args.max_orgs > 0:
        org_inputs = org_inputs[:args.max_orgs]
    orgs = [item["organisation_number"] for item in org_inputs]
    if args.expected_count is not None and len(orgs) != args.expected_count:
        raise SystemExit(f"Expected {args.expected_count} organisations, received {len(orgs)}")

    # Builderr standard quotas:
    # - 2,000 requests per 100 companies (20 requests per company)
    # - $10.0 API spend per 100 companies ($0.10 per company, e.g. $1,000 for 10,000 companies)
    # Scale with the batch size. The API spending ceiling can only be lowered.
    standard_request_allowance = int(len(orgs) * 20)
    if args.max_requests is not None and args.max_requests < 0:
        parser.error("--max-requests must be non-negative")
    if args.max_time is not None and (not math.isfinite(args.max_time) or args.max_time <= 0):
        parser.error("--max-time must be a finite positive number of seconds")
    if args.max_cost is not None and (not math.isfinite(args.max_cost) or args.max_cost < 0):
        parser.error("--max-cost must be a finite non-negative amount")
    effective_max_requests = args.max_requests if args.max_requests is not None else standard_request_allowance
    protected_request_reserve = min(int(effective_max_requests * 0.05), max(50, int(args.workers * 15)))
    usable_requests = max(0, effective_max_requests - protected_request_reserve)
    company_budget_pace = max(12, int(usable_requests / max(1, len(orgs))))
    standard_time_allowance = max(2700.0, (len(orgs) / 100.0) * 2700.0)
    effective_max_time = args.max_time if args.max_time is not None else standard_time_allowance
    standard_cost_allowance = len(orgs) * 10 / 100
    effective_max_cost = (
        min(args.max_cost, standard_cost_allowance) if args.max_cost is not None else standard_cost_allowance
    )

    # Initialize budget tracker
    budget = BudgetTracker(
        max_requests=effective_max_requests,
        max_cost_usd=effective_max_cost,
        max_time_seconds=effective_max_time,
        request_reserve=protected_request_reserve,
    )

    # Output paths
    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    profiles_out = Path(args.profiles_output) if args.profiles_output else (out_dir / "profiles.jsonl")
    previous_profiles = {}
    if args.previous_profiles:
        if args.resume_mode != "refresh" or args.previous_profiles.resolve().parent == profiles_out.resolve().parent or args.previous_profiles.resolve().parent == out_dir.resolve():
            parser.error("--previous-profiles requires --resume-mode refresh and a separate output directory")
        if not args.previous_profiles.is_file():
            parser.error(f"Previous profiles file is missing: {args.previous_profiles}")
        with args.previous_profiles.open(encoding="utf-8-sig") as previous_file:
            for line in previous_file:
                if line.strip():
                    previous_profile = json.loads(line)
                    previous_profiles[previous_profile["organisation_number"]] = previous_profile
    envelopes_out = Path(args.output) if args.output else (out_dir / "envelopes.jsonl")
    observations_out = out_dir / "all-observations.jsonl"
    labels_out = out_dir / "external-audit-labels.jsonl"
    audit_labels_path = Path(args.audit_labels) if args.audit_labels else None
    batch_report_out = Path(args.report) if args.report else (out_dir / "batch-report.json")
    external_report_out = out_dir / "external-report.json"
    score_report_out = out_dir / "score-report.json"

    print(f"[INFO] Processing {len(orgs)} companies | Budget: {effective_max_requests} requests, ${effective_max_cost} API cost, {effective_max_time}s wall clock")
    print(f"[INFO] Protected request reserve: {protected_request_reserve} | Source policy: {args.source_policy}")

    # Load profiles from bulk registry
    profiles, registry_meta = profiles_from_bulk(args.bulk, orgs, fetcher=budget.fetch_json)
    print(f"[INFO] Loaded {len(profiles)} profiles from bulk registry ({registry_meta['registry_rows_scanned']} rows scanned)")

    # Annotations
    annotations = {item["organisation_number"]: item for item in org_inputs}
    for profile in profiles:
        for key in ("evaluation_split", "sample_slice", "source_leads"):
            if key in annotations[profile["organisation_number"]]:
                profile[key] = annotations[profile["organisation_number"]][key]

    cache_path = Path(args.verified_site_cache) if args.verified_site_cache else Path("config/verified-sites-cache.jsonl")
    if cache_path.exists() and (args.verified_site_cache or args.resume_mode != "refresh"):
        cache_rows = [json.loads(line) for line in cache_path.read_text(encoding="utf-8").splitlines() if line.strip()]
        cache_by_org = {str(row.get("organisation_number")): row for row in cache_rows}
        attached = 0
        for profile in profiles:
            cached = cache_by_org.get(str(profile.get("organisation_number")))
            if cached and cached.get("url"):
                profile["verified_site_cache"] = cached
                attached += 1
        print(f"[INFO] Attached {attached} revalidation-only verified-site cache entries from {cache_path}")
    elif args.verified_site_cache:
        raise SystemExit(f"Verified-site cache does not exist: {cache_path}")

    # Define modules
    requested_modules = ["registry", "accounting_obligation", "registry_live", "financials", "roles", "group", "locations", "website"]
    fetch_modules = set(requested_modules) - {"registry", "accounting_obligation", "website"}

    # Check resume state or explicit seed directories
    state: dict[str, dict] = {}
    resumed_observations: list[dict[str, Any]] = []
    seed_paths: list[Path] = []
    if args.resume_mode != "refresh" and args.resume and out_dir.exists():
        seed_paths.append(out_dir)
    if args.resume_mode != "refresh" and args.seed_dirs:
        for sd in args.seed_dirs.split(","):
            sd_p = Path(sd.strip())
            if sd_p.exists():
                seed_paths.append(sd_p)

    wanted_set = set(orgs)
    for sp in seed_paths:
        sp_prof = sp / "profiles.jsonl"
        sp_obs = sp / "all-observations.jsonl"
        if sp_prof.exists():
            newly_seeded: set[str] = set()
            with sp_prof.open("r", encoding="utf-8") as f:
                for line in f:
                    if not line.strip():
                        continue
                    p = json.loads(line)
                    org_id = str(p.get("organisation_number") or "")
                    if org_id in wanted_set and org_id not in state:
                        if (p.get("evidence", {}).get("registry_live", {}).get("value") or {}).get("organisation_number") == org_id:
                            state[org_id] = p
                            newly_seeded.add(org_id)
            if newly_seeded and sp_obs.exists():
                with sp_obs.open("r", encoding="utf-8") as f:
                    for line in f:
                        if not line.strip():
                            continue
                        obs = json.loads(line)
                        if str(obs.get("organisation_number") or "") in newly_seeded:
                            resumed_observations.append(obs)
    if args.resume_mode == "revalidate":
        for profile in profiles:
            previous = state.get(str(profile["organisation_number"])) or {}
            website = ((previous.get("evidence") or {}).get("website") or {}).get("value") or {}
            if website.get("final_url") and (website.get("identity_assessment") or {}).get("publishable"):
                profile["verified_site_cache"] = {"url": website["final_url"]}
        state.clear()
        resumed_observations.clear()
    reused_orgs = set(state)
    if state:
        if args.source_policy == OFFICIAL_SAFE:
            verified_site_orgs = set()
            for org, profile in state.items():
                website = (profile.get("evidence") or {}).get("website") or {}
                gated = apply_entity_identity_gate(profile, website, require_legal_operator=True)
                profile["website"] = published_website_url(gated["website"])
                if profile["website"]:
                    verified_site_orgs.add(org)
            website_strategies = {"website_news_discovery", "website_social_discovery", "company_site_job", "youtube_linked_channel_api"}
            resumed_observations = [row for row in resumed_observations
                                    if row.get("strategy") not in website_strategies
                                    or row.get("organisation_number") in verified_site_orgs]
        existing_ids = {ob.get("id") for ob in resumed_observations}
        for prof_obj in state.values():
            for ob in extract_website_job_observations(prof_obj):
                if ob.get("kind") in {"company_site_ad", "verified_career_ad"} and ob["id"] not in existing_ids:
                    resumed_observations.append(ob)
                    existing_ids.add(ob["id"])
        obs_by_org: dict[str, list[dict[str, Any]]] = {}
        accepted_resumed, _ = filter_publishable_observations(resumed_observations, policy=args.source_policy)
        for ob in accepted_resumed:
            obs_by_org.setdefault(str(ob.get("organisation_number") or ""), []).append(ob)
        for org_id, prof_obj in state.items():
            approved_observations = obs_by_org.get(org_id, [])
            nav_jobs = [row for row in approved_observations if row.get("strategy") == "nav_official_vacancy_feed"]
            if (prof_obj.get("nav_feed_diagnostics") or {}).get("verified_ads") and not nav_jobs:
                prof_obj["nav_feed_diagnostics"] = {"feed_state": "stored_ads_require_current_registered_verification"}
                hiring, _ = evaluate_company_hiring(prof_obj, approved_observations, nav_feed_jobs=[])
                prof_obj["hiring"] = hiring
                hiring_record = (prof_obj.get("evidence") or {}).get("hiring")
                if hiring_record:
                    hiring_record["value"] = hiring
        print(f"[INFO] Loaded {len(state)} completed profiles and {len(resumed_observations)} observations from seed/resume state")

    all_observations: list[dict[str, Any]] = list(resumed_observations)
    pending_profiles = [profile for profile in profiles if profile["organisation_number"] not in state]
    completed = len(state)
    def worker_fn(profile, fetch_modules, requested_modules, budget, source_policy, company_budget_pace):
        with budget.attributed_to(str(profile["organisation_number"])):
            return enrich_single_company(profile, fetch_modules, requested_modules, budget, source_policy, company_budget_pace,
                                         nav_feed_cache=Path(args.nav_feed_cache))
    progress_step = 500 if len(profiles) >= 2000 else 10

    # Enrich pending companies in parallel
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {
            pool.submit(worker_fn, profile, fetch_modules, requested_modules, budget, args.source_policy, company_budget_pace): profile["organisation_number"]
            for profile in pending_profiles
        }
        for future in as_completed(futures):
            org = futures[future]
            try:
                profile, observations = future.result()
                all_observations.extend(observations)
                state[profile["organisation_number"]] = profile
                completed += 1
                if completed % progress_step == 0 or completed == len(profiles):
                    elapsed = time.monotonic() - wall_start
                    print(f"[PROGRESS] {completed}/{len(profiles)} companies | {budget.requests} requests used | {elapsed:.0f}s elapsed")
            except Exception as exc:
                print(f"[ERROR] Company {org}: {type(exc).__name__}: {str(exc)[:200]}")
                fallback_p = next((p for p in profiles if p.get("organisation_number") == org), None)
                if fallback_p:
                    from norway_company_agent.evidence import evidence as make_ev
                    for mod in requested_modules:
                        if mod not in fallback_p.get("evidence", {}):
                            fallback_p.setdefault("evidence", {})[mod] = make_ev(
                                mod,
                                "source_error",
                                "official_api",
                                f"https://data.brreg.no/enhetsregisteret/api/enheter/{org}",
                                note=f"Worker exception: {type(exc).__name__}",
                                retrieved_at=utc_now(),
                            )
                    state[org] = fallback_p
                completed += 1

    flush_nav_feed_details(Path(args.nav_feed_cache))

    # Merge completed state back into profiles order
    profiles = [state.get(org, p) for org, p in zip(orgs, profiles)]
    budget_report = budget.report()
    request_attribution = budget_report["request_attribution"]
    for profile in profiles:
        row = request_attribution["by_company"].get(str(profile.get("organisation_number")), {})
        profile["collection_mode"] = "reused_snapshot" if str(profile.get("organisation_number")) in reused_orgs else "live"
        profile.setdefault("run_metrics", {}).update({
            "requests": int(row.get("requests") or 0),
            "bytes": int(row.get("bytes") or 0),
            "third_party_cost_usd": round(float(row.get("cost_usd") or 0), 4),
            "request_scope": "current_run_company_attributed",
        })

    completed_at = utc_now()
    wall_elapsed = time.monotonic() - wall_start

    # Keep rejected routes for the learning harness, but remove them before
    # scoring, audit, showcase, or profile-claim publication.
    publishable_observations, quarantined_observations = filter_publishable_observations(
        all_observations, policy=args.source_policy
    )

    # Generate an explicitly unreviewed audit queue. Never seed reviewer verdicts
    # from fields asserted by the agent itself.
    profile_by_org = {str(profile.get("organisation_number")): profile for profile in profiles}
    label_rows = []
    for obs in publishable_observations:
        if not publishable_observation(obs):
            continue
        profile = profile_by_org.get(str(obs.get("organisation_number")), {})
        legal_identity = profile.get("legal_identity") or {}
        label_rows.append({
            "id": obs["id"],
            "organisation_number": str(obs.get("organisation_number") or ""),
            "expected_company_name": legal_identity.get("name") or profile.get("name") or profile.get("legal_name"),
            "platform": obs.get("platform"),
            "signal_type": obs.get("signal_type"),
            "claim": obs.get("metrics"),
            "claim_text": obs.get("text"),
            "title": obs.get("title"),
            "sentiment_label": obs.get("sentiment_label"),
            "source_url": obs.get("source_url"),
            "retrieved_at": obs.get("retrieved_at"),
            "content_sha256": obs.get("content_sha256"),
            "valid_through": obs.get("valid_through"),
            "application_due": obs.get("application_due"),
            "nav_feed": obs.get("nav_feed"),
            **observation_provenance(obs),
            "identity_proof": obs.get("identity_proof"),
            "acquisition_mode": obs.get("acquisition_mode"),
            "rights_status_claimed": obs.get("rights_status"),
            "exact_entity": None,
            "metric_correct": None,
            "evidence_supported": None,
            "evidence_span_valid": None,
            "connector_policy_passed": None,
            "sentiment_correct": None,
            "reviewer": None,
            "reviewed_at": None,
            "review_notes": None,
        })

    # Generate envelopes
    if previous_profiles:
        from norway_company_agent.refresh import diff_profile

        for profile in profiles:
            org = profile["organisation_number"]
            if org in previous_profiles:
                profile["change_history"] = diff_profile(previous_profiles[org], profile)

    envelopes = [
        terminal_envelope(
            profile, run_id=args.run_id, modules=requested_modules,
            started_at=started_at, completed_at=completed_at,
            first_party_activity_only=False,
            require_legal_operator=args.source_policy == OFFICIAL_SAFE,
        )
        for profile in profiles
    ]
    validation = validate_envelopes(envelopes, len(orgs))

    # Evaluate external footprint
    publishable = [obs for obs in publishable_observations if publishable_observation(obs)]
    external_orgs = {obs["organisation_number"] for obs in publishable}
    external_platforms = {obs["platform"] for obs in publishable}

    coverage = {
        "two_platforms": len([org for org in set(o["organisation_number"] for o in publishable)
                            if len({ob["platform"] for ob in publishable if ob["organisation_number"] == org}) >= 2]) / max(len(orgs), 1),
        "workforce_jobs": len({o["organisation_number"] for o in publishable if o["signal_type"] in {"workforce_snapshot", "job_posting"}}) / max(len(orgs), 1),
        "open_jobs": len({o["organisation_number"] for o in publishable if o["signal_type"] == "job_posting"}) / max(len(orgs), 1),
        "ratings_reviews": len({o["organisation_number"] for o in publishable if o["signal_type"] in {"review", "review_summary"}}) / max(len(orgs), 1),
        "buzz_engagement": len({o["organisation_number"] for o in publishable if o["signal_type"] in {"public_post", "public_mention"} or (o["signal_type"] == "buzz_metrics" and o.get("strategy") != "social_discovery_buzz")}) / max(len(orgs), 1),
        "sentiment": len({o["organisation_number"] for o in publishable if o.get("sentiment_label") and o["signal_type"] in {"review", "public_post", "public_mention"} and o.get("source_class") in INDEPENDENT_SENTIMENT_CLASSES}) / max(len(orgs), 1),
    }

    audit_passed = False

    # Budget report
    # The same budget snapshot backs the envelopes and run report.

    # Evaluation Contract Measurement schema
    claim_fields = ["legal_identity", "company_status", "official_website", "registry_declared_website", "norwegian_website_presence",
                    "group_website_company_page",
                    "registry_website_redirect", "annual_accounts", "annual_accounts_status", "annual_accounts_comparison",
                    "roles", "operating_locations", "registered_subunit_trading_names", "workforce_source_counts",
                    "group_membership", "hiring_status", "hiring_signal", "first_party_recruitment_notice", "accounting_obligation", "dated_news",
                    "job_postings", "construction_approval", "registry_events"]
    per_field = {}
    abstentions = {"total": 0, "by_state": {}, "by_field": {}}
    for env in envelopes:
        for c in env.get("claims", []):
            f = c.get("field")
            st = c.get("availability")
            if st != "available":
                abstentions["total"] += 1
                abstentions["by_state"][st] = abstentions["by_state"].get(st, 0) + 1
                abstentions["by_field"][f] = abstentions["by_field"].get(f, 0) + 1
    for f in claim_fields:
        field_avail = sum(1 for e in envelopes for c in e.get("claims", []) if c.get("field") == f and c.get("availability") == "available")
        per_field[f] = {
            "company_coverage": round(field_avail / max(len(envelopes), 1), 3),
            "precision": None,
            "recall": None,
        }

    website_funnel = dict.fromkeys(("registry_declared", "attempted", "fetched", "exact_operator_published",
                                  "scoped_group_page_published", "conflicting_operator", "blocked",
                                  "failed", "unverified", "unattempted"), 0)
    for envelope, profile in zip(envelopes, profiles):
        available = {c["field"]: c["value"] for c in envelope["claims"] if c["availability"] == "available"}
        declared = available.get("registry_declared_website")
        if not declared:
            continue
        website_funnel["registry_declared"] += 1
        try:
            target = (normalize_homepage(declared) or "").rstrip("/")
        except ValueError:
            website_funnel["unattempted"] += 1
            continue
        attempts = []
        for attempt in profile.get("website_attempts") or []:
            try:
                if target and (normalize_homepage(attempt.get("requested_url")) or "").rstrip("/") == target:
                    attempts.append(attempt)
            except ValueError:
                continue
        if not attempts:
            website_funnel["unattempted"] += 1
            continue
        website_funnel["attempted"] += 1
        states = {attempt.get("status") for attempt in attempts}
        website_funnel["fetched"] += int("available" in states)
        if available.get("official_website") and any(attempt.get("publishable") for attempt in attempts):
            result = "exact_operator_published"
        elif available.get("group_website_company_page"):
            result = "scoped_group_page_published"
        elif any(attempt.get("operator_conflict") for attempt in attempts):
            result = "conflicting_operator"
        elif "available" in states:
            result = "unverified"
        elif "blocked" in states:
            result = "blocked"
        else:
            result = "failed"
        website_funnel[result] += 1

    measurements = {
        "exact_company_precision": None,
        "wrong_company_publications": None,
        "evidence_span_validity": None,
        "crawl_completion": len(envelopes) / max(len(orgs), 1),
        "refresh_correctness": None,
        "false_change_rate": None,
        "cost_per_company": round(budget_report["total_cost_usd"] / max(len(orgs), 1), 4),
        "request_count": budget_report["total_requests"],
        "p50_ms": budget_report["p50_ms"],
        "p95_ms": budget_report["p95_ms"],
        "per_field_precision_recall_coverage": per_field,
        "registry_website_verification": website_funnel,
        "abstention": abstentions,
    }

    external_report = {
        "audit_status": "not_requested",
        "qualification_passed": False,
        "audit_size_gate": audit_passed,
        "exact_company_precision": None,
        "wrong_entity_publications": None,
        "wrong_company_publications": None,
        "unsupported_publications": None,
        "evidence_span_validity": None,
        "audit_queue_rows": len(label_rows),
        "audited_observations": 0,
        "published_audited": 0,
        "incomplete_audit_rows": len(label_rows),
        "connector_policy_passed": None,
        "fresh_coverage": len(external_orgs - reused_orgs) / max(len(orgs), 1),
        "coverage": coverage,
        "platforms": sorted(external_platforms),
        "total_observations": len(all_observations),
        "source_policy": args.source_policy,
        "quarantined_observations": len(quarantined_observations),
        "publishable_observations": len(publishable),
        "companies_with_external_data": len(external_orgs),
        "measurements": measurements,
    }

    batch_report = {
        "run_id": args.run_id,
        "started_at": started_at,
        "completed_at": completed_at,
        "wall_clock_seconds": round(wall_elapsed, 1),
        "expected_count": len(orgs),
        "emitted_envelopes": len(envelopes),
        "profiles_fetched_this_run": len(profiles) - len(reused_orgs),
        "resumed_profiles": len(reused_orgs),
        "resume_mode": args.resume_mode,
        "modules": requested_modules,
        "source_policy": args.source_policy,
        "registry": registry_meta,
        "operations": {
            "requests": budget_report["total_requests"],
            "bytes": budget_report["total_bytes"],
            "p50_ms": budget_report["p50_ms"],
            "p95_ms": budget_report["p95_ms"],
            "third_party_cost_usd": budget_report["total_cost_usd"],
        },
        "resource_budget_passed": budget_report["total_requests"] <= effective_max_requests and budget_report["elapsed_seconds"] <= effective_max_time and budget_report["total_cost_usd"] <= effective_max_cost,
        "measurements": measurements,
        "budget": budget_report,
        "external_connectors": {
            "google_news_rss": args.source_policy == EXPERIMENTAL,
            "news_credibility_engine": True,
            "linkedin_guest_discovery": args.source_policy == EXPERIMENTAL,
            "google_places_candidate_discovery": places_available(),
            "youtube_data_api": args.source_policy == EXPERIMENTAL and youtube_available(),
            "youtube_linked_channel_api": youtube_available(),
            "tavily_keyless_candidate_search": keyless_search_available(),
            "social_discovery": args.source_policy == EXPERIMENTAL,
            "wikidata_website_candidate_discovery": True,
            "enhetsregisteret_subunits": True,
            "patentstyret_nipo": patentstyret_available(),
        },
        "connector_diagnostics": {
            "google_places": places_status(),
            "tavily_keyless_search": {
                "configured": keyless_search_available(),
                "publication": "candidate discovery only; destination must pass exact-company verification",
            },
            "public_job_boards": {"providers": ["greenhouse", "lever", "ashby"], "requires": "verified company-site link and exact advert employer"},
        },
        "validation": validation,
    }

    # Proxy scoring computation (EXTERNAL CATEGORY NOW INTEGRATED WITH AUDIT)
    n = len(profiles)
    external_qualified = bool(external_report.get("qualification_passed"))
    published_audited = int(external_report.get("published_audited") or 0)
    zero_wrong = published_audited > 0 and external_report.get("wrong_entity_publications") == 0
    supported = (
        published_audited > 0
        and external_report.get("unsupported_publications") == 0
        and external_report.get("evidence_span_validity") == 1.0
    )

    def capped(weight: float, value: float | int | None) -> float:
        return round(weight * max(0.0, min(1.0, float(value or 0))), 3)

    def ratio(num: int, den: int) -> float:
        return num / den if den else 0.0

    entity_points = 10.0 if external_qualified and zero_wrong else 0.0
    breadth_points = capped(10, coverage.get("two_platforms")) if external_qualified else 0.0
    workforce_points = capped(7, coverage.get("workforce_jobs")) if external_qualified else 0.0
    review_points = capped(8, coverage.get("ratings_reviews")) if external_qualified else 0.0
    buzz_points = capped(7, coverage.get("buzz_engagement")) if external_qualified else 0.0
    sentiment_points = capped(10, coverage.get("sentiment")) if external_qualified else 0.0
    freshness_points = capped(3, external_report.get("fresh_coverage")) if external_qualified else 0.0

    external_score = {
        "verified_external_identity": entity_points,
        "multi_source_breadth": breadth_points,
        "workforce_and_jobs": workforce_points,
        "ratings_and_reviews": review_points,
        "buzz_and_engagement": buzz_points,
        "qualified_sentiment": sentiment_points,
        "external_freshness": freshness_points,
    }

    exact_registry = ratio(
        sum(
            (
                (row.get("evidence", {}).get("registry_live", {}).get("value") or {}).get("organisation_number")
                or (row.get("evidence", {}).get("registry", {}).get("value") or {}).get("organisasjonsnummer")
                or (row.get("evidence", {}).get("registry", {}).get("value") or {}).get("organisation_number")
                or (row.get("evidence", {}).get("registry", {}).get("status") == "available" and row.get("organisation_number"))
            )
            == row.get("organisation_number")
            for row in profiles
        ),
        n,
    )
    financial = ratio(sum(row.get("evidence", {}).get("financials", {}).get("status") == "available" for row in profiles), n)
    roles_locations = ratio(
        sum(all(module in row.get("evidence", {}) for module in ("roles", "locations")) for row in profiles),
        n,
    )
    website_terminal = ratio(sum("website" in row.get("evidence", {}) for row in profiles), n)
    foundation = {
        "official_identity": capped(4, exact_registry),
        "annual_accounts": capped(4, financial),
        "roles_and_locations": capped(4, roles_locations),
        "website_seed_and_terminal_state": capped(3, website_terminal),
    }

    p95 = budget_report.get("p95_ms")
    batch_valid = validation.get("passed", False)
    extensibility = {
        "terminal_daily_batch": 4.0 if batch_valid else 0.0,
        "deterministic_resume": None,
        "measured_refresh_diffs": None,
        "latency_budget": 1.0 if batch_valid and (p95 is None or p95 <= 10_000) else 0.0,
        "connector_rights_and_rate_policy": None,
    }

    categories = {
        "external_footprint_intelligence": round(sum(external_score.values()), 3),
        "official_company_foundation": round(sum(foundation.values()), 3),
        "research_agent": None,
        "daily_extensibility_refresh": None,
        "product_ux_design": None,
    }
    measured_subtotal = round(
        sum(value for value in categories.values() if value is not None)
        + sum(value for value in extensibility.values() if value is not None), 3
    )
    gates = {
        "external_audit_at_least_100": audit_passed,
        "zero_wrong_company_external_publications": zero_wrong,
        "external_claims_supported": supported,
        "external_connector_policy": bool(external_report.get("connector_policy_passed")),
        "official_identity_complete": exact_registry == 1.0,
        "terminal_batch_contract": batch_valid,
        "refresh_replay": False,
    }
    # Write all data files first so external audit and downstream tools can access them
    write_jsonl(profiles_out, profiles)
    write_jsonl(envelopes_out, envelopes)
    write_jsonl(observations_out, all_observations)
    write_jsonl(out_dir / "quarantined-observations.jsonl", quarantined_observations)
    write_jsonl(labels_out, label_rows)

    if audit_labels_path is None:
        print(f"[INFO] External audit not requested; wrote {len(label_rows)} unreviewed rows to {labels_out}")
    else:
        try:
            if not audit_labels_path.exists():
                raise FileNotFoundError(f"Audit labels do not exist: {audit_labels_path}")
            from scripts.external_audit_gate import call_external_audit
            external_report_path = out_dir / "external-report.json"
            print(f"[INFO] Running external audit gate with independent labels from {audit_labels_path}...")
            audit_report = call_external_audit(
                profiles_path=profiles_out,
                observations_path=observations_out,
                labels_path=audit_labels_path,
                output_path=external_report_path,
                min_audit=min(100, len(label_rows)),
                source_policy=args.source_policy,
            )
            external_qualified = audit_report.get("qualification_passed", False)
            audit_report["audit_status"] = "completed"
            print(f"[INFO] External audit completed: qualification_passed={external_qualified}")
            external_report.update(audit_report)
        except Exception as exc:
            external_report["audit_status"] = "failed"
            external_report["audit_error"] = str(exc)
            print(f"[WARN] External audit failed: {exc}")
    awardable_score = None

    score_report = {
        "scorer": "signalpost_local_measured_metrics_v2",
        "claim_boundary": "No 100-point score is assigned without independent audit, research, refresh and UX evaluation.",
        "rubric_weights": {
            "external_footprint_intelligence": 55,
            "official_company_foundation": 15,
            "research_agent": 10,
            "daily_extensibility_refresh": 12,
            "product_ux_design": 8,
        },
        "profiles": n,
        "details": {"external": external_score, "foundation": foundation, "extensibility": extensibility},
        "category_scores": categories,
        "raw_score": None,
        "measured_subtotal": measured_subtotal,
        "qualification_gates": gates,
        "qualification_passed": external_qualified,
        "awardable_score": None,
        "score_kind": "incomplete_local_measurement",
        "qualification_note": "Only an independent Builderr evaluation can establish qualification.",
    }

    # Generate research-report.json & ux-report.json for Synthesis (12 pts) & UX (8 pts) verification
    research_report = {
        "run_id": args.run_id,
        "companies_synthesized": len(envelopes),
        "grounded_rate": None,
        "unsupported_claims": None,
        "criteria_passed": {
            "what_company_does": None,
            "what_changed": None,
            "what_remains_unknown": None,
            "includes_sources": None,
            "grounded_without_unsupported_claims": None,
        },
        "summaries": [
            {
                "organisation_number": env["organisation_number"],
                "name": (env.get("legal_identity") or {}).get("name"),
                **(env.get("synthesis") or {}),
            }
            for env in envelopes[:500]
        ],
    }
    ux_report = {
        "run_id": args.run_id,
        "passed": None,
        "criteria_passed": {
            "find_companies": None,
            "compare_companies": None,
            "verify_evidence_and_sources": None,
            "desktop_responsive": None,
            "mobile_responsive": None,
        },
        "profiles_rendered": min(len(profiles), 500),
    }

    # Write reports and metadata
    batch_report_out.write_text(json.dumps(batch_report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (out_dir / "run-report.json").write_text(json.dumps(batch_report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    external_report_out.write_text(json.dumps(external_report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    score_report_out.write_text(json.dumps(score_report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (out_dir / "research-report.json").write_text(json.dumps(research_report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (out_dir / "ux-report.json").write_text(json.dumps(ux_report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    # Cache verified sites and preserve them through refreshes
    try:
        verified_cache_rows: list[dict] = []
        for profile in profiles:
            website = (profile.get("evidence") or {}).get("website") or {}
            value = website.get("value") or {}
            identity = value.get("identity_assessment") or {}
            url = value.get("final_url") or profile.get("website")
            if website.get("status") == "available" and identity.get("publishable") and url:
                verified_cache_rows.append({
                    "organisation_number": str(profile.get("organisation_number") or ""),
                    "url": str(url),
                    "identity_method": identity.get("method"),
                    "verified_at": website.get("retrieved_at"),
                    "source_content_sha256": website.get("content_sha256"),
                    "cache_policy": "revalidation_only",
                })
        if verified_cache_rows:
            verified_cache_rows.sort(key=lambda r: r["organisation_number"])
            (out_dir / "verified-sites-cache.jsonl").write_text(
                "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in verified_cache_rows),
                encoding="utf-8",
            )
            print(f"[INFO] Exported {len(verified_cache_rows)} verified-site revalidation hints to {out_dir}")
    except Exception as exc:
        print(f"[WARN] Failed to preserve verified-site cache: {exc}")

    # Generate Product UX & Design interactive showcase HTML (matching builderr.ai/signalpost)
    try:
        from norway_company_agent.showcase import write_showcase_html
        showcase_limit = min(max(100, len(profiles)), 500)
        showcase_out = out_dir / "showcase.html"
        write_showcase_html(profiles, publishable_observations, showcase_out, limit=showcase_limit,
                            reviewed_observations=int(external_report.get("published_audited") or 0))
        html_bytes = showcase_out.read_bytes()
        (out_dir / "index.html").write_bytes(html_bytes)
        if envelopes_out.parent.resolve() != out_dir.resolve():
            (envelopes_out.parent / "showcase.html").write_bytes(html_bytes)
            (envelopes_out.parent / "index.html").write_bytes(html_bytes)
            (envelopes_out.parent / "research-report.json").write_text(json.dumps(research_report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            (envelopes_out.parent / "ux-report.json").write_text(json.dumps(ux_report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"[INFO] Interactive showcase generated at {showcase_out}")
    except Exception as exc:
        print(f"[WARN] Failed to generate interactive showcase: {exc}")

    # Summary
    print(f"\n{'='*70}")
    print(f"[COMPLETE] SignalPost Live Agent finished!")
    print(f"  Companies: {len(profiles)}")
    print(f"  Envelopes: {len(envelopes)} ({'VALID' if validation['passed'] else 'INVALID'})")
    print(f"  Observations: {len(all_observations)} total, {len(publishable)} publishable")
    print(f"  Platforms: {', '.join(sorted(external_platforms))}")
    print(f"  Requests: {budget_report['total_requests']}/{effective_max_requests}")
    print(f"  API cost: ${budget_report['total_cost_usd']:.4f}")
    print(f"  Wall clock: {wall_elapsed:.1f}s")
    print(f"  Measured local subtotal: {measured_subtotal} points (no 100-point score assigned)")
    print(f"  Output: {out_dir}")
    print(f"{'='*70}")

    return 0 if validation["passed"] and batch_report["resource_budget_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
