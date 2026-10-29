# Submission-ready procedure

This is the only evaluator run path. It defaults to `official_safe`: personal
`.env` credentials are not loaded, restricted/undeclared observations are
quarantined, and results never self-certify Builderr qualification.

## Freeze and validate

```bash
uv sync --locked
uv run --with pytest python -m pytest -q
uv run python scripts/check_source_registry.py
```

Review `config/source-rights.json`. Each route must specify access, publication
state, identity rule, and minimum evidence. A conditional or experimental route
cannot be claimed as official-safe until an independently reviewed promotion
report passes.

## Run the 100-company smoke test

```bash
uv run python run_agent.py \
  --organisations dev-100-companies.jsonl \
  --bulk brreg-enheter.csv \
  --output-dir out/official-safe-100 \
  --expected-count 100 \
  --workers 8 \
  --source-policy official_safe

uv run python scripts/validate_submission_ready.py \
  --input dev-100-companies.jsonl \
  --output-dir out/official-safe-100 \
  --output out/official-safe-100/submission-readiness.json
```

The readiness report must pass before using the run as a smoke-test artifact.
It proves only output-contract completeness and traceability; it does not
establish a Builderr score or qualification.

## Compare with the prior run on the identical input

```bash
uv run python scripts/compare_runs.py \
  --baseline out/<prior-run> \
  --candidate out/official-safe-100 \
  --output out/official-safe-100/comparison.json
```

Compare supported claims by field, evidence linkage, observation/quarantine
counts, requests, runtime, and cost. Do not compare runs with different
organisation numbers; the command rejects them.

## Promote a connector only after independent review

```bash
uv run python scripts/promote_connector.py \
  --strategy nav_arbeidsplassen_search \
  --labels out/reviewed-held-out.jsonl \
  --run-report out/official-safe-100/run-report.json \
  --output out/nav-promotion-report.json
```

The promotion report requires coverage gain, zero wrong-company claims,
complete evidence, in-budget operations, and an `official_safe` source-rights
declaration. A passing report permits a code/config promotion; it is not a
Builderr qualification result.
