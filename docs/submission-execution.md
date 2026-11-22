# Submission execution and source controls

## Official-safe command

Use the [evaluator command in the submission checklist](../SUBMISSION.md) with Builderr's input, frozen registry and budget.

The run must emit one terminal envelope for every input. Official-safe mode
does not use unofficial LinkedIn, Meta, Proff, or undeclared sources.

## Optional server-side secret

`BRAVE_SEARCH_API_KEY` is optional. When supplied by an evaluator as a process
environment variable, it may discover candidate domains only. Search responses
are never evidence. The candidate is fetched and must pass the legal-entity
identity gate before website claims publish. When the variable is absent, the
agent skips this step and continues normally.

Supply evaluator secrets through process environment variables. Do not store them in Git, output JSONL, evidence, or logs.

The public company-site crawler checks robots rules and rejects private or reserved addresses. The connection checks the resolved address before opening a socket, including after redirects. Public Tavily keyless search is rate-limited and supplies website leads. Google Places is optional and transient. See the [source registry](../config/source-rights.json) for publication boundaries and platform documentation.

Paid discovery reserves the request and its estimated cost before making the call. Estimates use $0.005 per Brave search, $0.035 per Places Text Search (New) request and up to $0.057 when a separate Legacy Text Search and Place Details request are needed. [Brave pricing](https://brave.com/search/api/), [Google Maps Platform pricing](https://developers.google.com/maps/billing-and-pricing/pricing).

| Source | Rights and use |
| --- | --- |
| Brønnøysundregistrene | NLOD 2.0 for open entity, role and subunit data. The run records the registry file hash and links to source records. [Open registry API](https://data.brreg.no/enhetsregisteret/api/dokumentasjon/en/index.html) |
| Patentstyret | NLOD for public trademark, patent and design case data with source attribution. The connector uses an evaluator-provided subscription key if configured. [NIPO open data](https://www.patentstyret.no/en/open-data) |
| Kartverket | Open address API under Kartverket's open-data terms. Address corroboration is linked to the registry record for the company. [Kartverket open data](https://www.kartverket.no/en/api-and-data/eiendomsdata/apent-tilgjengelige-data-og-tjenester) |
| NAV | Registered feed access under the NAV vacancy API terms. Evaluation artifacts are timestamped snapshots. A public service displaying live NAV adverts needs prompt updates and removals. [Feed terms](https://arbeidsplassen.nav.no/vilkar-api) |
| Company sites and recruitment boards | Public pages with permitted automated access and documented board APIs linked by the employer. Published evidence identifies the individual source item and its legal employer or operator. [Builderr source policy](https://builderr.ai/starter-briefs/signalpost-sources.md) |

## Prior verified-site cache

Build the cache from an earlier official-safe run:

```powershell
uv run python scripts/build_verified_site_cache.py `
  --profiles out/official-safe-100-r3/profiles.jsonl `
  --output out/official-safe-100-r3/verified-sites.jsonl
```

Reuse it only as a revalidation hint:

```powershell
uv run python run_agent.py `
  --organisations dev-100-companies.jsonl `
  --bulk brreg-enheter.csv `
  --output-dir out/official-safe-100-r4 `
  --expected-count 100 `
  --workers 8 `
  --source-policy official_safe `
  --verified-site-cache out/official-safe-100-r3/verified-sites.jsonl
```

## Specialist registry trial

Every specialist source stays conditional until this trial, a rights review,
and an independently reviewed promotion report show an incremental benefit.

```powershell
uv run python scripts/run_specialist_connector_trial.py `
  --profiles out/official-safe-100-r3/profiles.jsonl `
  --connector doffin `
  --output-dir out/specialist-trials/doffin-100 `
  --limit 100
```

Repeat with `forskningsradet` and `innovasjon_norge`. Candidate output is not
official-safe output and must not be merged into profiles until promotion.

## Clean-machine smoke test

Run this only after committing the intended files. It clones the repository to
a new temporary directory, runs `uv sync --locked`, executes the agent, and
checks the output contract:

```powershell
.\scripts\clean_machine_smoke.ps1 `
  -RepositoryPath (Get-Location) `
  -OrganisationFile (Join-Path (Get-Location) 'dev-100-companies.jsonl') `
  -BulkFile (Join-Path (Get-Location) 'brreg-enheter.csv') `
  -ExpectedCount 100
```
