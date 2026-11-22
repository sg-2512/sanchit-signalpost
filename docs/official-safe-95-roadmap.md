# Official-safe delivery plan and 95-point route

## Executive decision

Do not aim to return a fact from every connector for every company. Aim to
return one complete, honest envelope for every company and publish each fact
only when both gates pass:

1. **Evidence gate** — the source resolves to the exact Norwegian legal entity
   and preserves a source URL, retrieval time, hash, extraction method, and
   source span or structured-field selector.
2. **Rights gate** — the submitted collection method is permitted by the
   source's terms/licence or is an official/licensed API. A code library,
   public URL, search snippet, or a row that says `rights_status: approved` is
   not proof of permission.

This project now has an `official_safe` default policy in
`src/norway_company_agent/source_policy.py`. It quarantines rather than
publishes restricted or undeclared routes; `--source-policy experimental` is
only for local measurement and must never be presented as an official result.

## Current compliance findings

| Area | Current condition | Required correction |
|---|---|---|
| Batch output | One-envelope workflow exists, but internal failure names leaked into module state. | Normalise emitted states to Builderr's six states only. |
| Identity | Brreg-first identity, website reverse proof, accounts and roles are strong foundations. | Add a retained evidence selector/span for every externally extracted claim and test parent/franchise/brand negatives. |
| Source rights | LinkedIn guest endpoints and Proff HTML collection have no demonstrated submitted access permission. | Keep as private discovery experiments only; publish only after an approved API/licence/written permission and a policy-registry entry. |
| External news | Search/RSS can discover candidates but cannot be final evidence. | Retain the publisher page, canonical URL, date, response hash, exact-company span, and article-level entity proof. |
| Local scoring | Historic scripts contain proxy metrics and previously created self-approved audit labels. | Emit an unreviewed audit queue; never write local results as qualified, awardable, or official. |
| Secrets | Environment variables and URL sanitisation exist. | Remove `.env` loading from evaluator mode unless Builderr supplies the values; validate that no key is in arguments, URLs, output, or committed files. |
| Reproducibility | Lockfile exists, but this checkout contains local changes and generated reports. | Freeze a clean commit, document exactly one install command and one run command, then recreate the smoke test from a new environment. |

## Connector publication matrix

| Route | Default state | What is needed to publish |
|---|---|---|
| Brreg, Regnskapsregisteret, roles, subunits, Kunngjøringer, Løsøreregisteret | Publish | Official endpoint; organisation-number join; source URL, retrieval time, reporting period and source hash. |
| NAV Arbeidsplassen | Publish | Employer organisation number equal to the company or an official subunit; item-level job URL, posting date and hash. |
| Verified company website, sitemap, newsroom, careers pages | Publish | Robots-permitted access and reverse identity proof: organisation number, or legal name plus address/phone/official cross-link. Use an article/job leaf URL, not a listing page. |
| Norid and Wikidata | Conditional | Exact organisation-number/domain binding, source licence attribution, and retained returned record. |
| Google Places, YouTube, official specialist datasets | Conditional | Declare API plan, permitted storage/display, attribution, reproducible evaluator credential, rate/cost cap, and an exact-entity test. Search results remain candidates only. |
| Google News RSS and press wires | Discovery only until promoted | Fetch the final publisher or company page; preserve canonical URL, article date, hash and company span. Do not publish the RSS redirect or search result. |
| LinkedIn, Meta, Glassdoor, Indeed, Proff and other directory HTML | Quarantined | Written approval, official/licensed API, or another clearly permitted access method. Then add rights declaration, extraction test, exact-entity test and promotion report. |

## How to raise the score safely

### 1. Make the official base close to complete

For every input, produce legal identity, accounts/history, roles, registered
workplaces, source availability and refresh metadata from the official
endpoints. This is the highest-confidence coverage and should reach near-100%
terminal completeness. Missing accounts, roles, jobs, or websites must be
`not_available`, `blocked`, `ambiguous`, or `failed`; never `0`.

### 2. Increase public-identity coverage without name guessing

Use this fixed route order:

```text
Brreg-declared website / corporate email domain
  -> robots + static homepage + structured data
  -> footer organisation number OR legal-name + address/phone proof
  -> verified company aliases and outbound profiles
  -> sitemap: about, contact, leadership, locations, careers, newsroom
  -> JavaScript rendering only after a deterministic shell check
```

Reject a site when it is a parent, group, franchise, marketplace, portfolio,
or similarly named business. Save every rejected candidate and its reason so
coverage work can be measured without weakening publication precision.

### 3. Add high-value claims from company-owned pages

Once a website is verified, extract: business description, leadership pages,
careers, dated news, contact/location records, and official outbound social
links. Each claim needs an immutable snapshot and selector/span. A company
page can support company-reported facts; it cannot support independent
sentiment.

### 4. Make refresh real

Use stable keys per claim, for example:

```text
orgnr + field + canonical-source-url + item-id-or-hash
```

Store prior snapshots rather than overwriting them. On refresh, report
`new_job`, `closed_job`, `new_role`, `new_location`, `new_filing`,
`changed_description`, or `source_failed`; a failed refresh keeps the prior
supported value and records the failure.

### 5. Gate every improvement with held-out evidence

Maintain development, validation and frozen final corpora with no company or
host overlap. A connector can be promoted only when a human-reviewed report
shows:

- zero new material wrong-company publications;
- 100% required evidence fields for accepted claims;
- no meaningful precision regression;
- a declared coverage gain on the held-out set; and
- request, time, and cost budgets remain within the official limit.

## 95+ is an outcome, not a configuration

The current public scoring split is 50 recall/coverage, 30 precision/evidence,
12 synthesis and 8 UX. A 95+ score therefore requires both unusually broad
verified external coverage and near-perfect precision. It cannot be credibly
claimed from a local proxy. The practical target is to earn it through five
independent acceptance reports:

| Dimension | Required internal release threshold |
|---|---|
| Terminal completeness | 100% of supplied inputs emit exactly one valid envelope. |
| Exact-company safety | Zero material wrong-company publications on the frozen human-labelled corpus. |
| Evidence completeness | 100% of published claims have source identifier/URL, retrieval time, hash, effective/reporting date where relevant, and selector/span. |
| Recall | Each promoted route adds measured unique companies/facts on held-out data; no invented denominator or self-labelled recall. |
| Synthesis | Every summary sentence links to accepted evidence; it explicitly names unknowns and changes. |
| UX | A reviewer can search by organisation number, compare profiles, open sources, and inspect availability/refresh history on desktop and mobile. |

## Delivery sequence

1. **Policy boundary — implemented:** official-safe default, explicit experimental mode, quarantined observations, no self-certified qualification output, and output-state normalisation.
2. **Source registry:** create one machine-readable declaration per connector: owner, endpoint, access method, terms/licence URL, permitted storage/display, attribution, credential source, cost, rate limit, evidence requirements, and approval date.
3. **Snapshot store:** persist raw response metadata and immutable content blobs or legally allowed hashes/references; add selector/span for every emitted claim.
4. **Identity test suite:** add parent/subsidiary/franchise/name-collision fixtures for every external route; block promotion on any material false match.
5. **Claim validator:** reject claims without the required evidence fields before writing envelopes; unit-test state transitions and no-zero behaviour.
6. **Independent review workflow:** generate a queue with blank verdict fields; apply signed reviewer verdicts separately; score only reviewed labels.
7. **Clean smoke run:** clone the frozen commit to an empty directory, install from the lockfile, execute one command on 100 supplied organisation numbers, validate exactly 100 envelopes, and save the report plus command output.
8. **Submit:** repository URL, exact commit, one command, smoke report, source-rights declaration, APIs/models/licences, expected official-batch cost, and a named contact.

## Definition of done for a submission

- No restricted or undeclared connector can publish while `official_safe` is
  selected.
- Every output availability value is one of `available`, `not_available`,
  `blocked`, `not_applicable`, `ambiguous`, or `failed`.
- All 100 smoke-test inputs have exactly one envelope and every published claim
  is traceable to durable evidence.
- No local report claims Builderr qualification, an awardable score, or
  independent precision without independent labels.
- A clean evaluator can install and execute the declared command at the frozen
  commit using only the documented dependencies and supplied credentials.
