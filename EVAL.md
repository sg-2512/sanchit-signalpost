# EVAL.md: Corpus Splits, Metrics & Qualification Thresholds

This document details the evaluation protocol, corpus partitioning, metrics calculation, and qualification gates under the Signalpost Evaluation Contract (v2).

## 1. Corpus Hierarchy

- **Public Universe:** All 411,160 active Norway-registered entities with observed annual accounts (`b82d6a3e7231...`).
- **Smoke-Test Batch:** 100 randomly sampled representative companies across industries and legal forms (`out/fresh-100/`).
- **Official Evaluation Batch:** 1,000 companies supplied dynamically by Builderr at run time (`tests/fixtures/batch-orgs-1.txt` or evaluator-provided file).
- **Extension / Zero-Overlap Set:** Blind held-out validation batch used to guard against overfitting.

## 2. Scoring Formula (100 Points Total)

$$\text{Total Score} = \text{Recall & Coverage (50)} + \text{Precision & Identity (30)} + \text{Synthesis (12)} + \text{UX & Interaction (8)}$$

### A. Recall & Coverage (50 Points)
For each external field family:
$$\text{Coverage} = 0.70 \times \text{Company Recall} + 0.30 \times \text{Claim Recall}$$
Scored against the cumulative, independently verified reference pool of discoveries across all participating crawlers and Builderr's reference crawler.

### B. Precision, Identity & Evidence (30 Points)
- **Exact-Company Precision:** Penalizes wrong-company publications. A material wrong-company publication disqualifies the run from becoming official.
- **Evidence-Span Validity:** Checks whether claims link to cryptographically valid evidence hashes, authentic timestamps, and verifiable source URLs.
- **No Hallucinations:** Immediate disqualification for fabricated financial values or hallucinated board members.

### C. Decision-Useful Synthesis (12 Points)
- Assesses depth of executive briefings: revenue, operating margins, capital structure, governance, hiring status, and explicit callouts of missing/unknown items.

### D. UX & Interaction (8 Points)
- Fast local inspection, clean interactive presentation ([`showcase.html`](file:///c:/Users/Sanchit%20Gupta/builderr/signalpost-starter-kit/out/fresh-100/showcase.html)), searchability, and clear audit metrics.

## 3. Qualification Gates

An entry qualifies when:
1. It achieves an official run score $\ge 65/100$.
2. It outputs exactly one terminal envelope per input company ($100/100$ or $1,000/1,000$).
3. Zero silent drops and zero unhandled fatal exceptions.
4. Operates strictly within resource budgets:
   - $\le 2,000$ HTTP requests
   - $\le \$10.00$ external cost
   - $\le 45$ minutes total elapsed time

## 4. Local Evaluation Commands

```bash
# Run unit & integration tests
uv run python -m unittest discover tests

# Run 100-company evaluation and generate artifacts
uv run python first_run.py

# Validate output envelopes contract
uv run python -c "import sys, json; sys.path.insert(0, 'src'); from norway_company_agent.batch import validate_envelopes; data = [json.loads(l) for l in open('out/fresh-100/envelopes.jsonl', encoding='utf-8')]; print(validate_envelopes(data, 100))"
```
