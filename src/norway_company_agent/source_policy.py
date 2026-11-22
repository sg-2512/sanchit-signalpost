"""Source-rights policy used at the publication boundary.

The policy deliberately does not treat an observation's ``rights_status`` field
as proof.  A connector must be declared here before it can publish in an
official run.  This keeps experimental discovery code from silently becoming
scored output.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any

from .verified_items import has_current_registered_nav_evidence, is_recruitment_page_url, is_website_policy_page


OFFICIAL_SAFE = "official_safe"
EXPERIMENTAL = "experimental"

# These platforms require a declared API, licence, or written permission for
# the submitted collection pattern.  They are not publishable by default.
RESTRICTED_PLATFORMS = frozenset({
    "linkedin", "proff", "1881", "facebook", "glassdoor", "indeed",
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


def audit_snapshot_time(observations_path: Path) -> datetime | None:
    """Use the original completion timestamp for an offline audit of a saved batch."""
    report = observations_path.with_name("batch-report.json")
    if not report.exists():
        return None
    completed_at = json.loads(report.read_text(encoding="utf-8")).get("completed_at")
    if not completed_at:
        return None
    timestamp = datetime.fromisoformat(completed_at)
    return timestamp.astimezone(timezone.utc) if timestamp.tzinfo else None


def observation_decision(
    observation: dict[str, Any], *, policy: str = OFFICIAL_SAFE, snapshot_time: datetime | None = None,
) -> PublicationDecision:
    """Return whether an observation may be emitted as scored evidence.

    Experimental mode is intentionally explicit; it does not certify rights.
    It exists only for private measurement and must not be presented as an
    official competition result.
    """
    validate_policy_name(policy)
    strategy = str(observation.get("strategy") or "").casefold()
    if (
        policy == OFFICIAL_SAFE
        and strategy == "nav_official_vacancy_feed"
        and not has_current_registered_nav_evidence(observation, snapshot_time)
    ):
        return PublicationDecision(False, "nav_current_registered_check_required")
    if observation.get("signal_type") == "dated_news":
        metrics = observation.get("metrics") or {}
        title = str(metrics.get("title") or observation.get("title") or observation.get("text") or "")
        source_url = str(observation.get("source_url") or "")
        article_url = str(observation.get("article_url") or metrics.get("url") or "")
        if is_recruitment_page_url(source_url) or is_recruitment_page_url(article_url):
            return PublicationDecision(False, "recruitment_page_not_news")
        if is_website_policy_page(source_url, title) or is_website_policy_page(article_url):
            return PublicationDecision(False, "website_policy_page_not_news")
    if policy == EXPERIMENTAL:
        return PublicationDecision(True, "experimental_mode")

    platform = str(observation.get("platform") or "").casefold()

    if platform in RESTRICTED_PLATFORMS:
        return PublicationDecision(False, f"restricted_platform:{platform}")
    declared = source_registry().get(strategy)
    if declared and declared.get("publication") == OFFICIAL_SAFE:
        return PublicationDecision(True, "source_rights_registry_official_safe")
    if declared and declared.get("publication") in {"experimental", "conditional", "discovery_only"}:
        return PublicationDecision(False, f"registry_{declared['publication']}:{strategy}")
    if strategy.startswith(CONDITIONAL_STRATEGY_PREFIXES):
        return PublicationDecision(False, f"rights_not_declared:{strategy}")
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
