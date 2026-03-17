<#
.SYNOPSIS
    Gracefully stops the BTCBiasEngine Windows service.

.DESCRIPTION
    Waits for any open position to clear (up to -WaitSeconds), then stops
    the service and prints a final snapshot from the trade ledger.

.PARAMETER ServiceName
    Windows service name. Default: BTCBiasEngine

.PARAMETER WaitSeconds
    Max seconds to wait for an open position to settle before forcing stop.
    Default: 120

.PARAMETER Force
    Skip the open-position wait and stop immediately.

.EXAMPLE
    .\scripts\off.ps1
    .\scripts\off.ps1 -Force
    .\scripts\off.ps1 -WaitSeconds 30
#>

[CmdletBinding()]
param(
    [string]$ServiceName = "BTCBiasEngine",
    [int]   $WaitSeconds = 120,
    [switch]$Force
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent $PSScriptRoot
$DbPath      = Join-Path $ProjectRoot "data\trades.db"
$LogFile     = Join-Path $ProjectRoot "data\engine.log"
$VenvPython  = Join-Path $ProjectRoot "venv\Scripts\python.exe"

function Write-Step([string]$msg) { Write-Host "" ; Write-Host "== $msg ==" -ForegroundColor Cyan }
function Write-OK([string]$msg)   { Write-Host "  OK   $msg" -ForegroundColor Green }
function Write-Warn([string]$msg) { Write-Host "  !!   $msg" -ForegroundColor Yellow }

# --- Check service exists ---

Write-Step "Checking service status"

$svc = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
if (-not $svc) {
    Write-Warn "Service '$ServiceName' not found - nothing to stop."
    exit 0
}

if ($svc.Status -ne "Running") {
    Write-OK "Service is already $($svc.Status) - nothing to do."
    exit 0
}

Write-OK "Service is RUNNING"

# --- Wait for open positions to clear ---

if ((-not $Force) -and (Test-Path $DbPath)) {
    Write-Step "Checking for open positions (wait up to $WaitSeconds`s)"

    $elapsed  = 0
    $interval = 5

    while ($elapsed -lt $WaitSeconds) {
        $openCount = & sqlite3 $DbPath "SELECT COUNT(*) FROM kalshi_trades WHERE status='open';" 2>$null
        if ($null -eq $openCount) { $openCount = "0" }

        if ($openCount -eq "0") {
            Write-OK "No open positions - safe to stop."
            break
        }

        Write-Host "  Waiting... open=$openCount  ($elapsed`s / $WaitSeconds`s)" -ForegroundColor DarkYellow
        Start-Sleep -Seconds $interval
        $elapsed += $interval
    }

    if ($elapsed -ge $WaitSeconds) {
        Write-Warn "Timed out waiting for position to clear - stopping anyway."
    }
} elseif ($Force) {
    Write-Warn "Force flag set - skipping open-position check."
}

# --- Stop service ---

Write-Step "Stopping $ServiceName"

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

if ($NssmExe) {
    & $NssmExe stop $ServiceName confirm | Out-Null
} else {
    Stop-Service -Name $ServiceName -Force
}

Start-Sleep -Seconds 3

$svc = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
if ($svc -and $svc.Status -eq "Stopped") {
    Write-OK "Service STOPPED"
} else {
    Write-Warn "Service may still be stopping (status: $($svc.Status))"
}

# --- Final ledger snapshot ---

if (Test-Path $VenvPython) {
    Write-Step "Final ledger snapshot"
    & $VenvPython "$ProjectRoot\ledger.py"
} else {
    Write-Warn "venv not found - skipping ledger snapshot."
}

# --- Tail of log ---

Write-Step "Last 10 log lines"
if (Test-Path $LogFile) {
    Get-Content $LogFile -Tail 10
} else {
    Write-Warn "Log file not found at $LogFile"
}

Write-Host ""
Write-Host "  Engine is OFF." -ForegroundColor Green
Write-Host "  To restart:  Start-Service $ServiceName" -ForegroundColor White
Write-Host ""
