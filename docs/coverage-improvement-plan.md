# Signalpost improvement plan

**Research date:** 2026-10-26  
**Baseline:** `out/review-100-user-live-r3/`  
**Purpose:** prioritize improvements that raise Builderr's fresh-batch score while preserving identity precision, evidence quality, reproducibility, and the run budget. This plan adds **no API keys, paid search APIs, or credentialed services**; existing keyed providers may continue to work when already configured.

## Executive recommendation

Prioritize better use of official open data, company-controlled websites, and public feeds/pages that can be fetched directly without credentials. Do not introduce new keys or paid providers. Existing configured connectors may continue to run. The latest 100-company run already returns all 100 terminal envelopes and has strong official-data coverage, but official website coverage is 17%, dated news 16%, job postings 2%, and operating locations 67%. The report's exact-company precision and evidence-span validity are `null`, so its 100% field precision values are local checks, not verified contest precision. Do not loosen publication checks to chase coverage.

The run used 1,869 of the 2,000 request ceiling, $1.207 of the $10 cost ceiling, and 235.6 seconds. The principal constraint is therefore the request ceiling and its retry reserve, not spend or wall time. Keep a reserved request pool, cut low-yield or duplicate calls, and spend the reclaimed requests on carefully gated discovery.

## Current rules and deliverable format

Builderr currently lists 50 points for recall/coverage, 30 for precision/evidence, 12 for synthesis, and 8 for UX. The recall component weights company recall at 70% and fact recall at 30%. It expects the runner to read Builderr's fresh company input, handle batches larger than 100, and return exactly one terminal envelope per input. Availability states are `available`, `not_available`, `blocked`, `not_applicable`, `ambiguous`, and `failed`. Evidence needs source, retrieval date, and relevant reporting period; refresh must be idempotent. A material wrong-company publication blocks qualification. The page currently says fixed resource budget but does not display the numeric HTTP ceiling; the 2,000-request operating cap here is taken from the team's stated Builderr limit and local submission documentation.

The live challenge page links the full brief, company research guide, learning harness, permitted-source list, and evaluation contract. They are served as `text/markdown`, which the available web readers reject. The challenge page remains the authoritative source used here for current score, input/output, revision, and qualification rules; verify the exact linked contracts in a browser or downloaded artifact before the next submission. Do not infer missing numeric rules from stale local docs.

## Baseline and gaps

| Area | Latest run | Readout |
|---|---:|---|
| Envelopes | 100/100; all terminal; zero silent drops | Operational shape is good for the smoke size. |
| Requests | 1,869 / 2,000 | Only 131 requests remain; retries/fallbacks can exceed the cap. |
| Cost / elapsed | $1.207 / $10; 235.6 sec | Large headroom; optimize request allocation and completion reliability. |
| Official website | 17% company coverage; local precision 100% | Major opportunity. Improve registry-field use and site-linked discovery without credentialed search. |
| Dated news | 16% company coverage; local precision 100% | Major opportunity. Expand discovery only with article-level verification. |
| Job postings | 2% company coverage; local precision 100% | Largest field gap; investigate query, employer-ID joins, and active-ad parsing. |
| Operating locations | 67% company coverage | Use official Brønnøysund subunit endpoint where registry snapshot lacks full detail. |
| Precision audit | null | Must add an independently adjudicated exact-entity audit before claiming precision. |
| UX report | rendered 100 profiles; all criteria null | Desktop/mobile, compare, evidence, and search tasks are not measured. |

Note: the 17% website value comes from the run's detailed measurement; older conversation summaries cite 21% from another run. Always use the exact run ID and artifact when comparing.

## Prioritized work

### P0 — Freeze a trustworthy baseline and the challenge contract

1. Create a frozen, stratified validation set with unseen companies, name collisions, parent/subunit and franchise cases, absent/dead/redirecting sites, legal-form and geography slices, and known positive news/jobs.
2. Label positives, negatives, and unresolved cases independently. Report population-weighted and stress-case metrics separately; do not use the agent's own abstentions to define ground truth.
3. Record per-company requests, bytes, cost, latency, connector outcomes, published fields, and exact evidence references. Keep the 100-company live run as an operational baseline, not an official score.
4. Align `EVAL.md`, `docs/external-footprint-loop.md`, `docs/poc-design.md`, and `SUBMISSION.md` with the current challenge page. These currently contain older scoring structures, older batch assumptions, or historical metrics that conflict with current runs. Label historical results clearly and retain commit/run provenance.

**Gate:** repeatable clean install and one-command run; 100% envelope completion; zero wrong-company facts on adjudicated identity cases; evidence hash and URL agree for every published claim.

### P1 — Improve exact company website discovery without adding keys

1. Use the organization number as the anchor. First take the official registry website and official subunit/group data; normalize canonical domains and redirects.
2. Improve the default keyless pipeline with official registry website fields, official subunit/group records, links from a verified company-controlled website, public sitemap/robots declarations, and public cross-links embedded on that site.
3. For missing domains, use only unauthenticated public pages already within the permitted-source rules, such as a company-linked registry/public directory page where its use is allowed. Do not scrape search engine result pages or treat browser search snippets as retained evidence. If no permitted candidate source exists, preserve the honest missing/ambiguous state.
4. Fetch candidate sites independently and require exact organization number, unique legal-name plus address/phone corroboration, or a verified registry/site cross-link before publication. Keep parent, franchise, marketplace, social, and directory domains out of the official-website field.
5. Preserve redirects and hash the fetched page that supports the claim. If the displayed URL is an article or company page, its evidence hash must come from that page's response, not the homepage/news index.

**Gate:** 500-company frozen benchmark with at least 200 known positives and hard negatives; exact-domain precision >=99.5%, recall >=70% on eligible positives, zero material wrong-company publications. These thresholds already appear in `docs/external-connectors.md` and should be treated as the project's gate, not Builderr's published scoring formula.

### P2 — Increase dated news coverage using public company and publisher pages

1. Resolve the canonical official site first, then check its newsroom, RSS/Atom, JSON-LD, and sitemap. Report a newsroom listing as a listing item unless the article page itself is fetched and verified.
2. Do not add a news/search API. Use company newsroom feeds and pages, RSS/Atom, JSON-LD, sitemap links, and publisher links discovered from verified company pages. The existing free RSS and configured keyed search paths can remain available; search output remains discovery metadata only.
3. Fetch each publisher article; retain canonical article URL, publication timestamp, response hash, and a claim span showing the target entity. Distinguish article publication date from fetch date.
4. Reject registry notices, patents, procurement, generic company directory pages, undated mentions, and Google redirect URLs as news. Do not turn company marketing into independent sentiment.
5. Deduplicate canonical articles and avoid multiple calls for equivalent query variants. Maintain `not_available` only after a completed eligible search; mark blocked/failed/ambiguous distinctly.

**Gate:** on frozen known-positive news cases, measure company recall and item recall separately; zero unsupported or wrong-company articles; evidence URL/hash/date validates on every publication.

### P3 — Fix job-posting recall and employer joins using existing sources

1. Keep the existing public NAV job connector. NAV's published terms allow use/republishing subject to promptly removing inactive/deleted ads, reflecting updates, privacy obligations, and preserving direct application links.
2. Query/join on organization number or verified company identifier where available. Do not rely on name-only matching; resolve staffing agencies, subsidiaries, and parent groups explicitly.
3. Inspect why the current NAV connector found only two jobs: query construction, pagination, active/inactive filters, employer ID mapping, response parsing, and the distinction between listing pages and individual job ads.
4. Check the verified company's public careers page and publicly linked ATS feed as a second source. Do not add a new API key for ATS access. Publish only a specific active job advertisement with exact employer evidence, title, location, dates/status, and canonical application link.
5. Keep hiring status separate from vacancy count: an empty completed search is not a negative hiring signal when the feed is incomplete or blocked.

**Gate:** frozen benchmark with known live vacancies and hard parent/agency negatives; exact employer precision 100% on adjudicated set; inactive jobs removed/updated on refresh; every ad deep-links to its application destination.

### P4 — Reallocate requests and protect completion

1. Instrument request yield by connector: requests per newly verified company, newly verified fact, and new exact entity. Rank calls by marginal recall gain, not raw observation count. Include the cost of failed and blocked requests.
2. Introduce a 10–15% request reserve for retries and final envelope writing (200–300 requests at the 2,000 cap). Stop optional enrichment early when the reserve threshold is reached.
3. Deduplicate calls across modules; share verified domains, fetched pages, and response bytes within the run. Use conditional requests/cache only where source terms and freshness allow.
4. Allocate the remaining budget in order: mandatory official facts and envelopes; verified website pages; NAV and company careers pages; dated news pages/feed items; then lower-yield social or engagement sources. Existing keyed connectors can run when configured, but do not add providers or require new keys. Social connectors must stay disabled or experimental unless acquisition and evidence rights are clear.
5. Add a deterministic per-company fallback path so a connector timeout cannot prevent the terminal envelope. Preserve `blocked` vs `failed` vs `not_available` accurately.

**Gate:** 100/100 and 300/300 completion; <=1,800 planned requests on a 100-company run before reserve; <=2,000 actual including retries; cost <=$10; no silent drops.

### P5 — Improve synthesis and UX using verified evidence

1. Build the executive brief only from supported claims: business activity, latest filed financial period, governance, locations, current jobs/news, changes since last refresh, and explicit unknowns.
2. Link each sentence or displayed fact to the specific evidence item. Keep reporting period, publication date, and retrieval time visible and distinct.
3. Make the company-number search, compare flow, source verification, and missing/blocked states discoverable on desktop and mobile. Show confidence/ambiguity without implying certainty.
4. Complete the existing UX harness: find a company, compare two companies, verify a source, and repeat on a narrow mobile viewport. `ux-report.json` currently reports all criteria as null, so there is no baseline UX score.
5. Keep the showcase as a reviewer tool and demonstrate the exact artifacts Builderr requests: 100-company run report, repository URL, pinned commit, one command, models/APIs/licences, cost, and contact.

**Gate:** all five UX tasks pass on desktop and mobile; synthesis statements have claim-to-source links; no unsupported rollups.

## Request allocation proposal for the next 100-company experiment (no-key)

Treat this as an initial experiment, not a permanent quota. Measure actual calls and adjust using marginal verified yield.

| Pool | Requests | Use |
|---|---:|---|
| Mandatory registry, accounts, roles, locations, envelopes | 900 | Preserve current official-data baseline; deduplicate shared records. |
| Website discovery and verification | 450 | Registry-missing/dead only; official registry/subunit/group links and public cross-links from verified sites; existing configured search connector may add candidates. |
| News discovery and article verification | 300 | Verified-site newsroom/feed/sitemap and publicly linked publisher articles; retain the current RSS path. |
| Job search and active-ad verification | 200 | Existing public NAV connector plus verified public career/ATS pages. |
| Other eligible sources | 50 | Only approved sources with measurable marginal value. |
| Retry/failure reserve | 100 | 5% reserve; raise to 10–15% if observed retry rates require it. |
| **Total ceiling** | **2,000** | Enforce atomically across initial calls, retries, fallbacks, and redirects. |

If the required official-data path consumes more than the 900 planning pool, reduce optional sources before reducing reliability reserve. Do not parallelize past source limits or retry around explicit blocks.

## Experiment and revision sequence

1. **Baseline audit:** re-score the latest artifacts and validate current contract; inspect existing website discovery, why job yield is two, and why 50 observations are blocked. Produce connector-level request/yield accounting and the first UX evaluation.
2. **Website-only treatment:** improve credential-free registry and verified-site link discovery behind strict exact-domain verification. Compare against the same frozen set; audit every positive and hard negative.
3. **Jobs treatment:** fix NAV employer-ID mapping and query coverage; inspect public career feeds. Test against known active ads and parent/staffing negatives; compare job recall and errors.
4. **News treatment:** expand company newsroom/feed/sitemap and verified publisher-link discovery; verify each fetched article. Compare dated-news recall, item precision, and budget use.
5. **Integrated 100-company smoke:** combine only treatments that passed; measure request and cost distribution, end-to-end completion, refresh idempotency, desktop/mobile UX, and synthesis grounding.
6. **Scale check:** run 300+ unseen companies using the official-style input and output format. Ensure no dependence on local manifests or precomputed profiles, and retain enough time/request reserve.
7. **Freeze the best commit:** prepare submission metadata and clean-machine run. Builderr says revisions are frozen and apply only to later batches; submit only after gates pass, with exact commit and reproducible command.

## Project documentation fixes to schedule

- `EVAL.md`: remove claims that the official batch is fixed at 1,000 and stale threshold wording; current challenge page describes fresh shared batches, says the shared set may grow, and uses the current 50/30/12/8 scoring.
- `docs/external-footprint-loop.md` and `docs/poc-design.md`: label their 55/15/10/12/8 rubric as an internal historical proxy or replace it with the current public rubric. Do not present the internal rubric as Builderr's current score.
- `SUBMISSION.md`: prominently label all listed run counts, costs, scores, dates, connectors, and commit hashes as historical unless regenerated from the exact candidate commit. Replace its old run command/budget narrative before resubmission.
- `CRAWLERS.md`: reconcile stated latency/cost and connector availability with the current live report; explicitly flag experimental or policy-blocked sources.
- `docs/external-connectors.md`: preserve its strong exact-domain benchmark gate; clarify that existing keyed providers may remain configured, while no new key/provider is part of this work.

## Research sources

- [Builderr Signalpost challenge: current requirements, scoring, input/output, submissions, and revision rules](https://builderr.ai/challenges/signalpost)
- [Brønnøysundregistrene open-data API documentation](https://data.brreg.no/enhetsregisteret/api/dokumentasjon/en/index.html)
- [NAV job-ad API terms](https://arbeidsplassen.nav.no/vilkar-api)

## Limits of this review

The Builderr Markdown brief, playbook, learning harness, permitted-source list, and evaluation contract links are currently served with `text/markdown`; both available web readers reject that content type. This plan therefore relies on the current HTML challenge page, locally checked project contracts, the latest saved run reports, and primary Brønnøysundregistrene and NAV documentation. Re-open the five linked Builderr files in a browser or download their artifacts and reconcile any exact-format or numeric-budget details before a final submission.
