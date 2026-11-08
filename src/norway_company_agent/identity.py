from __future__ import annotations

import json
import re
import unicodedata
import urllib.parse
from typing import Any

from .verified_items import name_in_text, registered_domain


LEGAL_AND_GENERIC = {
    "as", "asa", "ans", "da", "enk", "iks", "sa", "sam", "sti", "stiftelsen",
    "nuf", "ab", "b", "v", "limited", "ltd", "inc", "plc", "the", "og", "and",
}
GEOGRAPHIC_TOKENS = {"norway", "norge", "nordic", "scandinavia", "norden"}
GENERIC_INDUSTRY_WORDS = {
    "forsikring", "holding", "invest", "eiendom", "gruppen", "consulting",
    "drift", "bygg", "service", "regnskap", "advokat", "revisjon", "transport",
    "teknologi", "utvikling", "partner", "handel", "salg", "industri",
}


def _tokens(value: Any) -> list[str]:
    text = str(value or "").translate(str.maketrans({"ø": "o", "Ø": "O", "å": "a", "Å": "A", "æ": "ae", "Æ": "AE"}))
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().casefold()
    return [token for token in re.findall(r"[a-z0-9]+", text) if token not in LEGAL_AND_GENERIC and len(token) > 1]


def _structured_names(value: Any) -> list[str]:
    names: list[str] = []
    if isinstance(value, dict):
        for key, child in value.items():
            if key in {"name", "legalName", "alternateName"} and isinstance(child, str):
                names.append(child)
            else:
                names.extend(_structured_names(child))
    elif isinstance(value, list):
        for child in value:
            names.extend(_structured_names(child))
    return names


def assess_website_identity(profile: dict[str, Any]) -> dict[str, Any]:
    website = profile.get("evidence", {}).get("website", {})
    value = website.get("value") or {}
    core = _tokens(profile.get("name"))
    final_url = value.get("final_url") or website.get("source_url") or ""
    hostname = urllib.parse.urlparse(final_url).hostname or ""
    structured_names = _structured_names(value.get("structured_organisations") or [])
    rendered = value.get("js_fallback") or {}
    homepage_identity_parts = [
        value.get("title"), value.get("description"), value.get("identity_text_excerpt"), hostname, *structured_names,
        rendered.get("title"),
    ]
    candidate_parts = [
        *homepage_identity_parts, value.get("main_text_excerpt"),
        *[page.get("title") for page in value.get("pages", [])],
        *[page.get("main_text_excerpt") for page in value.get("pages", [])],
        *[page.get("identity_text_excerpt") for page in value.get("pages", [])],
    ]
    candidate_parts.append(rendered.get("main_text_excerpt"))
    candidate_text = " ".join(str(part or "") for part in candidate_parts)
    homepage_candidate_text = " ".join(str(part or "") for part in [*homepage_identity_parts, value.get("main_text_excerpt"), rendered.get("main_text_excerpt")])
    normalized_candidate_text = " ".join(_tokens(candidate_text))
    candidate_tokens = set(_tokens(candidate_text))
    org_digits = re.sub(r"\D", "", str(profile.get("organisation_number") or ""))
    compact_candidate = re.sub(r"\D", "", candidate_text)
    compact_homepage_candidate = re.sub(r"\D", "", homepage_candidate_text)
    overlap = sorted(set(core) & candidate_tokens)
    ratio = len(overlap) / len(set(core)) if core else 0.0
    reasons = []
    parked_markers = (
        "domain is for sale", "domain for sale", "hugedomains", "parked at", "miss hosting",
        "her flytter snart en ny gjest", "has been informing visitors",
        "find the best information and most relevant links on all topics related to",
        "buy this domain", "kjøp dette domenet", "domenet er til salgs",
        "this domain is parked", "under construction / sedo", "sedo domain parking",
        "domeneshop parkering", "parkert hos domeneshop", "dan.com",
        "the sponsored listings displayed above", "related searches", "is parked free",
        "velkommen til vårt nye nettsted", "siden er under konstruksjon",
    )
    normalized_raw = unicodedata.normalize("NFKD", candidate_text).encode("ascii", "ignore").decode().casefold()
    homepage_token_sets = [set(_tokens(part)) for part in homepage_identity_parts if part]
    combined_homepage_tokens = set(_tokens(" ".join(str(part or "") for part in homepage_identity_parts if part)))
    exact_homepage_name = bool(
        core and (
            any(set(core).issubset(tokens) for tokens in homepage_token_sets)
            or (len(core) >= 2 and set(core).issubset(combined_homepage_tokens))
        )
    )
    # A short legal name can also be a product/brand.  If the captured site
    # identifies a *different* legal company, preserve the useful relationship
    # but do not call the site the exact legal entity's website merely because
    # the brand token appears (for example, a product site hosted by its parent).
    legal_name_norm = " ".join(_tokens(profile.get("name")))
    candidate_legal_entities = {
        " ".join(_tokens(match))
        for match in re.findall(
            r"(?:[A-Za-zÃ†Ã˜Ã…Ã¦Ã¸Ã¥][A-Za-zÃ†Ã˜Ã…Ã¦Ã¸Ã¥&.'’ -]{1,80})\s+(?:AS|ASA|ANS|DA|ENK|NUF)\b",
            candidate_text,
            re.I,
        )
    }
    candidate_legal_entities.discard("")
    mentions_target_legal_entity = bool(legal_name_norm and legal_name_norm in candidate_legal_entities)
    other_legal_entities = sorted(entity for entity in candidate_legal_entities if entity != legal_name_norm)
    substantive_homepage = len(str(value.get("main_text_excerpt") or "").strip()) >= 50 or len(str(value.get("title") or "").strip()) >= 15
    is_business_sports_club = bool(re.search(r"(?:^|\s)B\.?\s*I\.?\s*L\.?(?:\s|$)", str(profile.get("name") or ""), re.I))

    # Sovereign registry official management portal recognition (e.g. housing co-ops c/o BORI BBL -> bori.no)
    reg_val = (profile.get("evidence", {}).get("registry_live") or {}).get("value") or (profile.get("evidence", {}).get("registry") or {}).get("value") or {}
    addr_val = profile.get("address") or reg_val.get("forretningsadresse") or {}
    raw_addr = addr_val.get("adresse") if isinstance(addr_val, dict) else (reg_val.get("forretningsadresse.adresse") or reg_val.get("postadresse.adresse") or "")
    if not raw_addr:
        raw_addr = reg_val.get("forretningsadresse.adresse") or reg_val.get("postadresse.adresse") or ""
    addr_text = " ".join(raw_addr) if isinstance(raw_addr, list) else str(raw_addr or "")
    post_val = reg_val.get("postadresse") or {}
    post_raw = post_val.get("adresse") if isinstance(post_val, dict) else (reg_val.get("postadresse.adresse") or "")
    post_text = " ".join(post_raw) if isinstance(post_raw, list) else str(post_raw or "")
    combined_addr_text = f"{addr_text} {post_text}"
    co_match = re.search(r"\bc/o\s+([a-zA-Z0-9æøåÆØÅ]+)", combined_addr_text, re.I)
    co_manager = co_match.group(1).lower() if co_match else ""
    email_val = str(reg_val.get("epostadresse") or profile.get("email") or "")
    email_domain = email_val.split("@")[1].lower() if "@" in email_val else ""
    org_form = str(profile.get("legal_form") or reg_val.get("organisasjonsform.kode") or (reg_val.get("organisasjonsform") or {}).get("kode") or "").upper()
    is_co_op = org_form in {"SAM", "BRL", "BOL", "BA", "SA", "ESEK"}

    reg_hjemmeside = str(reg_val.get("hjemmeside") or profile.get("website") or "").lower()
    clean_reg_host = reg_hjemmeside.replace("https://", "").replace("http://", "").split("/")[0].replace("www.", "")
    clean_cand_host = hostname.lower().replace("www.", "")
    is_declared_hjemmeside = bool(clean_reg_host and clean_cand_host and clean_reg_host == clean_cand_host)

    known_bbl_managers = ("bori", "obos", "usbl", "tobb", "vestbo", "bonord", "nobl", "kobbl", "gbl", "sobo", "bbl")
    is_known_bbl = any(bbl in clean_cand_host or bbl in clean_reg_host for bbl in known_bbl_managers)

    is_error_or_login_url = any(
        err in final_url.lower() for err in ("error=true", "login_required", "/login", "/logg-inn")
    )

    domain_sld = clean_cand_host.split(".")[0] if "." in clean_cand_host else clean_cand_host
    domain_sld_alnum = re.sub(r"[^a-z0-9]", "", domain_sld)
    core_compact = "".join(core)
    brand_tokens = [t for t in core if t not in GENERIC_INDUSTRY_WORDS and t not in GEOGRAPHIC_TOKENS]
    non_brand_tokens = [t for t in core if t in GENERIC_INDUSTRY_WORDS or t in GEOGRAPHIC_TOKENS]
    acronym = "".join(t[0] for t in core) if len(core) >= 3 else ""

    # Brand Bridge: Trademark registered with Patentstyret (Route 9 Leader & Brand Bridge)
    patent_brands = []
    for pat in (profile.get("patents") or []):
        p_title = pat.get("title")
        if p_title and len(p_title) >= 3:
            p_toks = _tokens(p_title)
            if p_toks:
                patent_brands.append((p_toks, "".join(p_toks), re.sub(r"[^a-z0-9]", "", "".join(p_toks))))

    trademark_brand_match = False
    for p_toks, p_compact, p_alnum in patent_brands:
        if p_alnum and domain_sld_alnum == p_alnum and any(set(p_toks).issubset(tokens) for tokens in homepage_token_sets):
            trademark_brand_match = True
            break
        elif p_alnum and p_alnum in domain_sld_alnum and len(p_alnum) >= 4:
            if set(p_toks).issubset(combined_homepage_tokens) or (org_digits and org_digits in compact_candidate):
                trademark_brand_match = True
                break

    if any(marker in normalized_raw for marker in parked_markers):
        score = 0.1
        reasons.append("captured page is a parked, for-sale, or generic hosting placeholder")
    elif is_error_or_login_url:
        score = 0.2
        reasons.append("captured URL redirected to an authentication error or login wall")
    elif is_business_sports_club and "bedriftsidrett" not in normalized_candidate_text and "b i l" not in normalized_candidate_text:
        score = 0.3
        reasons.append("business sports-club entity points to the operating company's site without club evidence")
    elif org_digits and (
        org_digits in compact_homepage_candidate
        or (org_digits in compact_candidate and core_compact and domain_sld_alnum == core_compact)
    ):
        score = 1.0
        reasons.append("exact organisation number appears in homepage identity evidence")
    elif trademark_brand_match and substantive_homepage:
        score = 0.95
        reasons.append("verified registered trademark from Patentstyret matches website domain and homepage branding")
    elif len(core) >= 2 and exact_homepage_name:
        score = 0.95
        reasons.append("all normalized legal-name tokens appear together in homepage identity evidence")
    elif is_declared_hjemmeside and substantive_homepage and len(core) >= 2 and ratio >= 0.99:
        score = 0.95
        reasons.append("all normalized legal-name tokens appear on declared registry website")
    elif is_declared_hjemmeside and len(core) == 1 and core_compact and domain_sld_alnum == core_compact and substantive_homepage:
        score = 0.94
        reasons.append("declared registry homepage domain SLD matches single legal-name token")
    elif len(core) == 1 and exact_homepage_name and substantive_homepage and not other_legal_entities:
        score = 0.95
        reasons.append("single distinctive legal-name token appears in homepage identity evidence with substantive content")
    elif len(core) == 1 and exact_homepage_name and substantive_homepage and other_legal_entities:
        score = 0.75
        reasons.append("brand token appears on a site that identifies another legal entity; preserve as related brand, not exact legal website")
    elif is_declared_hjemmeside and len(core) >= 2 and core_compact and domain_sld_alnum == core_compact:
        score = 0.94
        reasons.append("declared registry homepage domain SLD matches concatenated legal-name tokens")
    elif (
        is_declared_hjemmeside
        and substantive_homepage
        and brand_tokens
        and non_brand_tokens
        and domain_sld_alnum == "".join(brand_tokens)
        and any(set(brand_tokens).issubset(tokens) for tokens in homepage_token_sets)
    ):
        score = 0.93
        reasons.append("declared registry homepage domain SLD and homepage match distinctive brand token")
    elif (
        is_declared_hjemmeside
        and substantive_homepage
        and len(core) >= 3
        and domain_sld_alnum == acronym
        and set(core).issubset(candidate_tokens)
    ):
        score = 0.93
        reasons.append("declared registry homepage acronym domain contains all legal-name tokens on site")
    elif (
        is_co_op
        and is_known_bbl
        and is_declared_hjemmeside
        and substantive_homepage
        and not is_error_or_login_url
        and "minside." not in clean_cand_host
    ):
        score = 0.91
        reasons.append("official registry declares verified BBL housing manager portal for co-op entity")
    elif ratio >= 0.75 and len(overlap) >= 2:
        score = 0.85
        reasons.append("most legal-name tokens appear, but exact identity is incomplete")
    elif ratio >= 0.5 and len(overlap) >= 2:
        score = 0.65
        reasons.append("partial legal-name overlap only")
    else:
        score = 0.3
        reasons.append("registry-linked URL lacks strong exact-entity identity evidence")
    is_group_parent = False
    if other_legal_entities:
        for other_ent in other_legal_entities:
            other_toks = set(_tokens(other_ent))
            shared_brand = set(brand_tokens or core) & other_toks
            has_group_word = any(w in other_toks or w in candidate_text.lower() for w in ("holding", "group", "gruppen", "konsern", "morforetak"))
            if shared_brand and has_group_word:
                is_group_parent = True
                break
    if not is_group_parent and any(marker in candidate_text.lower() for marker in ("del av konsernet", "konsernselskap", "et selskap i", "a subsidiary of")):
        is_group_parent = True

    # Zero-tolerance: if a different 9-digit org number is detected in page evidence
    # but does not match the target org number, force not publishable.
    if org_digits and len(org_digits) == 9:
        # Extract any 9-digit org-like numbers from the candidate text
        found_orgs = set(re.findall(r"\b\d{9}\b", candidate_text))
        if found_orgs and org_digits not in found_orgs:
            score = 0.0
            reasons.append(f"different 9-digit organisation number found in page evidence ({sorted(found_orgs)}) but target {org_digits} not present")
            status = "related_or_uncertain"
            relationship = "unrelated_or_confuser"
            return {
                "status": status,
                "score": score,
                "publishable": False,
                "legal_name_tokens": core,
                "matched_tokens": overlap,
                "relationship": relationship,
                "identified_legal_entities": other_legal_entities,
                "target_legal_entity_mentioned": mentions_target_legal_entity,
                "reasons": reasons,
                "method": "deterministic_name_org_evidence_v2",
            }

    if score >= 0.9:
        if is_co_op and is_known_bbl:
            relationship = "housing_management_portal"
        else:
            relationship = "exact_legal_entity"
    elif is_group_parent:
        relationship = "corporate_group_parent"
    elif is_co_op and is_known_bbl:
        relationship = "housing_management_portal"
    elif exact_homepage_name and other_legal_entities:
        relationship = "public_brand_or_related_entity"
    elif other_legal_entities and score < 0.5:
        relationship = "unrelated_or_confuser"
    else:
        relationship = "ambiguous_or_unproven"

    status = "exact" if score >= 0.9 else "review" if score >= 0.8 else "related_or_uncertain"
    return {
        "status": status,
        "score": score,
        "publishable": status == "exact",
        "legal_name_tokens": core,
        "matched_tokens": overlap,
        "relationship": relationship,
        "identified_legal_entities": other_legal_entities,
        "target_legal_entity_mentioned": mentions_target_legal_entity,
        "reasons": reasons,
        "method": "deterministic_name_org_evidence_v2",
    }


def assess_social_identity(profile: dict[str, Any], link: dict[str, str]) -> dict[str, Any]:
    core = _tokens(profile.get("name"))
    parsed = urllib.parse.urlparse(link.get("url") or "")
    path = parsed.path.strip("/")
    handle_text = urllib.parse.unquote(path)
    handle_tokens = _tokens(handle_text)
    handle_compact = "".join(handle_tokens)

    # Generic platform endpoints to reject
    generic_paths = {
        "share", "sharer", "sharer.php", "intent", "tweet", "login", "home",
        "policies", "privacy", "terms", "help", "about", "search", "explore",
        "watch", "feed", "groups", "hashtag", "pages", "events", "jobs",
    }
    first_segment = path.split("/")[0].casefold() if path else ""
    if first_segment in generic_paths or not handle_compact:
        return {
            **link,
            "identity_score": 0.1,
            "publishable": False,
            "matched_tokens": [],
            "reason": "social link points to generic platform page or share dialog",
            "method": "deterministic_social_handle_identity_v2",
        }

    matched = [token for token in core if token in handle_compact]
    core_compact = "".join(core)
    ratio = len(set(matched)) / len(set(core)) if core else 0.0

    has_geo_token = any(token in GEOGRAPHIC_TOKENS for token in core)
    brand_tokens = [t for t in core if t not in GENERIC_INDUSTRY_WORDS and len(t) >= 4]

    # Domain SLD matching (e.g. g3i.no -> g3i in instagram.com/g3i.no)
    website_url = str(profile.get("website") or (profile.get("evidence", {}).get("website", {}).get("value") or {}).get("final_url") or "")
    web_host = urllib.parse.urlparse(website_url).hostname or ""
    web_clean = web_host.lower().removeprefix("www.")
    domain_sld = web_clean.split(".")[0] if "." in web_clean else web_clean
    domain_match = bool(
        domain_sld
        and len(domain_sld) >= 3
        and domain_sld not in GENERIC_INDUSTRY_WORDS
        and domain_sld not in LEGAL_AND_GENERIC
        and domain_sld in handle_compact
    )

    patents_brands = []
    for pat in (profile.get("patents") or []):
        p_title = pat.get("title")
        if p_title and len(p_title) >= 3:
            p_toks = _tokens(p_title)
            if p_toks:
                patents_brands.append(re.sub(r"[^a-z0-9]", "", "".join(p_toks)))

    trademark_social_match = any(tb in handle_compact for tb in patents_brands if len(tb) >= 4)

    if core_compact and core_compact in handle_compact:
        score = 0.98
        reason = "normalized legal-name sequence appears in the social handle"
    elif trademark_social_match:
        score = 0.96
        reason = "verified registered trademark from Patentstyret appears in the social handle"
    elif len(core) == 1 and matched:
        score = 0.95
        reason = "single distinctive legal-name token appears in the social handle"
    elif domain_match:
        score = 0.95
        reason = "verified company website domain SLD appears in the social handle"
    elif not has_geo_token and brand_tokens and all(bt in handle_compact for bt in brand_tokens):
        score = 0.94
        reason = "distinctive brand name appears in social handle for entity with generic industry descriptor"
    elif ratio >= 0.75 and len(set(matched)) >= 2:
        score = 0.9
        reason = "most legal-name tokens appear in the social handle"
    else:
        score = 0.3
        reason = "social handle lacks strong exact-entity name evidence"
    return {
        **link,
        "identity_score": score,
        "publishable": score >= 0.9,
        "matched_tokens": matched,
        "reason": reason,
        "method": "deterministic_social_handle_identity_v2",
    }


def apply_website_identity_gate(profile: dict[str, Any], website: dict[str, Any]) -> dict[str, Any]:
    if website.get("status") != "available":
        return {"website": website, "assessment": None, "quarantined_social_links": 0}
    temporary_profile = {**profile, "evidence": {**profile.get("evidence", {}), "website": website}}
    value = website.get("value") or {}
    assessment = assess_website_identity(temporary_profile)
    # Zero-tolerance: if assessment flags a group parent, public brand of another entity,
    # or unrelated/confuser, do not publish website facts regardless of score.
    if assessment.get("relationship") in {"corporate_group_parent", "public_brand_or_related_entity", "unrelated_or_confuser"}:
        return {"website": website, "assessment": assessment, "quarantined_social_links": 0}
    value["identity_assessment"] = assessment
    rel = assessment.get("relationship") or "exact_legal_entity"
    value["relationship_label"] = rel
    if rel == "exact_legal_entity":
        value["relationship_note"] = "Verified official website of target legal entity."
    elif rel == "corporate_group_parent":
        value["relationship_note"] = (
            "Corporate parent or group website. Associated with target entity's corporate structure; "
            "facts describe group operations and require attribution."
        )
    elif rel == "public_brand_or_related_entity":
        value["relationship_note"] = (
            "This site uses the company name as a public brand but identifies another legal entity. "
            "Its facts must not be published as facts about the target legal entity without separate relationship evidence."
        )
    elif rel == "housing_management_portal":
        value["relationship_note"] = (
            "Official residential management or BBL portal for co-operative housing entity."
        )
    elif rel == "unrelated_or_confuser":
        value["relationship_note"] = (
            "Site identifies a distinct legal entity with no verified corporate or brand ownership relationship."
        )
    else:
        value["relationship_note"] = (
            "Ambiguous or partial relationship; identity evidence is incomplete."
        )
    original = list(value.get("discovered_social_links") or value.get("social_links") or [])
    value["discovered_social_links"] = original
    social_assessments = [assess_social_identity(profile, link) for link in original]
    value["social_link_assessments"] = social_assessments
    value["social_links"] = [
        {"platform": item["platform"], "url": item["url"]}
        for item in social_assessments
        if assessment["publishable"] and item["publishable"]
    ]
    website["value"] = value
    return {
        "website": website,
        "assessment": assessment,
        "quarantined_social_links": len(original) - len(value["social_links"]),
    }


# --------------------------------------------------------------------------- multi-factor entity identity (P0 precision system)

_PARKED_MARKERS = (
    "domain is for sale", "domain for sale", "hugedomains", "parked at", "miss hosting",
    "her flytter snart en ny gjest", "has been informing visitors",
    "find the best information and most relevant links on all topics related to",
    "buy this domain", "kjøp dette domenet", "domenet er til salgs",
    "this domain is parked", "under construction / sedo", "sedo domain parking",
    "domeneshop parkering", "parkert hos domeneshop", "dan.com",
    "the sponsored listings displayed above", "related searches", "is parked free",
    "velkommen til vårt nye nettsted", "siden er under konstruksjon",
)


def _find_org_numbers(text: str) -> set[str]:
    """Find all 9-digit organisation-number-like strings in text."""
    return set(re.findall(r"\b\d{9}\b", text or ""))


def _find_legal_entities(text: str) -> set[str]:
    """Find Norwegian legal-entity names (X AS, Y ASA, etc.) in text."""
    entities: set[str] = set()
    for match in re.finditer(
        r"(?:[A-Za-zÆØÅæøå][A-Za-zÆØÅæøå&.'’\- ]{1,80})\s+(?:AS|ASA|ANS|DA|ENK|NUF)\b",
        text or "",
        re.I,
    ):
        ent = " ".join(_tokens(match.group(0).strip()))
        if ent:
            entities.add(ent)
    return entities


def assess_entity_identity(
    profile: dict[str, Any],
    evidence_items: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Multi-factor exact-entity identity assessment (P0 precision system).

    Combines organisation number, legal name, domain, and address evidence into a
    single publishable/not-publishable decision.  Hard vetoes (negative
    disqualification rules) override any positive score.

    Publishable only when:
      * all vetoes pass, AND
      * orgnr factor == 1.0, OR (name factor >= 0.95 AND domain factor >= 0.90),
      * weighted multi-factor score >= 0.95.
    """
    org = str(profile.get("organisation_number") or "")
    org_digits = re.sub(r"\D", "", org)
    name = str(profile.get("name") or "")
    core = _tokens(name)

    website = (profile.get("evidence") or {}).get("website") or {}
    w_value = website.get("value") or {}
    final_url = w_value.get("final_url") or website.get("source_url") or ""
    hostname = urllib.parse.urlparse(final_url).hostname or ""

    # --- Collect all evidence text for cross-validation ---
    all_text_parts: list[str] = []
    if evidence_items:
        for item in evidence_items:
            for key in ("text", "title", "main_text_excerpt", "identity_text_excerpt",
                        "description", "claim_span", "value", "evidence_span"):
                v = item.get(key)
                if isinstance(v, str):
                    all_text_parts.append(v)
                elif isinstance(v, dict):
                    all_text_parts.append(json.dumps(v, ensure_ascii=False))
    homepage_parts = [
        w_value.get("title"), w_value.get("description"), w_value.get("identity_text_excerpt"),
        hostname, w_value.get("main_text_excerpt"),
    ]
    all_text_parts.extend(str(p or "") for p in homepage_parts if p)
    rendered = w_value.get("js_fallback") or {}
    all_text_parts.append(str(rendered.get("main_text_excerpt") or ""))
    for page in w_value.get("pages", []):
        all_text_parts.append(str(page.get("title") or ""))
        all_text_parts.append(str(page.get("main_text_excerpt") or ""))
        all_text_parts.append(str(page.get("identity_text_excerpt") or ""))

    combined_text = " ".join(all_text_parts)
    normalized_text = " ".join(_tokens(combined_text))
    candidate_tokens = set(_tokens(combined_text))

    # --- Factor 1: Organisation number ---
    found_orgs = _find_org_numbers(combined_text)
    orgnr_match = bool(org_digits and len(org_digits) == 9 and org_digits in found_orgs)
    orgnr_score = 1.0 if orgnr_match else 0.0

    # --- Factor 2: Legal name ---
    name_match = name_in_text(name, combined_text) if name else False
    overlap = sorted(set(core) & candidate_tokens)
    name_ratio = len(overlap) / len(set(core)) if core else 0.0
    name_score = 1.0 if name_match else (name_ratio if name_ratio > 0 else 0.0)

    # --- Factor 3: Domain ---
    domain_sld = hostname.split(".")[0] if "." in hostname else hostname
    domain_sld_alnum = re.sub(r"[^a-z0-9]", "", domain_sld)
    domain_match = bool(
        domain_sld_alnum
        and len(domain_sld_alnum) >= 3
        and domain_sld_alnum not in GENERIC_INDUSTRY_WORDS
        and domain_sld_alnum not in LEGAL_AND_GENERIC
        and domain_sld_alnum in combined_text.casefold()
    )
    domain_score = 0.95 if domain_match else 0.0

    # --- Factor 4: Registered address ---
    reg_val = (profile.get("evidence") or {}).get("registry_live") or {}
    reg_val = reg_val or (profile.get("evidence") or {}).get("registry") or {}
    addr_val = profile.get("address") or reg_val.get("value", {}).get("forretningsadresse") or {}
    if isinstance(addr_val, dict):
        raw_addr = addr_val.get("adresse") or ""
    else:
        raw_addr = str(addr_val or "")
    addr_text = " ".join(raw_addr) if isinstance(raw_addr, list) else str(raw_addr or "")
    addr_tokens = [t for t in _tokens(addr_text) if len(t) >= 4]
    addr_match = bool(addr_text and any(t in normalized_text for t in addr_tokens))
    addr_score = 0.85 if addr_match else 0.0

    # --- Negative disqualification rules (hard vetoes) ---
    veto_reasons: list[str] = []

    # Veto 1: Different 9-digit org number in evidence, target orgnr absent
    if org_digits and len(org_digits) == 9:
        if found_orgs and org_digits not in found_orgs:
            veto_reasons.append(f"different_org_number_in_evidence:{sorted(found_orgs)}")

    # Veto 2: Different legal entity identified that is not the target
    legal_name_norm = " ".join(core)
    candidate_legal_entities = _find_legal_entities(combined_text)
    candidate_legal_entities.discard("")
    candidate_legal_entities.discard(legal_name_norm)
    if candidate_legal_entities:
        brand_tokens = {t for t in core if t not in GENERIC_INDUSTRY_WORDS and t not in GEOGRAPHIC_TOKENS}
        other_entity_shares_brand = False
        for other_ent in candidate_legal_entities:
            other_toks = set(_tokens(other_ent))
            if brand_tokens & other_toks:
                other_entity_shares_brand = True
                break
        if other_entity_shares_brand and not name_match:
            veto_reasons.append(f"different_legal_entity_in_evidence:{sorted(candidate_legal_entities)}")

    # Veto 3: Parked / for-sale domain
    if any(marker in normalized_text for marker in _PARKED_MARKERS):
        veto_reasons.append("parked_or_for_sale_domain")

    # Veto 4: Error / login page
    is_error_or_login = any(err in final_url.lower() for err in ("error=true", "login_required", "/login", "/logg-inn"))
    if is_error_or_login:
        veto_reasons.append("error_or_login_page")

    # Veto 5/6: Group parent or public brand of another entity without target evidence
    if candidate_legal_entities and not name_match and not orgnr_match:
        combined_lower = combined_text.lower()
        has_group = any(w in combined_lower for w in ("holding", "group", "gruppen", "konsern", "morforetak"))
        if has_group:
            veto_reasons.append("corporate_group_parent_without_target")
        else:
            veto_reasons.append("public_brand_of_other_entity")

    # --- Weighted multi-factor score ---
    # Weights: orgnr (0.40), name (0.30), domain (0.20), address (0.10)
    factors = {
        "orgnr_match": orgnr_score,
        "name_match": name_score,
        "domain_match": domain_score,
        "address_match": addr_score,
    }
    weights = {"orgnr_match": 0.40, "name_match": 0.30, "domain_match": 0.20, "address_match": 0.10}
    weighted_score = sum(factors[k] * weights[k] for k in weights)

    # --- Publishable decision ---
    if veto_reasons:
        status = "rejected"
        publishable = False
    elif orgnr_score >= 1.0 and name_score >= 0.9:
        status = "exact"
        publishable = True
    elif orgnr_score >= 1.0 and domain_score >= 0.9:
        status = "exact"
        publishable = True
    elif name_score >= 0.95 and domain_score >= 0.90 and weighted_score >= 0.95:
        status = "exact"
        publishable = True
    else:
        status = "review"
        publishable = False

    return {
        "status": status,
        "publishable": publishable,
        "score": round(weighted_score, 3),
        "orgnr_match": orgnr_score,
        "name_match": name_score,
        "domain_match": domain_score,
        "address_match": addr_score,
        "veto_reasons": veto_reasons,
        "matched_tokens": overlap,
        "found_org_numbers": sorted(found_orgs),
        "candidate_legal_entities": sorted(candidate_legal_entities),
        "target_orgnr": org_digits,
        "method": "multi_factor_entity_identity_v1",
    }


def apply_entity_identity_gate(
    profile: dict[str, Any],
    website: dict[str, Any],
    evidence_items: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Apply both website identity and multi-factor entity identity gates.

    A website is publishable only when BOTH assessments pass.  This is the P0
    precision gate: it requires multi-factor confirmation (orgnr + name + domain)
    and applies hard vetoes that override any single-source score.
    """
    if website.get("status") != "available":
        return {"website": website, "assessment": None, "entity_assessment": None, "quarantined_social_links": 0}

    temporary_profile = {**profile, "evidence": {**profile.get("evidence", {}), "website": website}}
    website_assessment = assess_website_identity(temporary_profile)
    entity_assessment = assess_entity_identity(temporary_profile, evidence_items)

    # Zero-tolerance: either assessment can veto publication
    if not website_assessment.get("publishable") or not entity_assessment.get("publishable"):
        return {
            "website": website,
            "assessment": website_assessment,
            "entity_assessment": entity_assessment,
            "quarantined_social_links": 0,
            "gate_rejected": True,
            "rejection_reasons": entity_assessment.get("veto_reasons", []),
        }

    value = website.get("value") or {}
    value["identity_assessment"] = website_assessment
    value["entity_identity_assessment"] = entity_assessment
    rel = website_assessment.get("relationship") or "exact_legal_entity"
    value["relationship_label"] = rel
    if rel == "exact_legal_entity":
        value["relationship_note"] = "Verified official website of target legal entity (multi-factor confirmed)."
    elif rel == "corporate_group_parent":
        value["relationship_note"] = "Corporate parent or group website; facts require attribution."
    elif rel == "public_brand_or_related_entity":
        value["relationship_note"] = "Public brand of another legal entity; not publishable as target entity facts."
    elif rel == "housing_management_portal":
        value["relationship_note"] = "Official residential management portal for co-operative housing entity."
    elif rel == "unrelated_or_confuser":
        value["relationship_note"] = "Site identifies a distinct legal entity with no verified relationship."
    else:
        value["relationship_note"] = "Ambiguous or partial relationship; identity evidence is incomplete."

    original = list(value.get("discovered_social_links") or value.get("social_links") or [])
    value["discovered_social_links"] = original
    social_assessments = [assess_social_identity(profile, link) for link in original]
    value["social_link_assessments"] = social_assessments
    value["social_links"] = [
        {"platform": item["platform"], "url": item["url"]}
        for item in social_assessments
        if website_assessment["publishable"] and item["publishable"]
    ]
    website["value"] = value
    return {
        "website": website,
        "assessment": website_assessment,
        "entity_assessment": entity_assessment,
        "quarantined_social_links": len(original) - len(value["social_links"]),
    }