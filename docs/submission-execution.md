# Submission execution and source controls

## Official-safe command

```powershell
uv run python run_agent.py `
  --organisations <builderr-supplied-input.jsonl> `
  --bulk brreg-enheter.csv `
  --output-dir out/official-run `
  --expected-count <input-count> `
  --workers 8 `
  --source-policy official_safe
```

The run must emit one terminal envelope for every input. Official-safe mode
does not use unofficial LinkedIn, Meta, Proff, or undeclared sources.

## Optional server-side secret

`BRAVE_SEARCH_API_KEY` is optional. When supplied by an evaluator as a process
environment variable, it may discover candidate domains only. Search responses
are never evidence. The candidate is fetched and must pass the legal-entity
identity gate before website claims publish. When the variable is absent, the
agent skips this step and continues normally.

Do not store secrets in Git, `.env`, output JSONL, evidence, or logs.

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
