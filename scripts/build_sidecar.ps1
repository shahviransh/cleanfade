$ErrorActionPreference = "Stop"

$repoRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
$requirements = Join-Path $repoRoot "requirements.txt"
$pythonScript = Join-Path $repoRoot "spotify_duck.py"
$binDir = Join-Path $repoRoot "src-tauri\\bin"
$buildRoot = Join-Path $repoRoot ".pybuild"
$workPath = Join-Path $buildRoot "work"
$specPath = Join-Path $buildRoot "spec"

$condaCmd = Get-Command conda -ErrorAction SilentlyContinue
if (-not $condaCmd) {
    throw "Conda is required for sidecar packaging. Install Miniconda/Anaconda and ensure 'conda' is on PATH."
}

$condaExe = $condaCmd.Source
$condaEnvName = if ([string]::IsNullOrWhiteSpace($env:CLEANFADE_CONDA_ENV)) { "cleanfade" } else { $env:CLEANFADE_CONDA_ENV }

$envExists = $false
try {
    $envListJson = (& $condaExe env list --json | Out-String)
    $envList = $envListJson | ConvertFrom-Json
    if ($null -ne $envList.envs) {
        foreach ($envPath in $envList.envs) {
            if ((Split-Path $envPath -Leaf) -eq $condaEnvName) {
                $envExists = $true
                break
            }
        }
    }
} catch {
    throw "Failed to inspect Conda environments. Verify Conda installation is healthy."
}

if (-not $envExists) {
    & $condaExe create -y -n $condaEnvName python=3.12
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to create Conda environment '$condaEnvName' with Python 3.12."
    }
}

$pythonVersion = (& $condaExe run -n $condaEnvName python -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')").Trim()
if ([version]$pythonVersion -ge [version]"3.13") {
    throw "Conda environment '$condaEnvName' is using Python $pythonVersion. Use Python 3.12 for sidecar packaging."
}

& $condaExe run -n $condaEnvName python -m pip install --upgrade pip
if ($LASTEXITCODE -ne 0) {
    throw "Failed to upgrade pip in Conda environment '$condaEnvName'."
}

& $condaExe run -n $condaEnvName python -m pip install -r $requirements pyinstaller
if ($LASTEXITCODE -ne 0) {
    throw "Failed to install Python dependencies in Conda environment '$condaEnvName'."
}

New-Item -ItemType Directory -Force -Path $binDir | Out-Null
New-Item -ItemType Directory -Force -Path $workPath | Out-Null
New-Item -ItemType Directory -Force -Path $specPath | Out-Null

$pyInstallerArgs = @(
    "--noconfirm",
    "--clean",
    "--onefile",
    "--noconsole",
    "--collect-data", "faster_whisper",
    "--collect-submodules", "faster_whisper",
    "--name", "cleanfade-engine",
    "--distpath", $binDir,
    "--workpath", $workPath,
    "--specpath", $specPath,
    $pythonScript
)

& $condaExe run -n $condaEnvName python -m PyInstaller @pyInstallerArgs

if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller failed to build cleanfade-engine."
}

$builtBinary = Join-Path $binDir "cleanfade-engine"
if (-not (Test-Path $builtBinary)) {
    $builtBinary = Join-Path $binDir "cleanfade-engine.exe"
}

if (-not (Test-Path $builtBinary)) {
    throw "Expected sidecar binary was not created at $binDir"
}

$targetTriple = $env:TAURI_ENV_TARGET_TRIPLE
if ([string]::IsNullOrWhiteSpace($targetTriple)) {
    $targetTriple = $env:CLEANFADE_TARGET_TRIPLE
}

if ([string]::IsNullOrWhiteSpace($targetTriple)) {
    $targetTriple = "x86_64-pc-windows-msvc"
    try {
        $rustcInfo = (rustc -vV 2>$null | Out-String)
        $hostMatch = [regex]::Match($rustcInfo, "(?m)^\s*host:\s*([A-Za-z0-9._-]+)\s*$")
        if ($hostMatch.Success) {
            $targetTriple = $hostMatch.Groups[1].Value
        }
    } catch {
        Write-Host "rustc not found when detecting host triple. Using default: $targetTriple"
    }
}

$targetTriple = ($targetTriple -replace "[^A-Za-z0-9._-]", "").Trim()
if ([string]::IsNullOrWhiteSpace($targetTriple)) {
    $targetTriple = "x86_64-pc-windows-msvc"
}

$binaryExtension = [System.IO.Path]::GetExtension($builtBinary)
$targetBinary = Join-Path $binDir ("cleanfade-engine-" + $targetTriple + $binaryExtension)
Copy-Item -Path $builtBinary -Destination $targetBinary -Force

Write-Host "Built sidecar:" $builtBinary
Write-Host "Copied sidecar for Tauri bundle:" $targetBinary
