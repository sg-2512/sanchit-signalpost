"""Multi-Source Hiring & Recruitment Intelligence Engine.

Answers the competition briefing requirement: 'whether it appears to be hiring'.
Synthesizes 4 distinct, permitted evidence layers:
1. NAV Arbeidsplassen (Norway's official public employment service)
2. LinkedIn Guest Jobs (open corporate job postings in Norway)
3. Company Website Career Sections & Norwegian recruitment keywords
4. Brønnøysund Official Registry registered workforce snapshot
"""
from __future__ import annotations

import re
from typing import Any

from .connectors.nav_jobs import fetch_nav_jobs
from .connectors.linkedin_jobs import fetch_linkedin_jobs

NORWEGIAN_HIRING_PATTERNS = [
    re.compile(r"\bvi\s+s[øo]ker\b", re.I),
    re.compile(r"\bledige?\s+stilling(?:er)?\b", re.I),
    re.compile(r"\bbli\s+med\s+p[åa]\s+(?:v[åa]rt\s+)?(?:team|laget)\b", re.I),
    re.compile(r"\bkarriere\s+hos\s+oss\b", re.I),
    re.compile(r"\bwe(?:'re|\s+are)\s+hiring\b", re.I),
    re.compile(r"\bopen\s+positions?\b", re.I),
    re.compile(r"\bjoin\s+our\s+team\b", re.I),
    re.compile(r"\bjobb\s+hos\s+oss\b", re.I),
    re.compile(r"\bjobbe\s+hos\s+oss\b", re.I),
    re.compile(r"\bny(?:e)?\s+medarbeidere?\b", re.I),
]

CAREER_URL_PATTERNS = [
    re.compile(r"/karriere", re.I),
    re.compile(r"/jobb", re.I),
    re.compile(r"/career", re.I),
    re.compile(r"/vacanc", re.I),
    re.compile(r"/stilling", re.I),
    re.compile(r"/work-with-us", re.I),
]


def extract_website_hiring_signals(website_val: dict[str, Any] | None) -> dict[str, Any]:
    """Scan company website evidence for career pages and recruitment notices."""
    if not website_val or not isinstance(website_val, dict):
        return {"has_career_page": False, "career_urls": [], "hiring_keywords_found": []}

    career_urls: list[str] = []
    keywords_found: list[str] = []

    # 1. Check pages crawl list
    pages = website_val.get("pages") or []
    for page in pages:
        page_url = str(page.get("url") or "")
        page_title = str(page.get("title") or "")
        page_text = str(page.get("main_text_excerpt") or "")

        # Check URL
        if any(p.search(page_url) for p in CAREER_URL_PATTERNS):
            career_urls.append(page_url)

        # Check title
        if any(p.search(page_title) for p in NORWEGIAN_HIRING_PATTERNS):
            keywords_found.append(f"Title: '{page_title[:60]}'")

        # Check text
        for pat in NORWEGIAN_HIRING_PATTERNS:
            m = pat.search(page_text)
            if m:
                snippet = page_text[max(0, m.start() - 20) : min(len(page_text), m.end() + 40)].strip()
                keywords_found.append(f"Keyword '{m.group(0)}' ({snippet})")

    # 2. Check homepage main text & description
    desc = str(website_val.get("description") or "")
    main_text = str(website_val.get("main_text_excerpt") or "")
    combined = desc + " " + main_text
    for pat in NORWEGIAN_HIRING_PATTERNS:
        m = pat.search(combined)
        if m:
            snippet = combined[max(0, m.start() - 20) : min(len(combined), m.end() + 40)].strip()
            keywords_found.append(f"Found '{m.group(0)}' on homepage ({snippet})")

    return {
        "has_career_page": len(career_urls) > 0,
        "career_urls": sorted(set(career_urls)),
        "hiring_keywords_found": keywords_found[:5],
    }


def evaluate_company_hiring(
    profile: dict[str, Any],
    observations: list[dict[str, Any]] | None = None,
    *,
    budget: Any | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Perform multi-source hiring assessment for a company.

    Returns:
        (hiring_block, new_observations)
    """
    org = str(profile.get("organisation_number") or "")
    name = profile.get("name") or ""
    employees = profile.get("employees")

    evidence = profile.get("evidence") or {}
    website_val = (evidence.get("website") or {}).get("value")

    new_obs: list[dict[str, Any]] = []
    signals: list[dict[str, Any]] = []

    # 1. Official Registry Workforce Signal
    if employees is not None:
        signals.append({
            "source": "brreg_enhetsregisteret",
            "type": "registered_workforce",
            "detail": f"{employees} registered employee(s)",
            "count": employees,
        })

    # 1b. Statutory Annual Accounts Workforce (FTEs / Årsverk)
    fin_ev = evidence.get("financials") or {}
    fin_val = fin_ev.get("value") or {}
    notes_text = str(fin_val.get("notes") or fin_val.get("raw_notes") or fin_val.get("director_report") or "")
    if notes_text:
        ANNUAL_REPORT_FTE_PATTERNS = [
            re.compile(r"antall\s+årsverk\s+i\s+regnskapsåret\s+(?:er|var)?\s*(\d+)", re.I),
            re.compile(r"selskapet\s+har\s+sysselsatt\s+(\d+)\s+årsverk", re.I),
            re.compile(r"sysselsatt\s+(\d+)\s+årsverk", re.I),
            re.compile(r"(\d+)\s+årsverk\b", re.I),
            re.compile(r"antall\s+ansatte\s+(?:er|var|:)?\s*(\d+)", re.I),
        ]
        fte_count = None
        if re.search(r"\b(?:selskapet|foretaket)\s+har\s+ingen\s+ansatte\b", notes_text, re.I):
            fte_count = 0
        else:
            for pat in ANNUAL_REPORT_FTE_PATTERNS:
                m = pat.search(notes_text)
                if m:
                    try:
                        fte_count = int(m.group(1))
                        break
                    except ValueError:
                        pass
        if fte_count is not None:
            signals.append({
                "source": "regnskapsregisteret_annual_accounts",
                "type": "statutory_workforce_fte",
                "detail": f"{fte_count} statutory årsverk (FTEs) reported in annual accounts filing",
                "count": fte_count,
            })

    # 2. Website Career Section & Recruitment Keywords
    web_signals = extract_website_hiring_signals(website_val)
    if web_signals["has_career_page"]:
        signals.append({
            "source": "company_website",
            "type": "career_section",
            "detail": f"Active career section: {', '.join(web_signals['career_urls'])}",
            "urls": web_signals["career_urls"],
        })
    if web_signals["hiring_keywords_found"]:
        signals.append({
            "source": "company_website",
            "type": "recruitment_keywords",
            "detail": "; ".join(web_signals["hiring_keywords_found"][:2]),
        })

    # 3. NAV Arbeidsplassen Live Job Search
    nav_jobs: list[dict[str, Any]] = []
    if budget and budget.can_proceed(reserve=50):
        nav_jobs = fetch_nav_jobs(name, org, budget=budget)
        for job in nav_jobs:
            signals.append({
                "source": "nav_arbeidsplassen",
                "type": "official_job_posting",
                "title": job.get("job_title"),
                "url": job.get("source_url"),
                "published_at": job.get("published_at"),
            })
        new_obs.extend(nav_jobs)

    # 4. LinkedIn Guest Jobs Search
    li_jobs: list[dict[str, Any]] = []
    if budget and budget.can_proceed(reserve=50):
        li_jobs = fetch_linkedin_jobs(name, org, budget=budget)
        for job in li_jobs:
            signals.append({
                "source": "linkedin_jobs",
                "type": "corporate_job_posting",
                "title": job.get("job_title"),
                "url": job.get("source_url"),
                "published_at": job.get("published_at"),
            })
        new_obs.extend(li_jobs)

    # Determine overall hiring status
    active_postings_count = len(nav_jobs) + len(li_jobs)
    has_web_hiring = web_signals["has_career_page"] or bool(web_signals["hiring_keywords_found"])

    if active_postings_count > 0:
        appears_to_be_hiring = True
        status_code = "actively_recruiting"
        titles = [s.get("title") for s in signals if s.get("title")][:3]
        summary_note = f"Actively hiring: {active_postings_count} open vacancy posting(s) confirmed on NAV/LinkedIn ({', '.join(filter(None, titles))})."
    elif has_web_hiring:
        appears_to_be_hiring = True
        status_code = "website_recruitment_open"
        urls_str = ", ".join(web_signals["career_urls"]) if web_signals["career_urls"] else "website"
        summary_note = f"Appears to be hiring: career section / recruitment notices maintained on company {urls_str}."
    elif employees is not None and employees > 0:
        appears_to_be_hiring = False
        status_code = "workforce_active_no_open_postings"
        summary_note = f"No active public job postings detected; registered workforce stands at {employees} employee(s)."
    elif employees == 0:
        appears_to_be_hiring = False
        status_code = "zero_workforce_no_hiring"
        summary_note = "No active recruitment postings or registered workforce observed (0 registered employees)."
    else:
        appears_to_be_hiring = False
        status_code = "unreported_workforce_no_hiring"
        summary_note = "No active recruitment postings observed across checked sources; workforce size unfiled in registry."

    hiring_block = {
        "appears_to_be_hiring": appears_to_be_hiring,
        "status": status_code,
        "confidence": "high" if (active_postings_count > 0 or employees is not None) else "medium",
        "active_job_postings_count": active_postings_count,
        "registered_employees": employees,
        "sources_checked": [
            "nav_arbeidsplassen",
            "linkedin_jobs",
            "company_website",
            "brreg_workforce",
        ],
        "career_urls": web_signals["career_urls"],
        "signals": signals,
        "assessment": summary_note,
    }

    return hiring_block, new_obs
