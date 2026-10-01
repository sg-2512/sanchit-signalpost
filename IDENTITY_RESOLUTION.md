# IDENTITY_RESOLUTION.md: Candidate & Publication Gates

This document defines the evidence graph, candidate generation methods, multi-attribute scoring, and publication gates required to prevent wrong-company errors.

## 1. The Evidence Graph

The Norway Company Agent does not rely on simple string or name similarity. It builds an explicit evidence graph:

$$\text{Legal Entity (Brreg)} \longrightarrow \text{Candidate URL} \longrightarrow \text{Extracted Footprint} \longrightarrow \text{Reverse Proof} \longrightarrow \text{Publication}$$

```
                ┌───────────────────────────────────┐
                │   Legal Entity (9-digit OrgNr)   │
                └─────────────────┬─────────────────┘
                                  │
                   Candidate Discovery via Tier 5
                                  │
                                  ▼
                ┌───────────────────────────────────┐
                │        Candidate URL / Source     │
                └─────────────────┬─────────────────┘
                                  │
                       Deterministic Crawl
                                  │
                                  ▼
                ┌───────────────────────────────────┐
                │ Extracted Site Footprint:         │
                │ - Org Number mentions             │
                │ - Registered Address / Postcode   │
                │ - Phone Numbers                   │
                │ - Board / Management Names        │
                └─────────────────┬─────────────────┘
                                  │
                     Reverse Proof Scoring Engine
                                  │
                                  ▼
                ┌───────────────────────────────────┐
                │ Score >= 70 & Zero Discrepancies? │
                └─────────┬───────────────┬─────────┘
                          │ YES           │ NO
                          ▼               ▼
                 [PUBLISH CLAIM]     [ABSTAIN: not_available]
```

## 2. Multi-Attribute Scoring Rubric

Implemented in [`src/norway_company_agent/identity.py`](file:///c:/Users/Sanchit%20Gupta/builderr/signalpost-starter-kit/src/norway_company_agent/identity.py):

| Attribute Verified | Evidence Strength | Point Weight | Notes |
| :--- | :--- | :---: | :--- |
| **Exact 9-digit OrgNr in Footer/Imprint** | Gold Standard | **+80** | Unambiguous legal reverse proof |
| **Exact Normalized Legal Name Match** | Strong | **+30** | Stripping legal forms (AS, ASA, ENK) |
| **Registered Address / Postcode Match** | Strong | **+25** | Matches registered Brreg municipality |
| **Registered Phone Number Match** | Strong | **+20** | Matches Brreg contact phone |
| **Board / Executive Name Match** | Strong | **+20** | Confirms verified person from roles API |
| **Domain Matches Official Registry** | Very Strong | **+40** | Matches domain declared in Brreg filing |

### Publication Gate Thresholds
- **Score $\ge 70$:** Qualified as **Publishable**. Claim availability is set to `"available"` with high confidence ($\ge 0.95$).
- **Score $40 - 69$:** Qualified as **Ambiguous**. Marked `"availability": "ambiguous"`. Not published as an official claim.
- **Score $< 40$:** Rejected. Marked `"availability": "not_available"`.

## 3. Disqualification Gates (Zero Tolerance)

A candidate is immediately disqualified (`publishable = False`) regardless of score if:
1. **Different Org Number Found:** If the page footer declares a different 9-digit Norwegian organisation number, it belongs to another legal entity (e.g. parent company, franchise partner, web agency).
2. **Sister / Franchise Conflict:** Sites that represent an entire multinational group or holding company without explicit Norwegian subsidiary attribution.
3. **Dead / Parked Domain:** Domain yields HTTP 4xx/5xx, parking page indicators, or domain sale advertisements.
