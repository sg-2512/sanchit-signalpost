# CRAWLERS.md: Connectors, Budgets & Fallback Rules

This document specifies the crawler architecture, rate limiting, deterministic fetching pipeline, and fallback cascades used by the Norway Company Agent.

## 1. Crawler Architecture Overview

The system uses a multi-stage deterministic crawling pipeline managed by [`src/norway_company_agent/website.py`](file:///c:/Users/Sanchit%20Gupta/builderr/signalpost-starter-kit/src/norway_company_agent/website.py) and [`src/norway_company_agent/scrapy_crawler.py`](file:///c:/Users/Sanchit%20Gupta/builderr/signalpost-starter-kit/src/norway_company_agent/scrapy_crawler.py):

1. **Robots.txt & Etiquette Check:** Respects `robots.txt` disallows. Disallowed domains transition gracefully to `"availability": "blocked"` (`blocked_robots`).
2. **Deterministic Sitemap & Route Inspection:** Prioritizes high-signal paths:
   - `/om-oss`, `/about`, `/om`
   - `/kontakt`, `/contact`
   - `/karriere`, `/stillinger`, `/careers`, `/jobs`
   - `/ledelse`, `/leadership`, `/investor`
3. **Structured Metadata Extraction:** Uses `extruct` to parse schema.org JSON-LD, OpenGraph, and microdata.
4. **Main Text Extraction:** Uses `Trafilatura` for clean, boilerplate-free page text.
5. **Headless Browser Fallback:** If a deterministic crawl detects a single-page JavaScript shell (e.g. empty body with React/Vue root), Playwright rendering is available as a controlled fallback.

## 2. Connectors Summary

| Connector | Module Path | Source Class | Rate Limit / Throttling | Purpose |
| :--- | :--- | :--- | :--- | :--- |
| **Brreg Entity API** | `official.py` | Official Register | 50 req/sec (polite: 20/s) | Legal identity, status, form, address |
| **Regnskapsregisteret** | `official.py` | Official Register | 30 req/min | Annual accounts, balance sheet, P&L |
| **Brreg Roles API** | `official.py` | Official Register | 50 req/sec | Board members, general managers, auditors |
| **Brreg Subunits (Underenheter)** | `connectors/subunits.py` | Official Register | 50 req/sec | Operating locations, branch offices & headcount |
| **Brreg Kunngjøringer** | `connectors/kunngjoringer.py` | Official Register | 10 req/sec | Dated legal notices, capital & auditor changes |
| **Wikidata SPARQL (P2333)** | `connectors/wikidata.py` | Open CC0 Data | 5 req/sec | Official social handles, CEO, inception, QID |
| **Patentstyret API** | `connectors/patentstyret.py` | Official Open API | 10 req/sec | Norwegian trademarks and patent registrations |
| **NAV Arbeidsplassen** | `connectors/nav_jobs.py` | Official Govt API | 10 req/sec | Active hiring vacancies by orgnr |
| **Google Places API** | `connectors/google_places.py` | Permitted API | 10 req/sec | Physical store ratings, address, coords |
| **YouTube Data API v3** | `connectors/youtube.py` | Permitted API | 10 req/sec | Corporate channel verification and video metrics |
| **LinkedIn Guest Typeahead** | `connectors/linkedin.py` | Open Discovery API | 5 req/sec | Corporate LinkedIn profile verification |
| **Google News RSS** | `connectors/google_news.py` | Permitted RSS | 5 req/sec | Media mentions and news sentiment |
| **Brave Search API** | `connectors/brave_search.py` | Candidate Discovery | 5 req/sec | Website candidate discovery |

## 3. Resource Budgets & Governance

Per company batch execution, the agent strictly enforces the competition resource ceiling:
- **Maximum Request Budget:** 2,000 requests across entire batch (averaging 1.1 to 2.0 requests per company).
- **Cost Budget:** \$10.00 USD total cap (current agent operates at **\$0.00 third-party cost**).
- **Runtime Budget:** 45 minutes total (current 100-company run completes in **~14.5 seconds**; p50: 0.12s, p95: 0.28s).
- **Enforcement:** Managed by [`src/norway_company_agent/budget.py`](file:///c:/Users/Sanchit%20Gupta/builderr/signalpost-starter-kit/src/norway_company_agent/budget.py). If limits are approached, external discovery terminates cleanly, falling back to cached or official register records.

## 4. Fallback Cascade

```
[Target: Official Website]
        │
        ├── 1. Registry-declared homepage (data.brreg.no)
        │      └── Fetch -> Verify DNS/SSL -> Reverse Proof Match
        │
        ├── 2. Exact Domain Candidate Generation (Brave Search / Heuristic)
        │      └── Fetch -> Check Robots.txt -> Extract Footer OrgNr / Address
        │
        └── 3. No match or verification failed?
               └── Mark "availability": "not_available" (Zero wrong-company leaks)
```

## 5. Safe URL Handling & SSRF Defense

All outbound crawling executes through [`assert_public_url`](file:///c:/Users/Sanchit%20Gupta/builderr/signalpost-starter-kit/src/norway_company_agent/website.py#L41-L56) and `SafeRedirectHandler`:
- **Protocol Whitelist:** Enforces `http` or `https` schemes; disallows dangerous schemas (`file:`, `gopher:`, `ftp:`, `data:`).
- **IP Address Sanitization:** Resolves domain DNS and blocks private networks (`10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`), loopback (`127.0.0.1`, `::1`), link-local (`169.254.169.254` AWS/cloud metadata), and multicast ranges.
- **Redirect Re-Validation:** Every HTTP 3xx redirect target is checked before the redirect is performed.
- **Payload Caps:** Strict socket timeouts (5.0s–20.0s) and bounded read sizes prevent decompression bombs or resource exhaustion.

