# LIMITATIONS.md: Known Gaps, Licenses & Access Restrictions

This document specifies the lawful boundary, non-negotiable platform restrictions, licensing terms, and known data coverage limits of the Norway Company Agent.

## 1. Non-Negotiable Platform Restrictions

In strict adherence to the Signalpost Evaluation Playbook:
- **Prohibited Scraping:** LinkedIn, Meta, Glassdoor, Indeed, and similar authenticated walled gardens are **never scraped directly**. Unofficial scrapers or open-source headless bypass scripts violate terms of service, jeopardize reproducibility, and are prohibited as sole support for a published claim.
- **Permitted Alternatives:**
  - Active recruitment & job vacancies: Sourced via the official, open **NAV Arbeidsplassen API** (`arbeidsplassen.nav.no`) administered by the Norwegian Labour and Welfare Administration.
  - Media & Public Mentions: Sourced via open Google News RSS feeds.
  - Physical Footprint: Sourced via Google Places API and OpenStreetMap Nominatim.

## 2. Source Licensing & Provenance

| Source | License / Terms of Service | Permitted Use |
| :--- | :--- | :--- |
| **Brønnøysundregistrene (Enhetsregisteret)** | Norwegian License for Open Government Data (NLOD 2.0) | Full commercial and non-commercial reuse |
| **Regnskapsregisteret** | NLOD 2.0 / Public Registry | Free retrieval of statutory annual accounts |
| **NAV Arbeidsplassen** | NLOD 2.0 / Public API | Free public search of Norwegian job openings |
| **OpenStreetMap** | Open Data Commons Open Database License (ODbL) | Address and geographic attribution |
| **Google Places / News** | Public API / RSS Standard Terms | Non-cached transient display and sentiment calculation |

## 3. Known Coverage Gaps & Inherent Limitations

1. **Sole Proprietorships (ENK):** Under Norwegian accounting legislation (*Regnskapsloven* § 1-2), sole proprietorships and smaller general partnerships (ANS/DA) without corporate partners or asset thresholds are not legally obliged to file annual accounts with Regnskapsregisteret. The absence of financial accounts for an ENK is an accurate reflection of Norwegian corporate law, not a crawler failure.
2. **Specialized Financial Institutions:** Central banks, insurance mutuals, and foreign company branches (NUF) submit specialized statutory reports that may not appear in the standard normalized corporate accounts API.
3. **Domain Conflicts & Brand Aliases:** Highly diversified holding companies with generic Norwegian names (e.g. *Invest AS*, *Nordic Group AS*) frequently share names with unrelated local businesses. When reverse proof cannot establish 100% certainty, the agent intentionally abstains rather than risking a material wrong-company publication.
