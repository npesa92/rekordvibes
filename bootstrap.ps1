# Bootstrap the rekordvibes skill on Windows.
# UNTESTED branch: written to spec, not yet run on real Windows — please
# report results. Idempotent; safe to re-run.
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

# Find a Python 3.11+ via the py launcher, then PATH.
$py = $null
foreach ($ver in @("3.13", "3.12", "3.11")) {
    & py "-$ver" -c "pass" 2>$null
    if ($LASTEXITCODE -eq 0) { $py = @("py", "-$ver"); break }
}
if (-not $py) {
    & python -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)" 2>$null
    if ($LASTEXITCODE -eq 0) { $py = @("python") }
}
if (-not $py) {
    Write-Error "No Python 3.11+ found (tried py -3.13/-3.12/-3.11 and python). Install from python.org."
}
Write-Host "Using $(& $py[0] $py[1..($py.Length)] -V)"

if (-not (Test-Path "venv")) { & $py[0] $py[1..($py.Length)] -m venv venv }
& .\venv\Scripts\pip install --quiet --upgrade pip
& .\venv\Scripts\pip install --quiet -r scripts\requirements.txt

Write-Host ""
Write-Host "Core install done. Optional analysis extras (~400 MB; rhythm/timbre lanes):"
Write-Host "  .\venv\Scripts\pip install -r scripts\requirements-analysis.txt"
Write-Host ""
& .\venv\Scripts\python scripts\rbx.py setup
