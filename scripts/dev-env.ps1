# DevPilot AI development environment for the current PowerShell session only.
# Usage (from the repo root):  . .\scripts\dev-env.ps1
#
# Points temp files and package caches away from C:, which is small on the original development laptop: at DEVPILOT_CACHE if
# set, else D:\DevPilotCache when there is a D: drive, else .cache inside this clone (gitignored).
# Nothing here changes Windows settings or persists after the session ends.

$repoRoot = Split-Path -Parent $PSScriptRoot
$cacheRoot = if ($env:DEVPILOT_CACHE) { $env:DEVPILOT_CACHE } elseif (Test-Path -LiteralPath 'D:') { 'D:\DevPilotCache' } else { Join-Path $repoRoot '.cache' }
foreach ($dir in "$cacheRoot\temp", "$cacheRoot\pip", "$cacheRoot\uv", "$cacheRoot\npm") {
    if (-not (Test-Path -LiteralPath $dir)) { New-Item -ItemType Directory -Path $dir | Out-Null }
}

$env:TEMP = "$cacheRoot\temp"
$env:TMP = "$cacheRoot\temp"
$env:PIP_CACHE_DIR = "$cacheRoot\pip"
$env:UV_CACHE_DIR = "$cacheRoot\uv"
$env:npm_config_cache = "$cacheRoot\npm"
$env:PIP_DISABLE_PIP_VERSION_CHECK = '1'
$env:PIP_REQUIRE_VIRTUALENV = '1'   # refuse accidental global installs
$env:PYTHONUTF8 = '1'

$env:DEVPILOT_PYTHON = Join-Path $repoRoot 'backend\.venv\Scripts\python.exe'

Write-Host "DevPilot dev env: TEMP=$env:TEMP  PIP_CACHE_DIR=$env:PIP_CACHE_DIR"
Write-Host "Interpreter: $env:DEVPILOT_PYTHON"
