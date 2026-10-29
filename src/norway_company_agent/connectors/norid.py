"""Norid (.no National Domain Registry) connector.

RDAP may expose a registrant display name while withholding an organisation
number.  Those two cases are deliberately distinct: a matching display name is
useful candidate-domain evidence, but only a visible exact organisation number
is legal ownership proof for publication under that entity.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
import urllib.parse
import urllib.request
from typing import Any

NORID_RDAP_ENDPOINT = "https://rdap.norid.no/domain"
USER_AGENT = "SignalPostAgent/1.0 (+https://builderr.ai/signalpost)"


def verify_norid_domain_ownership(
    domain_or_url: str,
    org_number: str,
    company_name: str = "",
    *,
    budget: Any | None = None,
    timeout: float = 5.0,
) -> dict[str, Any] | None:
    """Verify whether a .no domain is legally registered to the specified Norwegian org number.

    Returns:
        Structured observation dict if verified or checked, else None.
    """
    if not domain_or_url or not org_number:
        return None

    clean_org = org_number.strip().replace(" ", "")
    clean_domain = domain_or_url.lower().strip()
    if "://" in clean_domain:
        clean_domain = clean_domain.split("://")[-1]
    clean_domain = clean_domain.split("/")[0].split(":")[0]
    if clean_domain.startswith("www."):
        clean_domain = clean_domain[4:]

    if not clean_domain.endswith(".no") or len(clean_domain) < 4:
        return None

    if budget and not budget.can_proceed():
        return None

    start_time = time.perf_counter()
    bytes_received = 0
    retrieved_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    rdap_url = f"{NORID_RDAP_ENDPOINT}/{clean_domain}"

    try:
        req = urllib.request.Request(
            rdap_url,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "application/rdap+json,application/json",
            },
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read(200_000)
            bytes_received = len(raw)
            data = json.loads(raw.decode("utf-8", errors="replace"))

        elapsed_ms = int((time.perf_counter() - start_time) * 1000)
        if budget:
            budget.record_request(cost_usd=0.0, bytes_received=bytes_received, elapsed_ms=elapsed_ms)

        # In Norid RDAP, the registrar and registrant entities contain organization details
        entities = data.get("entities") or []
        registrant_org = ""
        is_exact_owner = False

        for entity in entities:
            roles = entity.get("roles") or []
            if "registrant" in roles or "registrant contact" in roles:
                vcard = entity.get("vcardArray") or []
                if len(vcard) > 1 and isinstance(vcard[1], list):
                    for prop in vcard[1]:
                        if isinstance(prop, list) and len(prop) > 3:
                            if prop[0] == "fn" or prop[0] == "org":
                                registrant_org = str(prop[3])
                # Check entity handle or public id
                public_ids = entity.get("publicIds") or []
                for pid in public_ids:
                    if str(pid.get("identifier") or "") == clean_org:
                        is_exact_owner = True

        raw_text = json.dumps(data)
        if clean_org in raw_text:
            is_exact_owner = True

        def norm_name(value: str) -> str:
            return "".join(re.findall(r"[a-z0-9]+", value.casefold()))

        registrant_name_matches = bool(
            registrant_org and company_name
            and norm_name(registrant_org) == norm_name(company_name)
        )
        ownership_state = (
            "org_number_match" if is_exact_owner
            else "registrant_name_match" if registrant_name_matches
            else "registrant_unverified"
        )

        h_digest = hashlib.sha256(raw).hexdigest()
        obs_id = f"norid-{clean_org}-{h_digest[:16]}"

        strategy = "norid_rdap_verification" if is_exact_owner else "norid_candidate_discovery"
        signal_type = "legal_domain_ownership" if is_exact_owner else "candidate_domain_discovery"

        return {
            "id": obs_id,
            "organisation_number": clean_org,
            "platform": "norid",
            "signal_type": signal_type,
            "source_url": rdap_url,
            "retrieved_at": retrieved_at,
            "published_at": retrieved_at[:10],
            "content_sha256": h_digest,
            "exact_entity": is_exact_owner,
            "identity_proof": [
                {
                    "type": "official_domain_registry_rdap",
                    "registry": "Norid (.no national registry)",
                    "domain": clean_domain,
                    "organisation_number": clean_org,
                    "verified_ownership": is_exact_owner,
                    "ownership_state": ownership_state,
                }
            ],
            "acquisition_mode": "official_api",
            "rights_status": "approved",
            "source_class": "national_domain_registry",
            "evidence_span": f"Norid .no registry verification for {clean_domain}: " + (
                f"registrant organisation number matches {clean_org}"
                if is_exact_owner else (
                    f"registrant display name matches {company_name}; organisation number was not exposed"
                    if registrant_name_matches else "registrant record checked; no exact organisation-number match exposed"
                )
            ),
            "metrics": {
                "domain": clean_domain,
                "verified_ownership": is_exact_owner,
                "ownership_state": ownership_state,
                "registrant_name_matches_legal_name": registrant_name_matches,
                "registrant_name": registrant_org or company_name,
                "registry": "Norid",
            },
            "strategy": strategy,
        }

    except Exception:
        elapsed_ms = int((time.perf_counter() - start_time) * 1000)
        if budget:
            budget.record_request(cost_usd=0.0, bytes_received=bytes_received, elapsed_ms=elapsed_ms)
        return None


NORWEGIAN_FIRST_NAMES = {
    "oyvind", "nils", "ole", "per", "jan", "lars", "arne", "svein", "geir", "bjorn",
    "kari", "anne", "astrid", "hilde", "knut", "tor", "erik", "hans", "morten",
    "espen", "anders", "kjetil", "runar", "frode", "terje", "stian", "einar",
    "harald", "magnus", "olav", "johan", "martin", "thomas", "christian", "henrik",
}


def discover_norid_domain(
    company_name: str,
    org_number: str,
    email: str = "",
    legal_form: str = "",
    *,
    budget: Any | None = None,
    timeout: float = 3.0,
) -> tuple[str | None, dict[str, Any] | None]:
    """Discover and verify a registered .no domain for a Norwegian company via Norid RDAP.

    Generates candidate brand slugs from company name and registered corporate email,
    probes the official Norid RDAP service, and returns (domain, observation) upon verification.
    Applies strict identity safeguards to prevent wrong-company matching on personal names or co-ops.
    """
    if not company_name or not org_number:
        return None, None

    # Housing co-operatives and boligsameier do not own corporate commercial websites
    if legal_form in ("ESEK", "BRL") or any(
        company_name.lower().startswith(p) for p in ["boligsameiet", "borettslaget", "sameiet"]
    ):
        return None, None

    clean_words_variants: list[list[str]] = []
    # Generate words with both å -> aa and å -> a transliterations
    for a_rep in ("aa", "a"):
        clean = company_name.lower().translate(str.maketrans({"æ": "ae", "ø": "o", "å": a_rep}))
        words = re.findall(r"[a-z0-9]+", clean)
        legal_stopwords = {
            "as", "asa", "ba", "da", "enk", "iks", "nuf", "sa", "sam",
            "sti", "holding", "eiendom", "invest", "drift", "sameie", "boligsameiet",
        }
        while words and words[-1] in legal_stopwords:
            words.pop()
        while words and words[0] in {"boligsameiet", "sameiet", "borettslaget"}:
            words.pop(0)
        if words and words not in clean_words_variants:
            clean_words_variants.append(words)

    all_company_tokens = {w for w_list in clean_words_variants for w in w_list if len(w) >= 3}

    primary: list[str] = []
    secondary: list[str] = []

    GENERIC_MAIL_HOSTS = {
        "gmail.com", "hotmail.com", "outlook.com", "yahoo.com", "icloud.com",
        "me.com", "mac.com", "live.no", "live.com", "online.no", "c2i.net",
        "frisurf.no", "broadpark.no", "start.no", "spray.no", "getmail.no",
        "ntebb.no", "lyse.net", "enitel.no", "styrerommet.net", "vibbo.no", "bbl.no",
        "mail.com", "zoho.com", "proton.me", "protonmail.com", "fastmail.com",
    }

    # 1. Email-based candidates: if email has a custom domain, check it with Norid RDAP
    if email and "@" in email:
        user, host = email.split("@", 1)
        user = user.lower().strip()
        host = host.lower().strip().translate(str.maketrans({"æ": "ae", "ø": "o", "å": "a"}))
        host_base = host.split(".")[0]
        is_custom_domain = host not in GENERIC_MAIL_HOSTS and len(host_base) >= 3
        host_matches_company = any(t in host_base or host_base in t for t in all_company_tokens)
        user_matches_company = any(t in user or user in t for t in all_company_tokens)

        if is_custom_domain:
            if host.endswith(".no"):
                primary.append(host)
            else:
                primary.append(f"{host_base}.no")
        if user_matches_company and len(user) >= 3 and not any(bad in user for bad in ["post", "kontakt", "info", "mail", "support", "admin", "faktura", "regnskap", "office"]):
            primary.append(f"{user}.no")

    # 2. Slugs from company name
    industry_words = {"industri", "teknologi", "handel", "bygg", "service", "gruppen", "norge", "nordic", "invest"}
    for words in clean_words_variants:
        full_slug = "".join(words)
        hyphen_slug = "-".join(words)
        primary.append(f"{full_slug}.no")
        if len(words) > 1:
            primary.append(f"{hyphen_slug}.no")
            # If 3 or more words, also try combinations without generic industry words (e.g. GOT VINJE INDUSTRI -> gotvinje)
            core_words = [w for w in words if w not in industry_words]
            if len(core_words) >= 1 and core_words != words:
                primary.append(f"{''.join(core_words)}.no")
                if len(core_words) > 1:
                    primary.append(f"{'-'.join(core_words)}.no")
            # First distinctive word if >= 4 characters and NOT a common personal first name
            if len(words[0]) >= 4 and words[0] not in NORWEGIAN_FIRST_NAMES:
                primary.append(f"{words[0]}.no")
            # Last distinctive word if >= 4 characters and not generic
            if len(words[-1]) >= 4 and words[-1] not in NORWEGIAN_FIRST_NAMES and words[-1] not in industry_words:
                secondary.append(f"{words[-1]}.no")

    cands = primary + secondary
    seen: set[str] = set()
    dedup: list[str] = [c for c in cands if not (c in seen or seen.add(c)) and len(c) >= 5]

    for cand_dom in dedup:
        if budget and not budget.can_proceed():
            break
        obs = verify_norid_domain_ownership(cand_dom, org_number, company_name, budget=budget, timeout=timeout)
        # Do not use a merely registered, unrelated .no domain as a website
        # candidate. A name match may be used for a later website identity gate;
        # an org-number match is direct legal ownership proof.
        if obs and (obs.get("metrics") or {}).get("ownership_state") in {"org_number_match", "registrant_name_match"}:
            return cand_dom, obs

    return None, None



