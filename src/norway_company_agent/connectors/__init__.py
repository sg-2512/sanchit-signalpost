"""External data connectors for the SignalPost live agent."""

from .brave_search import discover_company_website, is_available as brave_available
from .doffin import fetch_doffin_procurement
from .esg_certifications import fetch_esg_certifications
from .forskningsradet import fetch_forskningsradet_grants
from .gleif import fetch_gleif_identifier
from .google_news import fetch_google_news
from .google_places import fetch_place_data, is_available as places_available
from .innovasjon_norge import fetch_innovasjon_norge_grants
from .kartverket import geocode_address_kartverket
from .kunngjoringer import fetch_brreg_kunngjoringer
from .linkedin import discover_linkedin_company, is_available as linkedin_available
from .linkedin_jobs import fetch_linkedin_jobs
from .losoreregisteret import fetch_losoreregisteret_liens
from .lovdata import fetch_insolvency_legal_notices
from .mattilsynet import fetch_mattilsynet_inspections
from .miljodirektoratet import fetch_miljodirektoratet_permits
from .nav_jobs import fetch_nav_jobs
from .norid import verify_norid_domain_ownership
from .oslo_bors import fetch_oslo_bors_disclosures
from .patentstyret import fetch_patentstyret_data, is_available as patentstyret_available
from .proff_directory import resolve_commercial_directory_presence
from .regulatory_licenses import fetch_regulatory_licenses
from .ssb_benchmarks import fetch_ssb_industry_benchmark
from .subunits import fetch_company_subunits
from .wikidata import fetch_wikidata_entity
from .youtube import fetch_youtube_data, is_available as youtube_available

__all__ = [
    "discover_company_website",
    "brave_available",
    "fetch_doffin_procurement",
    "fetch_esg_certifications",
    "fetch_forskningsradet_grants",
    "fetch_gleif_identifier",
    "fetch_google_news",
    "fetch_place_data",
    "places_available",
    "fetch_innovasjon_norge_grants",
    "geocode_address_kartverket",
    "fetch_brreg_kunngjoringer",
    "discover_linkedin_company",
    "linkedin_available",
    "fetch_linkedin_jobs",
    "fetch_losoreregisteret_liens",
    "fetch_insolvency_legal_notices",
    "fetch_mattilsynet_inspections",
    "fetch_miljodirektoratet_permits",
    "fetch_nav_jobs",
    "verify_norid_domain_ownership",
    "fetch_oslo_bors_disclosures",
    "fetch_patentstyret_data",
    "patentstyret_available",
    "resolve_commercial_directory_presence",
    "fetch_regulatory_licenses",
    "fetch_ssb_industry_benchmark",
    "fetch_company_subunits",
    "fetch_wikidata_entity",
    "fetch_youtube_data",
    "youtube_available",
]
