# SignalPost roadmap toward a 95+ official score

**Updated:** 2026-10-27  
**Official target:** 95/100 on Builderr's verified run; first gate is 65/100.  
**Constraint:** no new API keys or providers. Existing configured providers remain usable; improve the keyless paths and public-source verification.

## First, make the score measurable

The latest local `19.94/100` report is not an official score estimate. Its scorer uses an internal `55/15/10/12/8` breakdown, while Builderr currently publishes `50 recall / 30 precision / 12 synthesis / 8 UX`. It also gives no credit for unreviewed external claims and has null synthesis/UX evaluations. Do not use 19.94 to estimate distance from the official 65-point bar.

Builderr's current page says recall is weighted 70% by the number of companies covered and 30% by the number of checked facts found. Precision/evidence checks company match, source, and date; a material wrong-company match blocks qualification. Official batches are fresh and shared across entries; local 100-company runs are smoke tests, not competition scores. See [the current challenge and scoring rules](https://builderr.ai/challenges/signalpost).

Before optimizing, make the local evaluator mirror those four official categories and score only against a frozen, independently checked truth set. Keep separate fields for measured results, unreviewed estimates, and official results. Agent assertions such as `precision: 1.0` are not ground truth. The current 100-row audit queue is an internal diagnostic; its 100 rows still need actual review.

## Score target

These are stretch targets for planning, not promised outcomes or a claim about Builderr's exact hidden sub-metrics:

| Official category | Target | What must be true |
|---|---:|---|
| Recall and coverage | 47.5 / 50 | About 96% company coverage and 94% checked-fact recall would yield 47.7 under the published 70/30 blend, if the benchmark population supports those rates. |
| Precision and evidence | 29 / 30 | Near-zero wrong-company claims; claims have the right value, source URL, retrieval/publication date, and evidence that supports the claim. |
| Synthesis | 11 / 12 | Useful, concise profile explaining the business, changes, and unknowns, with every conclusion tied to evidence. |
| UX | 7.5 / 8 | Search, compare, and source verification are easy on desktop and mobile. |
| **Total** | **95 / 100** | Only Builderr's verified fresh run can confirm this score. |

The challenge's recent public board shows the leading directional result at 70.43, with 35.77/50 recall and 27.46/30 evidence. That board uses a 300-company round and labels the result provisional. A 95 target is therefore an exceptional stretch goal; qualify first, then pursue score gains without trading away precision.

## Baseline from the same 100-company run

Run `same100-after-20261026`: 100/100 envelopes, 1,821 requests, $1.173, 406 seconds. Local field coverage reported official websites at 17%, dated news at 18%, and job postings at 2%. These are useful diagnostics, not official recall: there is no checked denominator for all facts Builderr could find. The same run's reports also leave exact-company precision and evidence-span validity unmeasured. The audit setup has prepared 100 observations from 100 companies, but currently has **0 reviewed rows**.

Treat these as the priorities:

1. **Recall bottlenecks:** website, dated news, and jobs. Improve source discovery and per-field recall while maintaining exact-entity checks.
2. **Evidence:** verify each claim against the actual fetched page, retain canonical URL and publication/retrieval dates, hash the response that supports the displayed claim, and deduplicate repeated facts.
3. **Scoring/measurement:** replace the historical proxy weights and self-filled metrics with official-category reports and adjudicated labels.
4. **Synthesis and UX:** current reports have no evaluator measurements, so create explicit human or harness evidence for these categories instead of assuming they score well.
5. **Operational risk:** preserve one terminal envelope per supplied company, including unknown/blocked/ambiguous states, and protect retries/fallbacks within the 2,000-request ceiling used in local planning.

## Work plan and exit gates

### Phase 0 — Establish a trustworthy scorecard

- Align the local score report to `50/30/12/8`; remove the internal 55-point external bucket from any report presented as a Builderr estimate.
- Create a frozen, stratified evaluation set with independent labels for company identity, website, dated news, jobs, and source/evidence correctness. Include name collisions, parent/subunit relationships, dead/redirecting sites, and known positives and negatives.
- Run the unchanged agent on the same batch to establish per-field company recall, fact recall, exact-company error rate, evidence validity, request yield, cost, and completion.
- Complete the prepared 100-claim reviewer audit. Keep these results clearly marked as a local audit, not Builderr's hidden labels.

**Exit:** same inputs produce the same metrics; every reported rate names its denominator; no agent self-asserted values are treated as audited.

### Phase 1 — Win recall from public, exact-company sources

- **Official websites:** begin with BRREG website fields and official parent/subunit/group references. For missing or stale values, discover candidates only from permitted public sources and verified company-controlled links. Resolve redirects and require company-number evidence or multiple independent identity signals before publication. Keep parent, franchise, directory, marketplace, and social domains out of the official website field.
- **Dated news:** expand newsroom RSS/Atom, sitemap, JSON-LD, and links from verified company sites. Fetch the article itself before publishing it. Store the article URL, publication date, retrieval date, response hash, and a short supporting span. A newsroom index item must be labeled as such if the article page was not fetched.
- **Jobs:** investigate NAV query coverage, pagination, employer-ID joins, active/inactive filtering, and parsing. Add public career-page and linked ATS-feed discovery. Publish a specific active advert only when the employer, status/date, and application link are supported. Never infer hiring from an incomplete or blocked search.
- **Other sources:** use a provider only when its terms and evidence quality are clear. Existing keys can continue to activate their existing connectors; this plan adds no keys. Optimize measured verified-fact yield, not raw observation count.
- Run one-source-at-a-time treatments on the fixed batch, then test successful changes on a zero-overlap batch. Keep a change only if benchmark recall improves without a precision regression.

**Exit:** measurable company and fact recall gains by field; no material wrong-company publications; request use stays within the run budget with retry reserve.

### Phase 2 — Protect the 30 precision/evidence points

- Make identity resolution a publication gate, not a ranking boost. When evidence is ambiguous, abstain as `ambiguous` or `not_available`.
- Add claim-level provenance: target organization number, source identity, canonical URL, retrieval time, publication/reporting period, response hash, extracted claim, and evidence span.
- Hash the fetched document that backs each fact. Do not attach a listing-page hash to a claim presented as coming from an article.
- Deduplicate by canonical source and claim while retaining multiple independent sources when they add support.
- Refresh records idempotently: unchanged source snapshots create no duplicate records or false changes; changed values retain prior evidence and show the diff.
- Audit hard negatives and every observed wrong-company candidate. Treat one material wrong-company match as a release blocker.

**Exit:** complete source/date/evidence coverage for published claims; zero material wrong-company matches in the adjudicated test set; refresh replay has no duplicate or false-change findings.

### Phase 3 — Earn synthesis and UX points with visible evidence

- Generate a brief with four explicit parts: what the company does, what changed, what remains unknown, and the evidence behind each statement.
- Keep facts, source dates, reporting periods, and retrieval dates distinct. Do not convert stale or unavailable evidence into confident prose.
- Evaluate realistic tasks: find by organization number/name, compare two companies, open and verify a source, identify blocked/missing data, and repeat on mobile width.
- Fix the issues found in those tasks, then record screenshots or a structured review and tie each criterion to a test result. Do not set UX or synthesis to full points just because the page renders.

**Exit:** target 11/12 synthesis and 7.5/8 UX in an evidence-backed rubric review; all sources are reachable from the statements they support.

### Phase 4 — Submission readiness and scale

- Run the exact submission command from a clean clone at the pinned commit with an empty environment. Document install, models/APIs/licences, cost, contact, and one reproducible command.
- Test 100-company completion, then a larger unseen batch (300 or more) to expose input assumptions, throttling, memory growth, and long-tail failures. Builderr supplies the official batch; do not rely on the local manifest or cached profiles.
- Enforce one envelope per input and all terminal states (`available`, `not_available`, `blocked`, `not_applicable`, `ambiguous`, `failed`). A source failure must not drop a company result.
- Keep the request/cost/time limits enforced across retries, redirects, and fallbacks. The current 1,821-request run leaves only 179 requests under a 2,000 cap, less than the configured reserve of 200; reduce optional work or duplicate calls before adding discovery.
- Freeze each revision only after same-batch and zero-overlap results show a gain. The challenge says revisions apply to later runs; confirm current submission-window and revision availability before planning a final freeze.

**Exit:** clean-machine run succeeds; full completion; budget passes with reserve; zero material identity errors; pinned commit and command are reproducible.

## Score checkpoints

1. **Qualification checkpoint:** independently measured total at least 65/100 on a verified-style benchmark, with no material wrong-company match and no completion failure.
2. **Competitive checkpoint:** 80+ on the same fixed benchmark, backed by a zero-overlap validation run.
3. **Stretch checkpoint:** 95+ on both fixed and zero-overlap local sets, with recall and precision both high. Do not call it an official 95 until Builderr reports it on the official fresh batch.

At every checkpoint, record the score breakdown, exact code revision, input-set hash, labels version, request/cost/time totals, and failed or abstained fields. If score rises because the benchmark changed, the result is not a valid treatment comparison.

## Limits and risks

- The public challenge page states the overall score formula, 70/30 recall blend, qualification threshold, required output behavior, and wrong-company rule. The linked evaluation contract is served as Markdown that the current web reader does not render, so exact per-type quotas or point formulas beyond those public statements are not assumed here.
- A 100-company local set cannot predict the official hidden batch. The official benchmark can contain unseen and difficult companies.
- A 95 target cannot be guaranteed from code changes alone; it depends on the checked collection, eligible public evidence, and evaluator judgments.
- Current local score `19.94` is a separate internal proxy and must not be represented as Builderr's official score or as 19.94/65 progress.
