# DevPilot AI: Windows setup. Safe to run again; each step is skipped if it is already done.
#
#   .\setup.cmd                 interactive: asks for your OpenAI key (hidden), model and prices
#   .\setup.cmd --key-from-env  use the OPENAI_API_KEY environment variable instead of asking
#   .\setup.cmd --rebuild       build and audit the Docker runtime image again
#
# What it does: checks Git, Python 3.12 and Docker; creates an isolated Python environment (.venv) in this folder; installs the pinned
# dependencies; then runs `devpilot setup`, which stores your key encrypted for your Windows account, downloads and verifies OpenCode
# and ripgrep (both pinned by hash), builds the Docker runtime image, audits its contents offline, and runs the readiness check.
# It never prints, commits or uploads your key, and makes no OpenAI request. Extra arguments go to `devpilot setup`.

$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$env:PYTHONUTF8 = '1'

function Stop-Setup([string]$Message) {
    Write-Host ''
    Write-Host "Setup stopped: $Message" -ForegroundColor Red
    exit 1
}

function Step([string]$Message) { Write-Host ''; Write-Host "== $Message" -ForegroundColor Cyan }

# --- 1. Python 3.12 ---------------------------------------------------------------------------------
Step 'Checking Python 3.12'
$python = $null
$candidates = @(
    [pscustomobject]@{ Exe = 'py'; Args = @('-3.12') },
    [pscustomobject]@{ Exe = 'python3.12'; Args = @() },
    [pscustomobject]@{ Exe = 'python'; Args = @() }
)
foreach ($candidate in $candidates) {
    $command = Get-Command $candidate.Exe -ErrorAction SilentlyContinue
    if (-not $command) { continue }
    $extra = @($candidate.Args)
    try {
        $version = & $command.Source @extra -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>$null
    } catch { continue }
    if ($LASTEXITCODE -eq 0 -and $version -eq '3.12') { $python = @($command.Source) + $extra; break }
}
if (-not $python) {
    Stop-Setup ("Python 3.12 was not found. DevPilot needs exactly Python 3.12 (3.13 is not supported yet).`n" +
        "Install it from https://www.python.org/downloads/release/python-3129/ (tick 'Add python.exe to PATH'),`n" +
        "or run:  winget install Python.Python.3.12`n" +
        "Then open a new terminal and run setup.cmd again.")
}
Write-Host "  found: $($python -join ' ')"

# --- 2. Git (informational) -------------------------------------------------------------------------
Step 'Checking Git'
if (Get-Command git -ErrorAction SilentlyContinue) { Write-Host '  found.' }
else { Write-Host '  Git was not found. DevPilot works without it; install Git for Windows only to update DevPilot later.' -ForegroundColor Yellow }

# --- 3. Docker --------------------------------------------------------------------------------------
Step 'Checking Docker'
if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    Stop-Setup ("Docker was not found. Install Docker Desktop (https://www.docker.com/products/docker-desktop/) with the WSL2 backend,`n" +
        "start it, wait until it says it is running, then run setup.cmd again.")
}
& docker version --format '{{.Server.Version}}' *> $null
if ($LASTEXITCODE -ne 0) {
    Stop-Setup 'Docker is installed but not running. Start Docker Desktop, wait until it says it is running, then run setup.cmd again.'
}
Write-Host '  Docker is running.'

# --- 4. Isolated environment and dependencies -------------------------------------------------------
Step 'Creating the isolated Python environment (.venv in this folder)'
$venvPython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $venvPython)) {
    $extra = @($python | Select-Object -Skip 1)
    & $python[0] @extra -m venv .venv
    if ($LASTEXITCODE -ne 0) { Stop-Setup 'The Python environment could not be created.' }
}
Write-Host '  ready.'

Step 'Installing the pinned dependencies (needs the internet; this can take a few minutes the first time)'
& $venvPython -m pip install --disable-pip-version-check --quiet -r backend\requirements.lock
if ($LASTEXITCODE -ne 0) { Stop-Setup 'Installing the dependencies failed. Check your internet connection and run setup.cmd again.' }
& $venvPython -m pip install --disable-pip-version-check --quiet --no-deps -e backend
if ($LASTEXITCODE -ne 0) { Stop-Setup 'Installing DevPilot itself failed.' }
Write-Host '  installed.'

# --- 5. DevPilot's own setup: key, tools, Docker runtime image, readiness check -------------------------
Step 'Setting up DevPilot (key, OpenCode, Docker runtime image)'
& $venvPython -m app.cli setup @args
$code = $LASTEXITCODE
Write-Host ''
if ($code -eq 0) {
    Write-Host 'Setup finished. Build an application with:  .\devpilot.cmd build' -ForegroundColor Green
} else {
    Write-Host 'Setup is not finished: fix the items marked FAIL above, then run setup.cmd again (it skips what is done).' -ForegroundColor Yellow
}
exit $code
