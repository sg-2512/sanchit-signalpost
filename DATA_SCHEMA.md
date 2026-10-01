# DATA_SCHEMA.md: Terminal Envelopes, Claims & Evidence

This document defines the output schema, terminal envelopes, claim structures, and evidence references required by the Signalpost Evaluation Contract (v2) and [`OUTPUT_CONTRACT.md`](file:///c:/Users/Sanchit%20Gupta/builderr/signalpost-starter-kit/OUTPUT_CONTRACT.md).

## 1. Terminal Envelope Specification

Every processed organisation number produces exactly one terminal envelope. An evaluator run on $N$ companies must yield exactly $N$ envelopes with zero silent drops.

```json
{
  "run_id": "fresh-100",
  "organisation_number": "930263673",
  "state": "complete",
  "started_at": "2026-10-01T15:20:00.000000Z",
  "completed_at": "2026-10-01T15:20:14.512390Z",
  "run": {
    "run_id": "fresh-100",
    "started_at": "2026-10-01T15:20:00.000000Z",
    "completed_at": "2026-10-01T15:20:14.512390Z",
    "terminal_status": "completed"
  },
  "legal_identity": {
    "organisation_number": "930263673",
    "name": "VEAMYR ENTREPRENØR AS",
    "legal_form": "AS",
    "municipality": "KRISTIANSAND"
  },
  "claims": [ ... ],
  "evidence": [ ... ],
  "operations": {
    "requests": 2,
    "runtime_ms": 145,
    "third_party_cost_usd": 0.0
  },
  "changes": [],
  "errors": [],
  "modules": { ... },
  "profile": { ... }
}
```

## 2. Strict Availability States

The evaluation contract restricts all module and claim states to exactly 6 valid enum values:

| State | Definition | Example Scenario |
| :--- | :--- | :--- |
| `available` | Claim is fully supported by verified evidence. | Annual accounts filed in Regnskapsregisteret. |
| `not_available` | Source was queried or evaluated, but no record exists. | Sole proprietorship without filed accounts; no website found. |
| `blocked` | Source collection was blocked by robots.txt or site protection. | Target website disallows automated user-agents. |
| `not_applicable` | Field is legally or structurally irrelevant for this legal form. | Group structure query on a sole proprietorship (ENK). |
| `ambiguous` | Evidence exists but cannot be tied unambiguously to this entity. | Multiple companies share a similar name in search results. |
| `failed` | Infrastructure, network, or server-side exception occurred. | External API returned HTTP 500 or connection timed out. |

## 3. Claim Schema

Each claim is an atomic, independently testable proposition:
```json
{
  "field": "annual_accounts",
  "value": {
    "year": 2024,
    "revenue_nok": 4086758,
    "operating_profit_nok": 444391,
    "net_profit_nok": 331776,
    "total_assets_nok": 2187652
  },
  "availability": "available",
  "confidence": 1.0,
  "evidence_ids": [
    "ev-financials-930263673"
  ]
}
```

## 4. Evidence Schema

Evidence items represent immutable, verifiable source observations:
```json
{
  "evidence_id": "ev-financials-930263673",
  "source": "regnskapsregisteret",
  "retrieved_at": "2026-10-01T15:20:14.512390Z",
  "reporting_period": "2024",
  "status": "available",
  "content_sha256": "3a8f10b...",
  "data": { ... }
}
```
Each evidence record preserves:
- Canonical source name (`brreg_enhetsregisteret`, `regnskapsregisteret`, `nav_arbeidsplassen`, `website_snapshot`)
- Precise ISO 8601 retrieval timestamp
- Reporting period / effective year (e.g. `"2024"`)
- SHA-256 cryptographic digest of the raw response payload
