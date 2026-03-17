<#
.SYNOPSIS
    Re-deploys the BTCBiasEngine service after a code update.

.DESCRIPTION
    Stops the service, reinstalls it (preserving existing secrets from the
    registry), then restarts it.  Run this after pulling new code.

    Does NOT touch the venv or pip packages. Run setup.ps1 instead if
    requirements.txt changed.

.PARAMETER ServiceName
    Windows service name. Default: BTCBiasEngine

.EXAMPLE
    # After git pull
    git pull
    .\scripts\reinstall_service.ps1
#>

[CmdletBinding()]
param(
    [string]$ServiceName = "BTCBiasEngine"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent $PSScriptRoot

function Write-Step([string]$msg) { Write-Host "`n== $msg ==" -ForegroundColor Cyan }
function Write-OK([string]$msg)   { Write-Host "  OK  $msg" -ForegroundColor Green }
function Write-Fail([string]$msg) { Write-Host "  FAIL  $msg" -ForegroundColor Red; exit 1 }

# ── Locate NSSM ───────────────────────────────────────────────────────────────

$NssmExe = $null
try { $NssmExe = (Get-Command "nssm" -ErrorAction SilentlyContinue).Source } catch { }
if (-not $NssmExe) {
    $wingetNssm = Get-ChildItem "$env:LOCALAPPDATA\Microsoft\WinGet\Packages" -Recurse -Filter "nssm.exe" -ErrorAction SilentlyContinue |
                  Where-Object { $_.FullName -match "win64" } | Select-Object -First 1
    if ($wingetNssm) { $NssmExe = $wingetNssm.FullName }
}
if (-not $NssmExe) {
    $localNssm = Join-Path $ProjectRoot "tools\nssm.exe"
    if (Test-Path $localNssm) { $NssmExe = $localNssm }
}
if (-not $NssmExe) { Write-Fail "NSSM not found. Run setup.ps1 first." }

# ── Read existing secrets from registry ──────────────────────────────────────

Write-Step "Reading existing service configuration"

$regPath = "HKLM:\SYSTEM\CurrentControlSet\Services\$ServiceName\Parameters"
if (-not (Test-Path $regPath)) { Write-Fail "Service '$ServiceName' not found. Run setup.ps1 first." }

$params = Get-ItemProperty $regPath
$envExtra = $params.AppEnvironmentExtra
Write-OK "Existing config found"

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

# ── Reinstall with updated code paths ─────────────────────────────────────────

Write-Step "Reinstalling service"

$VenvPython = Join-Path $ProjectRoot "venv\Scripts\python.exe"
$DataDir    = Join-Path $ProjectRoot "data"
$LogFile    = Join-Path $DataDir "engine.log"

if (-not (Test-Path $VenvPython)) { Write-Fail "venv not found at $VenvPython. Run setup.ps1 first." }

& $NssmExe remove $ServiceName confirm | Out-Null
Start-Sleep -Seconds 1

& $NssmExe install $ServiceName $VenvPython "$ProjectRoot\main.py"
& $NssmExe set $ServiceName AppDirectory         $ProjectRoot
& $NssmExe set $ServiceName AppStdout            $LogFile
& $NssmExe set $ServiceName AppStderr            $LogFile
& $NssmExe set $ServiceName AppRotateFiles       1
& $NssmExe set $ServiceName AppRotateBytes       10485760
& $NssmExe set $ServiceName AppRestartDelay      5000
& $NssmExe set $ServiceName Start                SERVICE_AUTO_START
& $NssmExe set $ServiceName DisplayName          "BTC Bias Engine"
& $NssmExe set $ServiceName Description          "Multi-timeframe BTC binary options engine (Kalshi)"
& $NssmExe set $ServiceName AppEnvironmentExtra  $envExtra

Write-OK "Service reinstalled (secrets preserved)"

# ── Start ─────────────────────────────────────────────────────────────────────

Write-Step "Starting service"

& $NssmExe start $ServiceName
Start-Sleep -Seconds 4

$svc = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
if ($svc -and $svc.Status -eq "Running") {
    Write-OK "Service is RUNNING"
} else {
    Write-Host "  Service did not start. Check: $LogFile" -ForegroundColor Yellow
}

Write-Host ""
Write-Host "  Get-Content '$LogFile' -Wait -Tail 50" -ForegroundColor White
Write-Host ""
