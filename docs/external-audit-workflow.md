# Independent external evidence audit

The agent writes `external-audit-labels.jsonl` as an unreviewed review queue and reports audit status as `not_requested` unless completed reviewer labels are explicitly supplied. For an existing run, prepare the fixed-size queue without repeating collection:

```powershell
uv run python scripts/prepare_external_audit.py `
  --profiles out/<run>/profiles.jsonl `
  --observations out/<run>/all-observations.jsonl `
  --output out/<run>-audit/external-audit-labels.jsonl `
  --sample-size 100 --seed signalpost-external-audit-v1
```

This is local file processing and makes no network requests. It deterministically balances the sample across platform and signal type, spreading it across companies where possible. Reuse the same seed to reproduce it. Every verdict starts as `null`; queue rows are not audit results. Reviewers must check the source page and evidence for each published observation, then fill the verdict fields and reviewer identity.

## Fields reviewers must complete

- `exact_entity`: the source refers to the expected legal company, not a similarly named entity.
- `metric_correct`: the extracted value, event, date, or description matches the source.
- `evidence_supported`: the source actually supports the claim as written.
- `evidence_span_valid`: the stored evidence span is present in, or faithfully represents, the cited source content. A hash or valid JSON alone is insufficient.
- `connector_policy_passed`: the source and acquisition method are allowed under the project's applicable source policy. Review the actual source and access method, not only the agent's `rights_status_claimed` value.
- `sentiment_correct`: required when the observation has a sentiment label; otherwise it may remain `null`.
- `reviewer`: the human reviewer or independent review system identifier.
- `reviewed_at`: an ISO 8601 timestamp, for example `2026-10-27T10:30:00Z`.

Verdict fields must be literal JSON booleans (`true` or `false`). Rows with a missing verdict, reviewer, or valid timestamp remain incomplete and are excluded from audit counts. The minimum audit gate counts fully reviewed, publishable observations only.

## Run the evaluator

After independently completing at least 100 queue rows, run from the repository root. You may also pass the completed file directly to `run_agent.py --audit-labels <completed-labels.jsonl>` on a rerun of the same input.

```powershell
uv run python scripts/evaluate_external_footprint.py `
  --profiles out/<run>/profiles.jsonl `
  --observations out/<run>/all-observations.jsonl `
  --labels out/<run>/external-audit-labels.jsonl `
  --output out/<run>/external-report.json `
  --minimum-audit 100
```

The evaluator reports completed rows separately from incomplete queue rows. It uses reviewer verdicts for entity precision, metric precision, evidence support, evidence-span validity, and connector policy. Do not manually edit the generated report to force a gate to pass.

## Feed the result into the proxy scorer

Run `scripts/score_competition_v3.py` with the resulting `external-report.json` and the same run's profiles, batch, research, and UX reports. Include `--refresh-report` when a refresh evaluation exists; otherwise the refresh gate stays unproven. For example:

```powershell
uv run python scripts/score_competition_v3.py `
  --profiles out/<run>/profiles.jsonl `
  --external-report out/<run>-audit/external-report.json `
  --batch-report out/<run>/batch-report.json `
  --research-report out/<run>/research-report.json `
  --ux-report out/<run>/ux-report.json `
  --output out/<run>-audit/score-report.json
```

Its external points remain gated by evaluator qualification; the overall result remains an internal proxy and cannot certify the organizer's hidden evaluation.

## Audit quality

Use a fixed sample selected before reviewing results. Include observations across companies, platforms, and signal types, with ambiguous company matches and likely false positives represented. Keep the selected sample and completed reviewer labels with the run artifacts. For uncertain cases, resolve them through a second reviewer and record the final adjudicated verdict in the row; do not silently convert uncertainty into `true`.
