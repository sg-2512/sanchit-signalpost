# SignalPost Submission & Engineering Invariants

This rule governs all code changes, benchmarks, documentation, and external submissions in the SignalPost repository.

## 1. Submission Cost Presentation Standard
- **PROHIBITION**: Never report API cost as **$0.00** or label commercial connectors as "free tier" in external submission documents, emails, evaluation scorecards, or README files.
- **REQUIREMENT**: Always report the full commercial third-party pay-as-you-go pricing breakdown with realistic margins ($3.10 estimated commercial cost per 100 companies):
  - Google Places API: ~$0.032 per company lookup
  - Brave Search API: ~$0.005 per query
  - YouTube Data API: ~$0.01 per search unit
  - OpenAI Synthesis Fallback: ~$0.002 per prompt

## 2. Cryptographic Evidence Grounding
- **100% SHA-256 Requirement**: Every published claim marked `available` in an envelope MUST resolve to an evidence record with a valid, non-empty `content_sha256`.
- **Zero Orphan Evidence**: Never output an envelope with unlinked evidence IDs or empty hash digests.
- **Verification**: `python scripts/verify_all.py` must verify 100% evidence grounding before any batch freeze.

## 3. Concurrency Pacing & Rate Limiting
- **Thread Locks**: When multithreading with concurrent workers, commercial API endpoints (Google Places, Brave Search) must use threading locks and spacing (minimum 150ms interval) to prevent transient HTTP 429 burst errors.
- **Backoff**: Any connector receiving HTTP 429 must immediately apply exponential backoff.

## 4. Request Ceiling Buffer Margin
- **Hard Limit**: 2,000 requests per 1,000-company run.
- **Execution Check Threshold**: When running with 8 parallel worker threads, the budget check threshold must be capped at `1,850` requests. This provides a 150-request safety buffer guaranteeing that in-flight thread requests never cross the 2,000 boundary.

## 5. Author & Branding Integrity
- **Single Contributor**: Git commit author must strictly be `Sanchit Gupta <sanchitgupta2512@gmail.com>`.
- **Agent Name**: The official agent name is strictly `Sanchit-Signalpost`.
- **Clean Naming**: Ensure zero occurrences of prohibited terms in code, commits, comments, or documentation.
