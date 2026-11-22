# Signalpost reference agent

This is a runnable starting point for the Signalpost company-research challenge. It is intentionally a solid baseline, not a winning submission.

The public universe contains 411,160 eligible companies. Before submitting, run a 100-company local smoke test. Builderr supplies the fresh official batch for scoring; it is currently 1,000 companies and may grow.

## What it already does

- reads a batch of Norwegian organisation numbers;
- anchors identity in the Brønnøysund bulk registry;
- fetches official financials, roles, group links and registered workplaces;
- visits the registry-listed website and rejects weak entity matches;
- emits one terminal JSONL envelope per input;
- records sources, retrieval times, content hashes, request counts and latency;
- supports checkpoint/resume and a deterministic refresh replay;
- includes examples for external-footprint discovery and an evidence-bounded research agent.

## First run: try one saved example

Requires Python 3.12+. Open a terminal inside this extracted folder.

Before downloading company data or running a full crawl, try the bundled public
sample. It uses saved responses: no API key, registry download or live web requests.

On Windows:

```bash
py first_run.py
```

On macOS or Linux:

```bash
python3 first_run.py
```

If the launcher does not work, run the same check directly:

```bash
python3 scripts/run_refresh_replay.py --manifest tests/fixtures/refresh-snapshots.json --output out/refresh-test.json
```

Open `out/refresh-test.json`. The `events` list shows what changed between two
versions of one company profile and the source evidence for each change. The sample
should find two expected changes, no false changes, and no extra changes when the
same data is checked again.

The report's `qualification_passed` field refers only to this public sample check.
It does not qualify an entry for the competition or prove live information coverage.
The printed request counts are reads from saved responses, not network calls.

## Next: research live companies

Requires Python 3.12+ and `uv`. This step downloads data and makes live requests.
The manifest selector can create a local batch of any size. Use 100 companies for
the required smoke test; Builderr supplies the fresh official batch for scoring.

```bash
uv sync --locked
curl -L 'https://data.brreg.no/enhetsregisteret/api/enheter/lastned/csv' -o brreg-enheter.csv
curl -L 'https://builderr.ai/signalpost-company-universe-2025.jsonl.gz' -o signalpost-universe.jsonl.gz

uv run python select_entry_batch.py \
  --universe signalpost-universe.jsonl.gz \
  --count 100 \
  --output entry-companies.jsonl

# The selected 100 companies are a local smoke test only.

# Primary evaluation entrypoint (processes any JSONL batch of 10, 100, 300, or 1,000+ companies):
uv run python run_agent.py \
  --organisations dev-100-companies.jsonl \
  --bulk brreg-enheter.csv \
  --output-dir out/eval-run \
  --expected-count 100

# Batch contract runner:
uv run python scripts/run_competition_batch.py \
  --organisations entry-companies.jsonl \
  --bulk brreg-enheter.csv \
  --profiles-output out/profiles.jsonl \
  --output out/envelopes.jsonl \
  --report out/run-report.json \
  --run-id local-001 \
  --expected-count 100

uv run --group dev python -m pytest -q
```

The published archive was clean-room verified on August 24, 2026: 104 tests and 5 subtests passed, followed by a one-company live BRREG smoke run with one terminal envelope, five requests and zero silent drops. That historical verification does not establish current challenge qualification.

The local score reports are optimization proxies only. Their generated audit labels are not independent judgments, so proxy scores must never be described as official scores or qualification. Only Builderr's verified run can establish either.

For a live benchmark, pass `--resume-mode refresh`. Use `--resume-mode revalidate` with a previous output directory to reuse verified website hints and refetch evidence. The default `reuse` mode can rebuild a saved snapshot and reports how many profiles came from that snapshot.

Registered NAV consumers can supply `SIGNALPOST_NAV_FEED_TOKEN` and use `--nav-feed-cache` to retain a local cursor between runs. SignalPost checks each candidate vacancy live before publication.

Run `python -m strategies.freeze --verify` to compare the workspace source hashes with the manifest. Run `python -m strategies.freeze --verify --release` from a clean checkout to also verify that the manifest is committed at the current Git HEAD.

Increase `--count` and `--expected-count` together for larger local experiments. These local profiles are not submitted for scoring: Builderr supplies the official company batch at run time.

## The improvement loop

1. Treat the organisation number as the anchor.
2. Generate site/profile candidates from official data, the company site, lawful search providers and named people.
3. Save every candidate and the evidence for or against it.
4. Publish only exact-entity matches. Parent, brand, franchise and similarly named companies are not exact.
5. Crawl static HTML first. Escalate to a browser only when a deterministic completeness check fails.
6. Measure added supported coverage, wrong-company claims, runtime, requests and cost.
7. Promote a strategy only when it improves coverage without weakening the accuracy gates.
8. Freeze strategies and thresholds before the daily evaluation run.

The strongest differentiator is external evidence that remains exact and auditable: official company pages, company-owned profiles, jobs, dated activity, ratings/reviews and permitted public signals. Do not trade accuracy for volume.

## Important source rule

Open-source code does not grant permission to scrape a platform. Follow each source's terms, robots policy, rate limits and licence. LinkedIn, Meta and Indeed are useful identity/discovery targets, but direct automated collection may be restricted. Use permitted APIs, licensed providers, company-owned outbound links, or return `blocked`/`not_available`.

Read `docs/competition-control-loop.md`, `docs/external-connectors.md` and the public source policy before adding connectors.

## Optional API Keys & Configuration

The official agent works without API keys. Use `--max-cost 0` to enforce zero spend on paid website discovery. Supply optional credentials as process environment variables. See `.env.example` for their names.

| Environment Variable | Connector | Description | Default / Fallback |
| :--- | :--- | :--- | :--- |
| `GOOGLE_PLACES_API_KEY` | Google Places API | Transient website candidates using evaluator-authorized access | Off ($0.00) / Registry and company-site discovery |
| `YOUTUBE_API_KEY` | YouTube Data API v3 | Official video channel metrics & engagement | Off ($0.00) / Website-linked channels |
| `PATENTSTYRET_API_KEY`| Patentstyret API | Norwegian patents & registered trademarks | Off ($0.00) / Bulk NIPO records |
| `BRAVE_API_KEY` | Brave Search API | Fallback search discovery for missing websites | Off ($0.00) / Norid RDAP & heuristic engine |
| `SIGNALPOST_NAV_FEED_TOKEN` | NAV Job Vacancy Feed | Registered access for active job verification | Off / Verified company career pages |

## Submission contract

Submit a repository with:

- a 100-company smoke-test result or run report;
- one documented command that accepts a JSONL batch of organisation numbers;
- exactly one terminal envelope per input;
- pinned dependencies and reproducible setup;
- a previous-snapshot input and material-change output;
- a machine-readable run report with runtime, request count and third-party cost;
- declared models, APIs, licences and source-rights assumptions.

Email the repository URL, exact commit, public smoke report, evaluator command, API and licence disclosures, expected cost per official run, agent name and contact to `submit@builderr.ai`. See [SUBMISSION.md](SUBMISSION.md).

## Independent audit (optional local evidence)

Every run writes an unreviewed `external-audit-labels.jsonl` queue. This is not a failed audit and it must not be self-labelled. To measure local exact-company precision, create a fixed 100-observation sample with `scripts/prepare_external_audit.py`, have an independent reviewer complete it, then pass that file to `run_agent.py --audit-labels <completed-labels.jsonl>`. See `docs/external-audit-workflow.md`.
