$ErrorActionPreference = "Stop"

$repoRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
$venvPath = Join-Path $repoRoot ".venv"
$requirements = Join-Path $repoRoot "requirements.txt"
$pythonScript = Join-Path $repoRoot "spotify_duck.py"
$binDir = Join-Path $repoRoot "src-tauri\\bin"
$buildRoot = Join-Path $repoRoot ".pybuild"
$workPath = Join-Path $buildRoot "work"
$specPath = Join-Path $buildRoot "spec"
$usePy312 = $false

if (Get-Command py -ErrorAction SilentlyContinue) {
    try {
        py -3.12 -c "import sys" *> $null
        $usePy312 = $true
    } catch {
        $usePy312 = $false
    }
}

if (-not (Test-Path $venvPath)) {
    if ($usePy312) {
        py -3.12 -m venv $venvPath
    } else {
        python -m venv $venvPath
    }
}

$pythonExe = Join-Path $venvPath "Scripts\\python.exe"
if (-not (Test-Path $pythonExe)) {
    throw "Python virtual environment is missing."
}

$pythonVersion = (& $pythonExe -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')").Trim()
if ([version]$pythonVersion -ge [version]"3.13") {
    if ($usePy312) {
        Remove-Item $venvPath -Recurse -Force
        py -3.12 -m venv $venvPath
        $pythonExe = Join-Path $venvPath "Scripts\\python.exe"
        $pythonVersion = (& $pythonExe -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')").Trim()
    } else {
        throw "Python $pythonVersion is not supported for sidecar packaging. Install Python 3.12 and run this script again."
    }
}

& $pythonExe -m pip install --upgrade pip
& $pythonExe -m pip install -r $requirements pyinstaller

New-Item -ItemType Directory -Force -Path $binDir | Out-Null
New-Item -ItemType Directory -Force -Path $workPath | Out-Null
New-Item -ItemType Directory -Force -Path $specPath | Out-Null

& $pythonExe -m PyInstaller --noconfirm --clean --onefile --noconsole --name cleanfade-engine --distpath $binDir --workpath $workPath --specpath $specPath $pythonScript

if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller failed to build cleanfade-engine."
}

$builtExe = Join-Path $binDir "cleanfade-engine.exe"
if (-not (Test-Path $builtExe)) {
    throw "Expected sidecar executable was not created: $builtExe"
}

$hostTriple = "x86_64-pc-windows-msvc"
try {
    $rustcInfo = (rustc -vV 2>$null | Out-String)
    $hostMatch = [regex]::Match($rustcInfo, "(?m)^\s*host:\s*([A-Za-z0-9._-]+)\s*$")
    if ($hostMatch.Success) {
        $hostTriple = $hostMatch.Groups[1].Value
    }
} catch {
    Write-Host "rustc not found when detecting host triple. Using default: $hostTriple"
}

$hostTriple = ($hostTriple -replace "[^A-Za-z0-9._-]", "").Trim()
if ([string]::IsNullOrWhiteSpace($hostTriple)) {
    $hostTriple = "x86_64-pc-windows-msvc"
}

$targetExe = Join-Path $binDir ("cleanfade-engine-" + $hostTriple + ".exe")
Copy-Item -Path $builtExe -Destination $targetExe -Force

Write-Host "Built sidecar:" $builtExe
Write-Host "Copied sidecar for Tauri bundle:" $targetExe
