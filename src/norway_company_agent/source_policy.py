"""Source-rights policy used at the publication boundary.

The policy deliberately does not treat an observation's ``rights_status`` field
as proof.  A connector must be declared here before it can publish in an
official run.  This keeps experimental discovery code from silently becoming
scored output.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any


OFFICIAL_SAFE = "official_safe"
EXPERIMENTAL = "experimental"

# These routes have an identity anchor or originate from a verified company
# controlled page.  They can still be rejected by the claim-level validators.
OFFICIAL_SAFE_STRATEGIES = frozenset({
    "official_subunit_workforce",
    "brreg_kunngjoringer_announcements",
    "brreg_kunngjoringer_ledger",
    "nav_arbeidsplassen_search",
    "norid_rdap_verification",
    "wikidata_p2333",
    "company_site_activity",
    "company_site_news",
    "company_site_job",
    "official_registry",
    "official_annual_accounts",
    "kartverket_address_geocode",
    "independent_news_discovery",
})

# These platforms require a declared API, licence, or written permission for
# the submitted collection pattern.  They are not publishable by default.
RESTRICTED_PLATFORMS = frozenset({
    "linkedin", "proff", "1881", "facebook", "instagram", "glassdoor", "indeed",
})
CONDITIONAL_STRATEGY_PREFIXES = (
    "google_", "youtube_", "brave_", "press_wire_", "miljofyrtarn_",
    "doffin_", "patentstyret_", "mattilsynet_", "miljodirektoratet_",
    "forskningsradet_", "innovasjon_norge_", "oslo_bors_", "ssb_", "gleif_",
    "kartverket_", "regulatory_",
)


@dataclass(frozen=True)
class PublicationDecision:
    publishable: bool
    reason: str


def source_registry() -> dict[str, dict[str, Any]]:
    """Load the versioned, machine-readable connector declarations."""
    path = Path(__file__).resolve().parents[2] / "config" / "source-rights.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return {str(item["strategy"]): item for item in data.get("connectors", [])}
    except (OSError, ValueError, KeyError, TypeError):
        # Fail closed: a missing/corrupt rights registry cannot approve output.
        return {}


def validate_policy_name(value: str) -> str:
    if value not in {OFFICIAL_SAFE, EXPERIMENTAL}:
        raise ValueError(f"Unknown source policy: {value}")
    return value


def observation_decision(observation: dict[str, Any], *, policy: str = OFFICIAL_SAFE) -> PublicationDecision:
    """Return whether an observation may be emitted as scored evidence.

    Experimental mode is intentionally explicit; it does not certify rights.
    It exists only for private measurement and must not be presented as an
    official competition result.
    """
    validate_policy_name(policy)
    if policy == EXPERIMENTAL:
        return PublicationDecision(True, "experimental_mode")

    platform = str(observation.get("platform") or "").casefold()
    strategy = str(observation.get("strategy") or "").casefold()
    source_class = str(observation.get("source_class") or "").casefold()
    mode = str(observation.get("acquisition_mode") or "").casefold()

    if platform in RESTRICTED_PLATFORMS:
        return PublicationDecision(False, f"restricted_platform:{platform}")
    declared = source_registry().get(strategy)
    if declared and declared.get("publication") == OFFICIAL_SAFE:
        return PublicationDecision(True, "source_rights_registry_official_safe")
    if declared and declared.get("publication") in {"experimental", "conditional", "discovery_only"}:
        return PublicationDecision(False, f"registry_{declared['publication']}:{strategy}")
    if strategy in OFFICIAL_SAFE_STRATEGIES:
        return PublicationDecision(True, "declared_official_safe_strategy")
    if strategy.startswith(CONDITIONAL_STRATEGY_PREFIXES):
        return PublicationDecision(False, f"rights_not_declared:{strategy}")
    if source_class in {"company_site", "company_owned", "official", "official_api", "official_registry", "official_subunits"} and mode not in {"unofficial_scraper", "guest_api", "public_directory"}:
        return PublicationDecision(True, "verified_company_or_official_source")
    return PublicationDecision(False, "undeclared_source_route")


def filter_publishable_observations(
    observations: list[dict[str, Any]], *, policy: str = OFFICIAL_SAFE
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Split observations into publishable and quarantined evidence.

    Quarantined rows remain available to the local learning harness but are
    never emitted as profile claims in official-safe mode.
    """
    accepted: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    for item in observations:
        decision = observation_decision(item, policy=policy)
        if decision.publishable:
            accepted.append(item)
        else:
            quarantined = dict(item)
            quarantined["publication_status"] = "quarantined"
            quarantined["quarantine_reason"] = decision.reason
            rejected.append(quarantined)
    return accepted, rejected
