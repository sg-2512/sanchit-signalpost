"""Strict publication gates for job-ad and news items.

Everything published under ``job_postings`` / ``dated_news`` must pass these gates.
They are pure functions so they can be unit-tested and reused by ``scripts/self_audit.py``.

Definitions
-----------
* Job item: ONE advertisement, at its own URL, posted by this organisation.
  Listing pages, career hubs, search pages and ads of other employers are rejected.
* News item: a real article with its own URL, a publication date read from the article
  (or from an item-level date on the company's own newsroom) and the company named in it.
  Registry notices, redirect links and hub pages are rejected.
"""
from __future__ import annotations

import re
import urllib.parse
from datetime import date, datetime, timezone
from typing import Any, Iterable

# --------------------------------------------------------------------------- shared helpers

_PATH_HUB_TOKENS = {
    "karriere", "career", "careers", "jobb", "jobs", "job", "stilling", "stillinger", "ledige-stillinger",
    "ledig-stilling", "vacancies", "vacancy", "work-with-us", "jobbe-hos-oss", "jobb-hos-oss", "rekruttering",
    "open-positions", "positions", "search", "sok", "nyheter", "aktuelt", "news", "presse", "pressemeldinger",
    "blogg", "blog", "media", "artikler", "articles", "siste-nytt", "innsikt", "om-oss", "about", "about-us",
    "kontakt", "contact", "en", "no", "nb", "nn", "se", "da", "de", "index", "home", "forside",
}

GENERIC_JOB_TITLES = {
    "vacancies", "offene stellen", "jobb hos oss", "jobbe hos oss", "ledige stillinger", "ledig stilling",
    "karriere", "careers", "career", "careers in", "work with us", "join our team", "our purpose",
    "contact us about careers", "åpen søknad", "open application", "stillinger", "jobs", "jobb", "jobber",
    "rekruttering", "open positions", "se alle stillinger", "alle stillinger", "les mer", "søk her",
    "søk stilling", "apply", "apply here", "klikk her", "se stillinger", "ledige jobber",
}
_GENERIC_JOB_TITLE_PARTS = (
    "karriere og rekruttering", "karriere hos", "career at", "careers at", "ledige stillinger", "jobb hos",
    "jobbe hos", "work with us", "join our team", "open positions", "alle stillinger", "ledig stilling (",
    "our purpose", "contact us", "meet the", "discover the stories", "summer internship",
)

GENERIC_NEWS_TITLES = {
    "nyheter", "aktuelt", "news", "presse", "pressemeldinger", "press releases", "siste nytt", "blogg",
    "artikler", "articles", "media", "innsikt", "nyhetsarkiv", "arkiv",
}

# Hosts that are never a news article about the company.
NON_NEWS_HOSTS = (
    "news.google.com", "google.com", "brreg.no", "patentstyret.no", "doffin.no", "mattilsynet.no",
    "miljofyrtarn.no", "rdap.norid.no", "norid.no", "arbeidsplassen.nav.no", "nav.no", "proff.no",
    "1881.no", "gulesider.no", "purehelp.no", "bisnode", "creditsafe", "linkedin.com", "facebook.com",
    "instagram.com", "finn.no", "wikipedia.org", "wikidata.org", "opencorporates.com", "gleif.org",
    "lovdata.no", "regnskapsregisteret", "brreg", "youtube.com",
)

_UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)


def _norm_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def host_of(url: str) -> str:
    try:
        return (urllib.parse.urlparse(str(url or "")).hostname or "").lower().removeprefix("www.")
    except Exception:
        return ""


def registered_domain(url_or_host: str) -> str:
    host = host_of(url_or_host) if "/" in str(url_or_host) else str(url_or_host or "").lower().removeprefix("www.")
    parts = [p for p in host.split(".") if p]
    if len(parts) <= 2:
        return ".".join(parts)
    if parts[-2] in {"co", "com", "org", "gov", "ac"} and len(parts[-1]) == 2:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def path_segments(url: str) -> list[str]:
    try:
        path = urllib.parse.urlparse(str(url or "")).path
    except Exception:
        return []
    return [seg for seg in path.split("/") if seg]


def _looks_like_leaf(segment: str) -> bool:
    """A leaf is an item slug or id, not a navigation word."""
    seg = segment.casefold().strip()
    if not seg or seg in _PATH_HUB_TOKENS:
        return False
    if _UUID_RE.fullmatch(seg) or (seg.isdigit() and len(seg) >= 4):
        return True
    if re.search(r"\d{4,}", seg):
        return True
    # slug with at least 2 words, e.g. "vi-soker-prosjektleder" or "equinor-investerer"
    return (seg.count("-") >= 2 and len(seg) >= 12) or (seg.count("-") >= 1 and len(seg) >= 16)


def is_hub_url(url: str) -> bool:
    segs = path_segments(url)
    parsed = urllib.parse.urlparse(str(url or ""))
    if not segs:
        return True
    if parsed.query and re.search(r"(^|&)(q|query|search|s|keyword|keywords|sok)=", parsed.query, re.I):
        return True
    # Schibsted / E24 news article pattern (e.g. /category/i/{article_id}/{slug})
    if len(segs) >= 2 and segs[-2] == "i":
        return False
    return not _looks_like_leaf(segs[-1])


def parse_iso_date(value: Any) -> date | None:
    text = _norm_text(value)
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})", text)
    if not m:
        return None
    try:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


# --------------------------------------------------------------------------- brand / identity

LEGAL_SUFFIXES = {"as", "asa", "ba", "da", "ans", "enk", "nuf", "sa", "sti", "iks", "ks", "ltd", "ab"}


def brand_tokens(company_name: str, site_url: str = "") -> set[str]:
    """Distinctive lowercase ascii tokens identifying this company (name words + site SLD)."""
    table = str.maketrans({"ø": "o", "å": "a", "æ": "ae", "Ø": "o", "Å": "a", "Æ": "ae"})
    words = re.findall(r"[a-z0-9]+", str(company_name or "").translate(table).casefold())
    while words and words[-1] in LEGAL_SUFFIXES:
        words.pop()
    tokens = {w for w in words if len(w) >= 4}
    if words:
        joined = "".join(words)
        if len(joined) >= 4:
            tokens.add(joined)
    if site_url:
        sld = registered_domain(site_url).split(".")[0].translate(table).casefold()
        if len(sld) >= 4:
            tokens.add(sld)
    return tokens


def name_in_text(company_name: str, text: str, *, min_tokens: int = 1) -> bool:
    """True when the full distinctive company name appears in ``text`` (token-sequence match)."""
    table = str.maketrans({"ø": "o", "å": "a", "æ": "ae", "Ø": "o", "Å": "a", "Æ": "ae"})
    name_words = re.findall(r"[a-z0-9]+", str(company_name or "").translate(table).casefold())
    while name_words and name_words[-1] in LEGAL_SUFFIXES:
        name_words.pop()
    if not name_words:
        return False
    text_words = re.findall(r"[a-z0-9]+", str(text or "").translate(table).casefold())
    n = len(name_words)
    if n == 1 and len(name_words[0]) < 5:
        return False
    for i in range(len(text_words) - n + 1):
        if text_words[i:i + n] == name_words:
            return True
    # also accept the concatenated brand ("Nordic Choice" -> "nordicchoice")
    joined = "".join(name_words)
    return len(joined) >= 6 and joined in set(text_words)


# --------------------------------------------------------------------------- jobs

ATS_HOST_SUFFIXES = (
    "webcruiter.no", "webcruiter.com", "jobbnorge.no", "teamtailor.com", "recman.no", "recman.io",
    "easycruit.com", "reachmee.com", "workbuster.com", "varbi.com", "jobylon.com", "homerun.co",
    "talentech.com", "talentech.io", "personio.de", "personio.com", "bamboohr.com", "lever.co",
    "greenhouse.io", "workable.com", "smartrecruiters.com", "hrmanager.no", "hr-manager.net",
    "cvpartner.com", "emply.com", "emply.net", "jobs.dayforce.com",
)


def is_ats_host(host: str) -> bool:
    host = host.lower()
    return any(host == suf or host.endswith("." + suf) for suf in ATS_HOST_SUFFIXES)


def ats_tenant_matches(url: str, tokens: Iterable[str]) -> bool:
    """The ATS tenant (subdomain or first path segment) must carry this company's brand."""
    tokens = {t for t in tokens if t}
    if not tokens:
        return False
    host = host_of(url)
    labels = host.split(".")
    tenant_candidates = [lbl for lbl in labels[:-2]] + [seg.casefold() for seg in path_segments(url)[:2]]
    table = str.maketrans({"ø": "o", "å": "a", "æ": "ae"})
    for cand in tenant_candidates:
        cand_n = re.sub(r"[^a-z0-9]", "", cand.translate(table).casefold())
        if len(cand_n) >= 4 and any(tok in cand_n or cand_n in tok for tok in tokens if len(tok) >= 4):
            return True
    return False


def is_generic_job_title(title: str) -> bool:
    t = _norm_text(title).casefold()
    if len(t) < 5:
        return True
    if t in GENERIC_JOB_TITLES:
        return True
    return any(part in t for part in _GENERIC_JOB_TITLE_PARTS)


_NAV_AD_RE = re.compile(r"^https://arbeidsplassen\.nav\.no/stillinger/stilling/[0-9a-f-]{8,}$", re.I)


def validate_job_item(
    item: dict[str, Any],
    *,
    org: str,
    company_name: str,
    site_url: str = "",
    site_publishable: bool = False,
    subunit_orgs: Any = (),
) -> tuple[bool, str]:
    """Return (ok, reason). Applied to every job item right before publication."""
    url = _norm_text(item.get("url"))
    title = _norm_text(item.get("title"))
    if not url.startswith(("http://", "https://")):
        return False, "no_url"
    if is_generic_job_title(title):
        return False, "generic_title"
    if not item.get("content_sha256"):
        return False, "no_evidence_hash"
    host = host_of(url)
    if host in {"rdap.norid.no", "news.google.com"} or host.endswith("linkedin.com"):
        return False, "disallowed_source"

    kind = str(item.get("kind") or "")
    if kind == "nav_ad" or host == "arbeidsplassen.nav.no":
        if not _NAV_AD_RE.match(url.split("?")[0].split("#")[0]):
            return False, "nav_not_single_ad"
        emp_org = str(item.get("employer_org") or "")
        sub_set = {str(s) for s in (subunit_orgs or ()) if s}
        if emp_org:
            if emp_org != str(org) and emp_org not in sub_set:
                return False, "nav_employer_orgnr_mismatch"
            return True, "nav_single_ad_exact_orgnr" if emp_org == str(org) else "nav_single_ad_subunit_orgnr"
        match_type = str(item.get("match_type") or "")
        emp_name = str(item.get("employer_name") or item.get("businessName") or "")
        if emp_name:
            if name_in_text(company_name, emp_name):
                return True, match_type if match_type in {"nav_employer_orgnr_match", "nav_company_core_match", "nav_company_subunit_match"} else "nav_company_name_match"
            return False, "nav_employer_name_mismatch"
        if match_type in {"nav_employer_orgnr_match", "nav_company_core_match", "nav_company_subunit_match"}:
            return True, match_type
        if not emp_name and item.get("exact_entity"):
            return True, "nav_single_ad_exact_entity"
        return False, "nav_employer_unverified"

    if host == "finn.no" or host.endswith(".finn.no"):
        if "finnkode=" not in url and "/stilling/" not in url:
            return False, "finn_not_single_ad"
        emp_org = str(item.get("employer_org") or "")
        sub_set = {str(s) for s in (subunit_orgs or ()) if s}
        if emp_org:
            if emp_org != str(org) and emp_org not in sub_set:
                return False, "finn_employer_orgnr_mismatch"
            return True, "finn_single_ad_exact_orgnr"
        emp_name = str(item.get("employer_name") or "")
        if emp_name and name_in_text(company_name, emp_name):
            return True, "finn_company_name_match"
        tokens = brand_tokens(company_name, site_url)
        if any(tok in title.lower() for tok in tokens if len(tok) >= 4):
            return True, "finn_brand_match"
        return False, "finn_employer_unverified"

    if is_hub_url(url):
        return False, "hub_or_search_url"
    if not site_publishable and kind != "ats_ad":
        return False, "site_identity_not_verified"

    tokens = brand_tokens(company_name, site_url)
    site_dom = registered_domain(site_url) if site_url else ""
    if is_ats_host(host):
        if kind == "jsonld_job" and item.get("hiring_org_match"):
            return True, "jsonld_job_ats_org_match"
        if not ats_tenant_matches(url, tokens):
            return False, "ats_tenant_not_this_company"
        return True, "ats_single_ad_tenant_match"
    if site_dom and registered_domain(url) == site_dom:
        if kind == "jsonld_job" and item.get("hiring_org_match") is False:
            return False, "jsonld_other_employer"
        return True, "company_site_single_ad"
    return False, "foreign_domain_unverified"


# --------------------------------------------------------------------------- news

def validate_news_item(
    item: dict[str, Any],
    *,
    company_name: str,
    site_url: str = "",
    today: date | None = None,
    max_age_days: int = 3 * 365 + 31,
) -> tuple[bool, str]:
    """Return (ok, reason) for one news item."""
    url = _norm_text(item.get("url"))
    title = _norm_text(item.get("title"))
    if not url.startswith(("http://", "https://")):
        return False, "no_url"
    if len(title) < 8 or title.casefold() in GENERIC_NEWS_TITLES:
        return False, "generic_title"
    if not item.get("content_sha256"):
        return False, "no_evidence_hash"
    host = host_of(url)
    site_dom = registered_domain(site_url) if site_url else ""
    is_company_site = bool(site_dom) and registered_domain(url) == site_dom
    if not is_company_site and any(bad in host for bad in NON_NEWS_HOSTS):
        return False, "non_news_host"
    lowered = f"{title} {url}".casefold()
    if any(tok in lowered for tok in ("offisiell registrering", "offisiell kunngj", "innsendt årsregnskap",
                                      "kunngjoring/hent_enhet", "regnskapsregisteret")):
        return False, "registry_notice_not_news"
    pub = parse_iso_date(item.get("published_at"))
    if pub is None:
        return False, "no_publication_date"
    ref = today or datetime.now(timezone.utc).date()
    if pub > ref:
        return False, "future_date"
    if (ref - pub).days > max_age_days:
        return False, "too_old"
    if host == "finn.no" or host.endswith(".finn.no"):
        if "finnkode=" not in url and "/stilling/" not in url:
            return False, "finn_not_single_ad"
        emp_org = str(item.get("employer_org") or "")
        sub_set = {str(s) for s in (subunit_orgs or ()) if s}
        if emp_org:
            if emp_org != str(org) and emp_org not in sub_set:
                return False, "finn_employer_orgnr_mismatch"
            return True, "finn_single_ad_exact_orgnr"
        emp_name = str(item.get("employer_name") or "")
        if emp_name and name_in_text(company_name, emp_name):
            return True, "finn_company_name_match"
        tokens = brand_tokens(company_name, site_url)
        if any(tok in title.lower() for tok in tokens if len(tok) >= 4):
            return True, "finn_brand_match"
        return False, "finn_employer_unverified"

    if is_hub_url(url):
        return False, "hub_or_listing_url"
    verification = str(item.get("verification") or "")
    if verification == "article_fetched" or item.get("name_in_article"):
        if not item.get("name_in_article"):
            return False, "company_not_named_in_article"
        return True, "article_verified"
    if (verification == "site_listing" or not verification) and is_company_site:
        return True, "company_newsroom_item"
    return False, "unverified_source"
