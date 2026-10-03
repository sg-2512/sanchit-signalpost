"""External data connectors for the SignalPost live agent."""

from .brave_search import discover_company_website, is_available as brave_available
from .doffin import fetch_doffin_awards, is_available as doffin_available
from .google_news import fetch_google_news
from .google_places import fetch_place_data, is_available as places_available
from .kunngjoringer import fetch_brreg_kunngjoringer
from .linkedin import discover_linkedin_company, is_available as linkedin_available
from .linkedin_jobs import fetch_linkedin_jobs
from .nav_jobs import fetch_nav_jobs
from .patentstyret import fetch_patentstyret_data, is_available as patentstyret_available
from .subunits import fetch_company_subunits
from .wikidata import fetch_wikidata_entity
from .youtube import fetch_youtube_data, is_available as youtube_available

__all__ = [
    "discover_company_website",
    "brave_available",
    "fetch_doffin_awards",
    "doffin_available",
    "fetch_google_news",
    "fetch_place_data",
    "places_available",
    "fetch_brreg_kunngjoringer",
    "discover_linkedin_company",
    "linkedin_available",
    "fetch_linkedin_jobs",
    "fetch_nav_jobs",
    "fetch_patentstyret_data",
    "patentstyret_available",
    "fetch_company_subunits",
    "fetch_wikidata_entity",
    "fetch_youtube_data",
    "youtube_available",
]
