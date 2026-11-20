# SignalPost revision status — 20 November 2026

## Submitted revision and this local revision

Builderr acknowledged receipt of commit `9678e0f4c2005112b0c9bf13ef89d3dd1e7149ca`. That acknowledgement did not include a revised score. This document describes later local changes; it does not claim that Builderr has tested them.

The reviewed 100-company input is `eval-fresh-100.jsonl`. The new `official_safe` run used that exact input, 8 workers, a 2,000-request and $10 API budget, and no local paid API key. Command:

```powershell
uv run python run_agent.py --input eval-fresh-100.jsonl --output-dir out/final-reviewed-batch-100-20261120 --run-id final-reviewed-batch-100-20261120 --expected-count 100 --workers 8 --source-policy official_safe
```

The run produced 100 valid terminal envelopes in 441.1 seconds, used 1,389 requests and $0 in metered API spend. The output format audit found 1,642 claims, 862 evidence items, zero broken evidence links, zero format errors and zero detected secret leaks.

| Published coverage on the same 100 companies | Earlier reviewed run | New run |
| --- | ---: | ---: |
| Verified websites | 18 | 19 |
| Companies with dated news | 3 | 3 |
| Companies with open job postings | 2 | 2 |
| Operating locations | 78 | 78 |
| Registered addresses, separate from operating locations | 22 | 22 |
| Registry business purpose exposed | 0 | 100 |
| Distinct historical legal names exposed | 0 | 20 |

The new website is SOGNEFJORD EVENT AS (`931642405`), with a newly fetched company page and an exact-entity gate. The earlier run used Google Places and spent $1.156; the new run did not load a Places key. The coverage figures are for the same input, but the discovery credentials differ, so the one-site increase is not a controlled connector-only effect. Google Places remains available when the evaluator supplies its own key.

## Accuracy changes

- Live Brønnøysund history and business purpose are published only with live registry evidence.
- Social URL aliases with and without `www` are deduplicated. Links from a verified company site are identified as brand channels, rather than claimed as exclusive legal-entity accounts.
- Registered addresses are distinct from operating locations. Missing source digests are not replaced by a hash of empty bytes; available claims without source evidence are withheld.
- Local score components that were not measured are `null`. There is no invented 100-point score or independent qualification claim.
- The regulatory connector no longer creates Finanstilsynet approvals from an industry code. An Arbeidstilsynet approval needs a fetched response containing the exact organisation number and an approved status; its SHA-256 is computed from that response.
- `scripts/audit_100_results.py` now reads the six terminal envelope states, counts published claims, and reports the actual request budget. It no longer says 0 of 100 valid or counts registered addresses as operating locations.
- A local `.env` is loaded only with `--load-env`. Default runs do not silently use the user's paid API key.
- `protego` is a direct locked dependency because the website crawler imports it in the default path; the first clean Linux CI run exposed its absence.

## Remaining limits

The supplied company-level workbook provides useful targeted review but does not label every published external observation. The new run wrote 362 unreviewed audit rows. Exact-company precision and Builderr qualification remain unmeasured until an independent reviewer labels a fixed sample of at least 100 observations and Builderr runs the official batch. No automatic labels are being used as proof.

GREGORIUS SELSKAPSMAT AS and BILRADIOSPESIALISTEN I BERGEN AS have externally identified candidate websites, but the 100-company run did not publish them. KAARE LUND AGENTURER AS remains unproven by the live website gate. TELIA NORGE AS still has missed newsroom items. News and job coverage did not improve in this revision. These are open coverage tasks, not claimed successes.

Verification completed locally after the last code change: `pytest` 354 passed with a writable pytest temp folder; source-rights registry, manifest integrity verification and Python compilation passed; `scripts/verify_field_formats.py` passed on the 100 new envelopes. A Builderr rerun and an independent observation review are still required before claiming competitive readiness.
