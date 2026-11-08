$ErrorActionPreference = "Stop"

$repoRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
$pythonScript = Join-Path $PSScriptRoot "build_sidecar.py"

$python = Get-Command python -ErrorAction SilentlyContinue
if (-not $python) {
    $python = Get-Command py -ErrorAction SilentlyContinue
    if (-not $python) {
        throw "Python is required for sidecar packaging. Install Python 3.12 and ensure it is on PATH."
    }
    & $python.Source -3.12 $pythonScript
} else {
    & $python.Source $pythonScript
}

if ($LASTEXITCODE -ne 0) {
    throw "Sidecar build failed."
}
