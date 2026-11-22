# Signalpost submission checklist

This document is the current submission checklist for this repository. It replaces historical proxy scores, archive run reports, and commit hashes; none of those establish Builderr qualification.

## What Builderr currently requires

Submit to `submit@builderr.ai`:

- the public repository URL and the exact frozen commit hash;
- one command that accepts the organisation-number JSONL supplied by Builderr;
- a 100-company local smoke-test report showing one terminal envelope per input;
- declared models, APIs, source rights, licences, and expected cost per official run;
- an agent name and contact for results.

Builderr supplies the fresh official company batch after the cutoff. It is currently 1,000 companies and may grow. Do not submit precomputed company profiles or a self-scored local proxy as an official result.

The official score uses 50 points for recall and coverage, 30 for precision and evidence, 12 for synthesis, and 8 for UX. Qualification requires at least 65/100 overall and 95% precision on Builderr's own verified run. A material wrong-company publication, fabricated financial value, missing result, timeout, unsafe source handling, or unreproducible evaluator command can invalidate a run.

## Reproducible commands

The [100-company smoke report](submission/smoke-report.json) records the live run on 22 November 2026 IST. It returned 100 valid envelopes, with evidence links for all 1,426 available claims. The run used 1,687 requests in 1,161.8 seconds and incurred $0 in API cost. The [smoke-test input](submission/smoke-input.jsonl) contains the organisation numbers used for this run.

The evaluator supplies `INPUT_JSONL`, `FROZEN_BRREG_CSV`, `REQUEST_LIMIT`, `WALL_SECONDS` and `API_BUDGET_USD`. Substitute their assigned values in this single command. The input may contain any batch size.

```bash
uv run --locked python run_agent.py --input INPUT_JSONL --bulk FROZEN_BRREG_CSV --output-dir out/official-run --source-policy official_safe --resume-mode refresh --max-requests REQUEST_LIMIT --max-time WALL_SECONDS --max-cost API_BUDGET_USD
```

For a refresh comparison, append `--previous-profiles PATH_TO_PRIOR_PROFILES_JSONL` and use a fresh output directory. The earlier artifacts provide the source snapshots for reviewing the changes in the new envelopes. The command above bypasses the bundled verified-site hint cache. If using an explicit `--verified-site-cache`, declare its provenance and supply it with the run. The agent revalidates every cached URL.

Install and verify from a clean checkout:

```bash
uv sync --locked --group dev
npm run check:signalpost
npm run check:signalpost-showcase
npm run check:signalpost-challenge
uv run --locked python -m strategies.freeze --verify --release
```

Run the required local smoke test:

```bash
uv run --locked python run_agent.py \
  --input submission/smoke-input.jsonl \
  --bulk brreg-enheter.csv \
  --output-dir out/submission-100 \
  --expected-count 100 \
  --workers 16 \
  --source-policy official_safe \
  --resume-mode refresh \
  --max-requests 2000 \
  --max-time 2700 \
  --max-cost 0

uv run --locked python scripts/validate_submission_ready.py \
  --input submission/smoke-input.jsonl \
  --output-dir out/submission-100 \
  --output out/submission-100/submission-readiness.json
```

The smoke report proves only local output-contract completeness and traceability. Builderr independently determines score and qualification.

The official command uses deterministic synthesis. The expected API spend without paid discovery is $0. Any optional credentials come from the evaluator's environment. Licensed Brave and Google Places can propose candidate websites within the supplied budget. Google Places response content stays transient, and only independently fetched, verified company pages can support a website claim. NAV vacancies need registered access. See the [source declaration](config/source-rights.json) and [execution notes](docs/submission-execution.md) for the licences, access requirements, and cache policy.

## Independent local audit

`external-audit-labels.jsonl` holds unreviewed claims. Leave verdicts blank until an independent reviewer checks the actual sources.

To measure local external precision, prepare a deterministic sample, have an independent reviewer complete every verdict, and run the evaluator with those completed labels:

```bash
uv run python scripts/prepare_external_audit.py \
  --profiles out/submission-100/profiles.jsonl \
  --observations out/submission-100/all-observations.jsonl \
  --output out/submission-100-audit/external-audit-labels.jsonl \
  --sample-size 100 \
  --seed signalpost-external-audit-v1

uv run python scripts/evaluate_external_footprint.py \
  --profiles out/submission-100/profiles.jsonl \
  --observations out/submission-100/all-observations.jsonl \
  --labels out/submission-100-audit/external-audit-labels.jsonl \
  --output out/submission-100-audit/external-report.json \
  --minimum-audit 100
```

See `docs/external-audit-workflow.md` for the required reviewer fields and the review procedure.

## Before sending

1. Confirm the workspace contains only intended changes.
2. Run the commands above from a clean clone at the commit being submitted.
3. Commit the smoke-test report or make it available in the public submission artifact.
4. Put the exact commit hash, current run command, report location, API and source disclosures, cost, name, and contact in the submission email.
