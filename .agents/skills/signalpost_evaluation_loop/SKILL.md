---
name: signalpost-evaluation-loop
description: Continuous testing, idempotent refresh replay, cryptographic freeze, and benchmark promotion harness for SignalPost.
---

# SignalPost Evaluation Loop & Promotion Runbook

This skill describes the standard 4-gate verification and promotion procedure for all model modifications, connector additions, and benchmark runs in the SignalPost agent repository.

## 1. The 4-Gate Continuous Verification Runner

Always execute the master verification runner to test all layers in one command:

```powershell
uv run python scripts/verify_all.py
```

This executes the following four gates in order:

1. **Step 1: Pytest Unit & Connector Test Suite (219 tests)**:
   - Validates all parsers, connectors, identity assessments, budget trackers, and scoring formulas.
   - Command: `python -m pytest tests/ -q`

2. **Step 2: Idempotent Refresh Replay (0 False Changes)**:
   - Replays historical snapshots to ensure exactly 2 true changes are identified and zero false changes are generated.
   - Command: `python first_run.py`

3. **Step 3: Cryptographic Strategy Freeze Verification**:
   - Computes SHA-256 digests over all frozen strategies and verifies against `manifest.json`.
   - Command: `python -m strategies.freeze --verify`

4. **Step 4: Golden Benchmark Promotion Evaluation (Fatal Gate & 6 Rubric Gates)**:
   - Evaluates the challenger profile and envelope output against `eval/gold_companies.jsonl`.
   - Command: `python -m eval.run --corpus eval/gold_companies.jsonl --profiles out/fresh-smoke-100/profiles.jsonl --envelopes out/fresh-smoke-100/envelopes.jsonl`

## 2. Promotion Decision Rules

Promote a strategy change only if all criteria are satisfied:
- **Zero Material Wrong-Company Publications** (Fatal gate).
- **Claim Precision**: Maintained at 100% or within 2% margin.
- **Evidence Validity**: 100% of accepted claims have valid SHA-256 evidence.
- **Recall & Coverage**: Improves company coverage or claim recall according to Builderr's 70% company + 30% claim rubric.
- **Budget Compliance**: ≤ 1,850 requests and ≤ $10.00 estimated third-party spend per 1,000 companies.
