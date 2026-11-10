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

The official score uses 50 points for recall and coverage, 30 for precision and evidence, 12 for synthesis, and 8 for UX. Qualification requires at least 65 on Builderr's own verified run. A material wrong-company publication, fabricated financial value, missing result, timeout, unsafe source handling, or unreproducible evaluator command can invalidate a run.

## Reproducible commands

Install and verify from a clean checkout:

```bash
uv sync --locked --group dev
npm run check:signalpost
npm run check:signalpost-showcase
npm run check:signalpost-challenge
```

Run the required local smoke test:

```bash
uv run python run_agent.py \
  --organisations dev-100-companies.jsonl \
  --bulk brreg-enheter.csv \
  --output-dir out/submission-100 \
  --expected-count 100 \
  --workers 8 \
  --source-policy official_safe

uv run python scripts/validate_submission_ready.py \
  --input dev-100-companies.jsonl \
  --output-dir out/submission-100 \
  --output out/submission-100/submission-readiness.json
```

The smoke report proves only local output-contract completeness and traceability. Builderr independently determines score and qualification.

## Independent local audit

`external-audit-labels.jsonl` is an **unreviewed queue**, not an audit result. It must remain blank unless an independent reviewer checks the actual sources. Never use `scripts/label_audit_rows.py` to claim independent precision: it only mirrors the agent's own assertions.

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
