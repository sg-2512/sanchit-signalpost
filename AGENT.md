# AGENT.md: Research & Abstention Policy

This document defines the agent architecture, research strategy, source ladder, and strict abstention policies for the Norway Company Agent under the Signalpost Evaluation Contract (v2).

## 1. Architectural Philosophy

The agent is designed to balance high-recall discovery with zero wrong-company errors:
1. **Official Anchor First:** Every entity profile is anchored by its 9-digit Norwegian Organisation Number (`organisasjonsnummer`) from Brønnøysundregistrene.
2. **Reverse Proof Before Publication:** Any external discovery (website, job posting, place of business, press mention) must pass a deterministic reverse-identity verification before being promoted into a published claim.
3. **Explicit Abstention:** When information does not exist or cannot be verified to high confidence, the agent returns an explicit terminal availability state (`not_available`, `blocked`, `not_applicable`, `ambiguous`) rather than hallucinating or converting missing data to zero.

## 2. Source Ladder

All data collection follows an explicit 5-tier credibility ladder:

1. **Tier 1: Authoritative Government Registers & Accounts**
   - Brønnøysundregistrene Enhetsregisteret (Official legal entity register)
   - Regnskapsregisteret (Official annual accounts & financial statements)
   - Brønnøysund Kunngjøringer (Official legal notices & bankruptcy filings)
   - NAV Arbeidsplassen (Official Norwegian Labour and Welfare Administration job listings)
   - *Status:* Definitive proof. Overrides all external sources.

2. **Tier 2: Verified Company-Owned Web & Channels**
   - Company official websites with verified Org Number or address/phone confirmation
   - Official RSS / news feeds directly hosted on the company domain
   - *Status:* Authoritative for operational descriptions, self-reported contact details, and hiring pages.

3. **Tier 3: Licensed & Permitted Official APIs**
   - Google Places API / OpenStreetMap Nominatim for physical store locations and verified coordinates
   - Licensed scholarly / corporate registry APIs
   - *Status:* Authoritative for physical footprint; subject to reverse-address verification.

4. **Tier 4: Permitted Public Aggregators with Provenance**
   - Google News RSS (filtered by exact company legal name)
   - YouTube official channels (verified against official website domain links)
   - *Status:* Used for buzz, media sentiment, and external visibility.

5. **Tier 5: Search & Discovery Engines (Candidate Generation Only)**
   - Brave Search API / DuckDuckGo HTML
   - *Status:* **Candidate generation only.** Search ranking is NEVER used as evidence for a published fact. The underlying destination URL must be independently crawled and verified.

## 3. Layered Extraction Pipeline

```
[Raw Snapshot] 
      │
      ▼
[1. Structured Data (JSON-LD, OpenGraph, Microdata)]
      │
      ▼
[2. DOM & Meta Attributes (Contact, Footer, Impressum)]
      │
      ▼
[3. Clean Text Extraction (Trafilatura)]
      │
      ▼
[4. Deterministic Identity & Financial Rules]
      │
      ▼
[5. Executive Synthesis (Constrained Markdown Generation)]
```

## 4. Abstention & Zero-Hallucination Policy

- **No Numeric Hallucinations:** Financial figures (`revenue_nok`, `operating_profit_nok`, `net_profit_nok`, `total_assets_nok`) are exclusively extracted from Regnskapsregisteret normalized accounts or official filed PDFs.
- **No Silent Zeroes:** An unfiled annual report or missing employee count is never represented as `0`. It is represented as `null` with `"availability": "not_available"`.
- **Identity Abstention:** If a website candidate matches a similar name but belongs to a parent holding, sister company, or unrelated entity, it is marked `"availability": "ambiguous"` or `"not_available"`.
- **LLM Boundary:** Language models are strictly utilized for synthesizing human-readable briefings from verified structured facts. An LLM is never permitted to invent missing fields, create company affiliations, or override deterministic registry data.
