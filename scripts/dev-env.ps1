# DevPilot AI development environment for the current PowerShell session only.
# Usage (from the repo root):  . .\scripts\dev-env.ps1
#
# Points temp files and package caches at D: because C: has very little free space.
# Nothing here changes Windows settings or persists after the session ends.

$cacheRoot = 'D:\DevPilotCache'
foreach ($dir in "$cacheRoot\temp", "$cacheRoot\pip") {
    if (-not (Test-Path -LiteralPath $dir)) { New-Item -ItemType Directory -Path $dir | Out-Null }
}

$env:TEMP = "$cacheRoot\temp"
$env:TMP = "$cacheRoot\temp"
$env:PIP_CACHE_DIR = "$cacheRoot\pip"
$env:PIP_DISABLE_PIP_VERSION_CHECK = '1'
$env:PIP_REQUIRE_VIRTUALENV = '1'   # refuse accidental global installs
$env:PYTHONUTF8 = '1'

$repoRoot = Split-Path -Parent $PSScriptRoot
$env:DEVPILOT_PYTHON = Join-Path $repoRoot 'backend\.venv\Scripts\python.exe'

Write-Host "DevPilot dev env: TEMP=$env:TEMP  PIP_CACHE_DIR=$env:PIP_CACHE_DIR"
Write-Host "Interpreter: $env:DEVPILOT_PYTHON"
