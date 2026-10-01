# REFRESH.md: Scheduling, Snapshots & Material Diffs

This document defines how the Norway Company Agent handles incremental updates, source volatility scheduling, snapshot immutability, and change classification.

## 1. Refresh Architecture

The agent supports continuous profiling over time without duplicating historical evidence:
- **Immutable Snapshots:** Every raw response is hashed and persisted with its retrieval timestamp.
- **Idempotent Upserts:** Re-running the agent on an unchanged company produces the identical terminal envelope without duplicate claims or false change signals.
- **Materiality Classification:** Changes between runs are isolated and classified by business materiality.

## 2. Volatility-Based Crawl Cadence

Not all corporate data changes at the same rate. The agent schedules re-crawls according to observed source volatility:

| Source Domain | Volatility | Recommended Refresh Interval | Rationale |
| :--- | :---: | :---: | :--- |
| **Annual Accounts (Regnskap)** | Low | Annual (post-July filing deadline) | Filed once per fiscal year; historical figures are immutable. |
| **Legal Entity & Form (Brreg)** | Low | Monthly / Event-driven | Changes only on mergers, capital changes, or address moves. |
| **Board & Executive Roles** | Medium | Monthly | Board resignations and appointments happen periodically. |
| **Job Vacancies (NAV / Careers)** | High | Weekly / Bi-weekly | Job postings open, expire, and rotate continuously. |
| **Company News & Media Buzz** | High | Daily / Weekly | Press releases and media mentions are time-sensitive. |

## 3. Change Classification Model

Implemented in [`src/norway_company_agent/refresh.py`](file:///c:/Users/Sanchit%20Gupta/builderr/signalpost-starter-kit/src/norway_company_agent/refresh.py):

When a new run compares against a previous snapshot, diffs are generated for each field:

```json
{
  "field": "hiring_status",
  "change_type": "material_change",
  "previous_value": { "active_job_postings_count": 0, "appears_to_be_hiring": false },
  "current_value": { "active_job_postings_count": 3, "appears_to_be_hiring": true },
  "first_observed_at": "2026-09-01T12:00:00Z",
  "last_observed_at": "2026-10-01T15:20:00Z",
  "supporting_evidence": [
    "ev-hiring-930263673-v1",
    "ev-hiring-930263673-v2"
  ]
}
```

### Change Types:
1. `unchanged`: Current value matches previous value exactly.
2. `added`: A previously `not_available` field was newly discovered.
3. `removed`: A previously active field is no longer present (e.g. all job openings closed).
4. `material_change`: High-impact change (e.g. CEO change, revenue swing >20%, bankruptcy notice).
5. `minor_change`: Routine variation (e.g. minor description tweak, phone number reformatting).

If an external refresh fails due to a network glitch, the agent retains the last known supported value while reporting the transient refresh warning in `errors`.
