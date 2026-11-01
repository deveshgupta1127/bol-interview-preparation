<#
Switches the API (container app bol-api) to the image GitHub Actions built, then checks it's healthy.

  Run from the bol folder, after the "Test and deploy" workflow has finished:
    powershell -ExecutionPolicy Bypass -File infra\deploy.ps1            # the commit you have checked out
    powershell -ExecutionPolicy Bypass -File infra\deploy.ps1 -Tag latest

  Demo days (no cold start; uses a little more of the free grant):  add  -AlwaysOn
  After judging, back to sleeping when idle:                          add  -SleepWhenIdle

Needs: infra\outputs.json from provision.ps1, and the Azure CLI signed in.
#>
param(
  [string]$Tag = "",
  [switch]$AlwaysOn,
  [switch]$SleepWhenIdle
)
$ErrorActionPreference = "Continue"
$az = (Get-Command az -ErrorAction SilentlyContinue).Source
if (-not $az) { $az = "C:\Program Files\Microsoft SDKs\Azure\CLI2\wbin\az.cmd" }
$root = Split-Path -Parent $PSScriptRoot
function Az { $out = & $az @args; if ($LASTEXITCODE -ne 0) { throw "az $($args[0..2] -join ' ') ... failed" }; return $out }

$out = Get-Content (Join-Path $PSScriptRoot "outputs.json") -Raw | ConvertFrom-Json
if (-not $Tag) { $Tag = (git -C $root rev-parse HEAD).Trim() }
$remote = (git -C $root remote get-url origin).Trim()
if ($remote -notmatch "github\.com[:/](.+?)(\.git)?$") { throw "origin isn't a GitHub repository: $remote" }
$image = "ghcr.io/$($Matches[1].ToLower()):$Tag"

Write-Host "Deploying $image to $($out.containerApp)"
$replicas = @()
if ($AlwaysOn) { $replicas = @("--min-replicas", "1") }
if ($SleepWhenIdle) { $replicas = @("--min-replicas", "0") }
Az containerapp update -g $out.resourceGroup -n $out.containerApp --image $image @replicas -o none
Az containerapp ingress update -g $out.resourceGroup -n $out.containerApp --target-port 8000 -o none

Write-Host "Waiting for the new version to answer..."
for ($i = 0; $i -lt 30; $i++) {
  try {
    $health = Invoke-RestMethod -Uri "$($out.apiUrl)/api/health" -TimeoutSec 20 -ErrorAction Stop
    if ($health.ok) {
      Write-Host "API healthy: claude=$($health.claude) speech=$($health.speech) stores_data=$($health.stores_data)" -ForegroundColor Green
      Write-Host "Website: $($out.webUrl)"
      exit 0
    }
  } catch { Start-Sleep -Seconds 10 }
}
throw "The API didn't answer in 5 minutes. Look at: az containerapp logs show -g $($out.resourceGroup) -n $($out.containerApp) --tail 50"
