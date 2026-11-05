# Starts a local n8n for DevPilot on 127.0.0.1:5678 only, with its data on D:, and imports DevPilot's workflow templates.
# Run it yourself: it pulls the n8n image from Docker Hub the first time (about 600 MB). Nothing else is contacted.
#
#   .\scripts\n8n-local.ps1                 # start (or restart) n8n and import the templates
#   .\scripts\n8n-local.ps1 -Stop           # stop and remove the container; the data folder stays
#
# The webhook secret is read from N8N_WEBHOOK_SECRET in the repo's .env and passed to the container. It is never printed.
# After the first start: open http://127.0.0.1:5678, create the owner account, create an API key under Settings > n8n API,
# put it in .env as N8N_API_KEY, and set N8N_ENABLED=true and
# N8N_CALLBACK_URL=http://host.docker.internal:8000/integrations/n8n/callback.
param(
    [string]$Image = "n8nio/n8n",
    [string]$DataDir = "D:\DevPilotCache\n8n",
    [switch]$Stop
)
$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot
$name = "devpilot-n8n"

if ($Stop) {
    docker rm -f $name | Out-Null
    Write-Output "Stopped $name. Its data stays in $DataDir."
    return
}

$line = Get-Content (Join-Path $repo ".env") | Where-Object { $_ -match '^\s*N8N_WEBHOOK_SECRET\s*=' } | Select-Object -First 1
$secret = if ($line) { ($line -split '=', 2)[1].Trim().Trim('"').Trim("'") } else { "" }
if (-not $secret) {
    Write-Output "Set N8N_WEBHOOK_SECRET in .env first (any long random string; DevPilot and n8n share it)."
    exit 1
}

New-Item -ItemType Directory -Force $DataDir | Out-Null
docker rm -f $name 2>$null | Out-Null
docker run -d --name $name -p 127.0.0.1:5678:5678 `
    -v "${DataDir}:/home/node/.n8n" -v "$(Join-Path $repo 'n8n\workflows'):/workflows:ro" `
    -e NODE_FUNCTION_ALLOW_BUILTIN=crypto -e N8N_BLOCK_ENV_ACCESS_IN_NODE=false -e "DEVPILOT_WEBHOOK_SECRET=$secret" `
    $Image | Out-Null
$digest = docker image inspect $Image --format "{{index .RepoDigests 0}}"
Write-Output "Started $name from $digest (record this digest to pin the version)."

Start-Sleep -Seconds 8
docker exec $name n8n import:workflow --separate --input=/workflows
Write-Output "Imported DevPilot's workflow templates. Activate each one in the n8n editor (toggle 'Active'), then check:"
Write-Output "  GET http://127.0.0.1:8000/api/v1/integrations/n8n/status   (every workflow should show found and active)"
