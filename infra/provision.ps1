<#
Creates Bol's Azure resources (the "30 Oct: provision" step of the Azure plan). Safe to run again: anything that
already exists is kept.

  Run from the bol folder:   powershell -ExecutionPolicy Bypass -File infra\provision.ps1

Needs: the Azure CLI signed in (az login) to the Azure for Students subscription, and the .env file with
ANTHROPIC_API_KEY and VACHANA_API_KEY.

Bol stores nothing (the browser keeps each student's practice), so there is no database, no file storage and no
sign-in to set up: just the API, its image registry, logs, and the static website.

Region: the subscription allows only eastasia, uaenorth, indiasouthcentral, indonesiacentral and koreacentral.
Container Apps isn't offered in indiasouthcentral, so everything runs in UAE North, the closest allowed region to
India. Static Web Apps is only offered in East Asia; its files are served from Microsoft's global edge anyway.

The two API keys become secrets of the container app; they are never printed.
#>
param(
  [string]$Region = "uaenorth",
  [string]$WebRegion = "eastasia",
  [string]$Group = "bol-rg"
)
# Not "Stop": in Windows PowerShell 5.1 that turns any az warning on stderr into a crash. Az() checks exit codes.
$ErrorActionPreference = "Continue"
$az = (Get-Command az -ErrorAction SilentlyContinue).Source
if (-not $az) { $az = "C:\Program Files\Microsoft SDKs\Azure\CLI2\wbin\az.cmd" }
$root = Split-Path -Parent $PSScriptRoot

function Az {
  # Runs az and stops the script on failure. Output is returned as text.
  $out = & $az @args
  if ($LASTEXITCODE -ne 0) { throw "az $($args[0..2] -join ' ') ... failed (exit $LASTEXITCODE)" }
  return $out
}
function Exists { # az <kind> show ... ; true if the resource is there
  & $az @args -o none 2>$null
  return ($LASTEXITCODE -eq 0)
}
function Step($text) { Write-Host "`n== $text" -ForegroundColor Cyan }
function Role($principal, $type, $role, $scope) {
  $have = & $az role assignment list --assignee $principal --role $role --scope $scope --query "length(@)" -o tsv 2>$null
  if ($have -ne "0" -and $have) { return }
  for ($i = 0; $i -lt 8; $i++) { # a brand-new identity can take a minute to be visible
    & $az role assignment create --assignee-object-id $principal --assignee-principal-type $type --role $role --scope $scope -o none 2>$null
    if ($LASTEXITCODE -eq 0) { return }
    Start-Sleep -Seconds 15
  }
  throw "couldn't assign $role"
}

# ---------- names (globally unique ones get a short suffix from the subscription id) ----------
Az config set extension.use_dynamic_install=yes_without_prompt core.only_show_errors=true -o none
$sub = Az account show --query id -o tsv
$sfx = $sub.Substring(0, 5)
$names = @{
  logs = "bol-logs"; insights = "bol-ai"; acr = "bolacr$sfx"; env = "bol-env"; app = "bol-api"; web = "bol-web"
}
Write-Host "Subscription: $(Az account show --query name -o tsv)   region: $Region   group: $Group"

# ---------- env file (keys for Key Vault) ----------
$envFile = Join-Path $root ".env"
$dotenv = @{}
Get-Content $envFile | Where-Object { $_ -match '^\s*([A-Z_]+)\s*=\s*(.*)$' } | ForEach-Object {
  $dotenv[$Matches[1]] = $Matches[2].Trim().Trim('"').Trim("'")
}
foreach ($k in "ANTHROPIC_API_KEY", "VACHANA_API_KEY") { if (-not $dotenv[$k]) { throw "$k is empty in .env" } }

Step "Resource group"
Az group create -n $Group -l $Region -o none

Step "Log Analytics (30-day retention, 0.15 GB/day cap) and Application Insights"
Az monitor log-analytics workspace create -g $Group -n $names.logs -l $Region --retention-time 30 --quota 0.15 -o none
$wsId = Az monitor log-analytics workspace show -g $Group -n $names.logs --query id -o tsv
$wsCustomer = Az monitor log-analytics workspace show -g $Group -n $names.logs --query customerId -o tsv
$wsKey = Az monitor log-analytics workspace get-shared-keys -g $Group -n $names.logs --query primarySharedKey -o tsv
if (-not (Exists monitor app-insights component show -g $Group --app $names.insights)) {
  Az monitor app-insights component create -g $Group --app $names.insights -l $Region --workspace $wsId --kind web --application-type web -o none
}
$insightsConn = Az monitor app-insights component show -g $Group --app $names.insights --query connectionString -o tsv

Step "Container Registry $($names.acr) (Standard)"
if (-not (Exists acr show -g $Group -n $names.acr)) {
  Az acr create -g $Group -n $names.acr -l $Region --sku Standard --admin-enabled false -o none
}
$acrId = Az acr show -g $Group -n $names.acr --query id -o tsv

Step "Container Apps environment $($names.env)"
if (-not (Exists containerapp env show -g $Group -n $names.env)) {
  Az containerapp env create -g $Group -n $names.env -l $Region --logs-workspace-id $wsCustomer --logs-workspace-key $wsKey -o none
}

Step "Container app $($names.app) (0.5 vCPU, 1 GiB, 0-1 replicas; a placeholder image until the first deploy)"
if (-not (Exists containerapp show -g $Group -n $names.app)) {
  # Max 1 replica: Gnani allows one request at a time per key, so one replica keeps the queue in one place.
  Az containerapp create -g $Group -n $names.app --environment $names.env --image mcr.microsoft.com/k8se/quickstart:latest `
    --target-port 80 --ingress external --cpu 0.5 --memory 1.0Gi --min-replicas 0 --max-replicas 1 --system-assigned -o none
}
$appIdentity = Az containerapp show -g $Group -n $names.app --query identity.principalId -o tsv
Role $appIdentity "ServicePrincipal" "AcrPull" $acrId
Az containerapp registry set -g $Group -n $names.app --server "$($names.acr).azurecr.io" --identity system -o none
# The keys become container app secrets (encrypted at rest; visible only to people with access to the app).
Az containerapp secret set -g $Group -n $names.app -o none --secrets `
  "anthropic-api-key=$($dotenv['ANTHROPIC_API_KEY'])" "vachana-api-key=$($dotenv['VACHANA_API_KEY'])" `
  "insights-connection-string=$insightsConn"
Write-Host "API keys stored as container app secrets"
$apiHost = Az containerapp show -g $Group -n $names.app --query properties.configuration.ingress.fqdn -o tsv

Step "Static Web App $($names.web) (Free)"
if (-not (Exists staticwebapp show -g $Group -n $names.web)) {
  Az staticwebapp create -g $Group -n $names.web -l $WebRegion --sku Free -o none
}
$webHost = Az staticwebapp show -g $Group -n $names.web --query defaultHostname -o tsv

# ---------- what later steps need (no secrets) ----------
$outputs = [ordered]@{
  subscriptionId = $sub; resourceGroup = $Group; region = $Region
  acrLoginServer = "$($names.acr).azurecr.io"; containerApp = $names.app; containerAppEnv = $names.env
  apiUrl = "https://$apiHost"; webUrl = "https://$webHost"; staticWebApp = $names.web
}
$outputs | ConvertTo-Json | Set-Content -Path (Join-Path $PSScriptRoot "outputs.json") -Encoding utf8
Step "Done"
$outputs.GetEnumerator() | ForEach-Object { "{0,-16} {1}" -f $_.Key, $_.Value }
Write-Host "`nSaved to infra\outputs.json. Next: the first deploy (GitHub Actions)."
