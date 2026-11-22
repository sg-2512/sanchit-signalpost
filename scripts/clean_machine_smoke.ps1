param(
    [Parameter(Mandatory = $true)][string]$RepositoryPath,
    [Parameter(Mandatory = $true)][string]$OrganisationFile,
    [Parameter(Mandatory = $true)][string]$BulkFile,
    [string]$OutputRoot = "",
    [int]$ExpectedCount = 100
)

$ErrorActionPreference = "Stop"
$repo = (Resolve-Path -LiteralPath $RepositoryPath).Path
$input = (Resolve-Path -LiteralPath $OrganisationFile).Path
$bulk = (Resolve-Path -LiteralPath $BulkFile).Path
if (-not $OutputRoot) { $OutputRoot = Join-Path $repo "out\clean-machine-100" }

# A separate clone and virtual environment prove the declared dependency and
# execution path; no current working-tree modules or virtualenv are reused.
$temp = Join-Path ([System.IO.Path]::GetTempPath()) ("signalpost-clean-" + [guid]::NewGuid().ToString("N"))
git clone --no-local $repo $temp
if ($LASTEXITCODE -ne 0) { throw "git clone failed ($LASTEXITCODE)" }
Push-Location $temp
try {
    uv sync --locked
    if ($LASTEXITCODE -ne 0) { throw "uv sync failed ($LASTEXITCODE)" }
    uv run --locked python -m strategies.freeze --verify --release
    if ($LASTEXITCODE -ne 0) { throw "Frozen commit verification failed ($LASTEXITCODE)" }
    uv run --locked python run_agent.py `
      --organisations $input `
      --bulk $bulk `
      --output-dir $OutputRoot `
      --expected-count $ExpectedCount `
      --workers 8 `
      --source-policy official_safe `
      --resume-mode refresh `
      --max-cost 0
    if ($LASTEXITCODE -ne 0) { throw "Agent smoke test failed ($LASTEXITCODE)" }
    uv run --locked python scripts/validate_submission_ready.py `
      --input $input `
      --output-dir $OutputRoot `
      --output (Join-Path $OutputRoot "submission-readiness.json")
    if ($LASTEXITCODE -ne 0) { throw "Submission readiness check failed ($LASTEXITCODE)" }
}
finally {
    Pop-Location
    Write-Host "Clean clone preserved for inspection: $temp"
}
