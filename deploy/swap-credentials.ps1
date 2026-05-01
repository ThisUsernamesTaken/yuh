# =============================================================================
# BTC Bias Engine — Credential Swap
# =============================================================================
# Use this to switch the engine to a different Kalshi account without a full
# reinstall.  Updates both the NSSM service environment AND the local
# C:\Trading\credentials\kalshi.env file used by health-check tooling.
#
# Run in an ELEVATED (Admin) PowerShell:
#
#   cd C:\Trading\btc-bias-engine
#   .\deploy\swap-credentials.ps1
#
#   Or pass arguments directly (for scripted/silent use):
#   .\deploy\swap-credentials.ps1 -ApiKey "YOUR-UUID" -KeyPath "C:\path\to\key.pem"
#
# What it does:
#   1. Validates API key (UUID format) and PEM file path
#   2. Updates NSSM AppEnvironmentExtra on the BTCBiasEngine service
#   3. Writes C:\Trading\credentials\kalshi.env (used by Claude health checks)
#   4. Stops, clears __pycache__, and restarts the service
#   5. Confirms SERVICE_RUNNING
# =============================================================================

param(
    [string]$ApiKey    = "",
    [string]$KeyPath   = "",
    [string]$ServiceName = "BTCBiasEngine",
    [string]$EnvFile   = "C:\Trading\credentials\kalshi.env",
    [switch]$DemoMode,
    [switch]$NoExecute
)

$ErrorActionPreference = "Stop"

function Write-Step([string]$msg) { Write-Host "`n== $msg ==" -ForegroundColor Cyan }
function Write-OK([string]$msg)   { Write-Host "  OK  $msg" -ForegroundColor Green }
function Write-Fail([string]$msg) { Write-Host "  FAIL  $msg" -ForegroundColor Red; exit 1 }

Write-Host ""
Write-Host "========================================" -ForegroundColor Cyan
Write-Host "  BTC Bias Engine — Credential Swap" -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan
Write-Host ""

# ── Locate NSSM ───────────────────────────────────────────────────────────────
Write-Step "Locating NSSM"
$NssmExe = $null
try { $NssmExe = (Get-Command "nssm" -ErrorAction SilentlyContinue).Source } catch { }
if (-not $NssmExe) {
    $wingetNssm = Get-ChildItem "$env:LOCALAPPDATA\Microsoft\WinGet\Packages" -Recurse `
        -Filter "nssm.exe" -ErrorAction SilentlyContinue |
        Where-Object { $_.FullName -match "win64" } | Select-Object -First 1
    if ($wingetNssm) { $NssmExe = $wingetNssm.FullName }
}
$ProjectRoot = Split-Path -Parent $PSScriptRoot
if (-not $NssmExe) {
    $localNssm = Join-Path $ProjectRoot "tools\nssm.exe"
    if (Test-Path $localNssm) { $NssmExe = $localNssm }
}
if (-not $NssmExe) { Write-Fail "NSSM not found. Is the engine installed?" }
Write-OK "NSSM at $NssmExe"

# ── Confirm service exists ────────────────────────────────────────────────────
$svcExists = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
if (-not $svcExists) { Write-Fail "Service '$ServiceName' not found. Run install.ps1 first." }
Write-OK "Service '$ServiceName' found"

# ── Read existing env to preserve non-credential vars ────────────────────────
Write-Step "Reading existing service configuration"
$regPath = "HKLM:\SYSTEM\CurrentControlSet\Services\$ServiceName\Parameters"
$existingEnv = ""
if (Test-Path $regPath) {
    $existingEnv = (Get-ItemProperty $regPath).AppEnvironmentExtra
}

# Parse existing env into a hashtable
$envVars = [ordered]@{}
foreach ($line in ($existingEnv -split "`n")) {
    $line = $line.Trim()
    if ($line -match "^([^=]+)=(.*)$") {
        $envVars[$Matches[1]] = $Matches[2]
    }
}

# Show current account
$currentKey = $envVars["KALSHI_API_KEY"]
if ($currentKey) {
    Write-Host "  Current API key: $($currentKey.Substring(0,8))..." -ForegroundColor Gray
}

# ── Collect new credentials ──────────────────────────────────────────────────
Write-Step "New Kalshi API Credentials"
Write-Host "  Get these from: https://kalshi.com/account/api" -ForegroundColor Gray
Write-Host ""

if ([string]::IsNullOrWhiteSpace($ApiKey)) {
    $ApiKey = Read-Host "  Enter new Kalshi API Key (UUID)"
}
if ([string]::IsNullOrWhiteSpace($ApiKey)) {
    Write-Fail "API key is required."
}
# Validate UUID format
if ($ApiKey -notmatch "^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$") {
    Write-Fail "Invalid API key format. Expected UUID like: xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"
}
Write-OK "API key format OK"

if ([string]::IsNullOrWhiteSpace($KeyPath)) {
    $KeyPath = Read-Host "  Enter path to RSA private key .pem file"
}
if ([string]::IsNullOrWhiteSpace($KeyPath)) {
    Write-Fail "Private key path is required."
}
# Normalize path separators
$KeyPath = $KeyPath.Replace("/", "\")
if (-not (Test-Path $KeyPath)) {
    Write-Fail "Private key file not found: $KeyPath"
}
# Confirm it looks like a PEM
$pemContent = Get-Content $KeyPath -Raw -ErrorAction SilentlyContinue
if ($pemContent -notmatch "BEGIN.*PRIVATE KEY") {
    Write-Fail "File does not appear to be an RSA private key PEM: $KeyPath"
}
Write-OK "Private key file OK"

# ── Determine execute/demo mode ───────────────────────────────────────────────
$executeMode = if ($NoExecute) { "false" } else { $envVars["EXECUTE_TRADES"] ?? "true" }
$demoMode    = if ($DemoMode)  { "true"  } else { $envVars["KALSHI_DEMO"]    ?? "false" }

Write-Host ""
Write-Host "  EXECUTE_TRADES: $executeMode" -ForegroundColor $(if ($executeMode -eq "true") { "Red" } else { "Yellow" })
Write-Host "  KALSHI_DEMO:    $demoMode"    -ForegroundColor $(if ($demoMode    -eq "true") { "Yellow" } else { "Gray" })

# ── Confirm ───────────────────────────────────────────────────────────────────
Write-Host ""
$confirm = Read-Host "  Apply these credentials? (y/n)"
if ($confirm -ne "y") {
    Write-Host "  Aborted." -ForegroundColor Yellow
    exit 0
}

# ── Build new env string (preserving PYTHONUNBUFFERED etc.) ──────────────────
Write-Step "Updating service environment"

$envVars["KALSHI_API_KEY"]          = $ApiKey
$envVars["KALSHI_PRIVATE_KEY_PATH"] = $KeyPath
$envVars["KALSHI_DEMO"]             = $demoMode
$envVars["EXECUTE_TRADES"]          = $executeMode
if (-not $envVars.Contains("PYTHONUNBUFFERED")) {
    $envVars["PYTHONUNBUFFERED"] = "1"
}

$newEnvString = ($envVars.GetEnumerator() | ForEach-Object { "$($_.Key)=$($_.Value)" }) -join "`n"

# ── Stop service ──────────────────────────────────────────────────────────────
Write-Step "Stopping service"
$svc = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
if ($svc -and $svc.Status -eq "Running") {
    & $NssmExe stop $ServiceName confirm | Out-Null
    Start-Sleep -Seconds 3
    Write-OK "Stopped"
} else {
    Write-OK "Service was not running"
}

# ── Apply NSSM env vars ───────────────────────────────────────────────────────
& $NssmExe set $ServiceName AppEnvironmentExtra $newEnvString
Write-OK "NSSM environment updated"

# ── Write kalshi.env for health-check tooling ────────────────────────────────
Write-Step "Writing $EnvFile"
$envDir = Split-Path $EnvFile
if (-not (Test-Path $envDir)) { New-Item -ItemType Directory -Path $envDir | Out-Null }

@"
# Kalshi API credentials — used by health-check tooling and scripts
# DO NOT commit this file or share it. Keep it alongside your private key.
#
# To update for a new account, run:
#   powershell -ExecutionPolicy Bypass -File C:\Trading\btc-bias-engine\deploy\swap-credentials.ps1
#
KALSHI_API_KEY=$ApiKey
KALSHI_PRIVATE_KEY_PATH=$($KeyPath.Replace('\', '/'))
KALSHI_DEMO=$demoMode
EXECUTE_TRADES=$executeMode
"@ | Set-Content $EnvFile -Encoding UTF8
Write-OK "Written: $EnvFile"

# ── Clear pycache ─────────────────────────────────────────────────────────────
$cacheDir = Join-Path $ProjectRoot "__pycache__"
if (Test-Path $cacheDir) {
    Remove-Item -Recurse -Force $cacheDir
    Write-OK "Cleared __pycache__"
}

# ── Start service ─────────────────────────────────────────────────────────────
Write-Step "Starting service"
& $NssmExe start $ServiceName
Start-Sleep -Seconds 4

$svc = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
if ($svc -and $svc.Status -eq "Running") {
    Write-OK "Service is RUNNING"
} else {
    Write-Host "  WARNING: Service did not start. Check logs:" -ForegroundColor Yellow
    Write-Host "    $(Join-Path $ProjectRoot 'data\engine_history.log')" -ForegroundColor Gray
    exit 1
}

# ── Done ──────────────────────────────────────────────────────────────────────
Write-Host ""
Write-Host "========================================" -ForegroundColor Cyan
Write-Host "  Credentials swapped successfully!" -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan
Write-Host ""
Write-Host "  New API key: $($ApiKey.Substring(0,8))..." -ForegroundColor White
Write-Host "  PEM file:    $KeyPath" -ForegroundColor White
Write-Host "  Env file:    $EnvFile" -ForegroundColor White
Write-Host ""
Write-Host "  Monitor startup:" -ForegroundColor Yellow
Write-Host "    Get-Content '$(Join-Path $ProjectRoot 'data\engine_history.log')' -Tail 30 -Wait" -ForegroundColor Gray
Write-Host ""
