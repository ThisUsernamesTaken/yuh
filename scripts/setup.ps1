<#
.SYNOPSIS
    Full deployment setup for BTC Bias Engine on a fresh Windows EC2 instance.

.DESCRIPTION
    - Creates Python venv and installs runtime dependencies
    - Creates the data/ directory
    - Downloads NSSM if not present
    - Installs and starts the BTCBiasEngine Windows service

.PARAMETER ApiKey
    Kalshi API key ID (UUID). If omitted, the script will prompt.

.PARAMETER KeyPath
    Path to the Kalshi RSA private key PEM file. If omitted, the script will prompt.

.PARAMETER ServiceName
    Windows service name. Default: BTCBiasEngine

.PARAMETER DailyLossLimit
    Hard stop for the day in dollars. Default: 500

.PARAMETER SkipService
    Only set up the environment; do not install or start the service.

.EXAMPLE
    # Interactive — prompts for secrets
    .\scripts\setup.ps1

    # Automated (CI / user-data script)
    .\scripts\setup.ps1 -ApiKey "uuid-here" -KeyPath "C:\secrets\kalshi.pem"

.NOTES
    Prerequisites (install these manually before running this script):
        Python 3.11+  https://www.python.org/downloads/windows/
                      Check "Add python.exe to PATH" during install.
        Git           https://git-scm.com/download/win  (if cloning the repo)

    Outbound network required:
        TCP 443   api.elections.kalshi.com
        TCP 9443  stream.binance.us (Binance WebSocket)
        Ensure the EC2 security group allows these outbound connections.
#>

[CmdletBinding()]
param(
    [string]$ApiKey        = "",
    [string]$KeyPath       = "",
    [string]$ServiceName   = "BTCBiasEngine",
    [double]$DailyLossLimit = 500.0,
    [switch]$SkipService
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

# ── Helpers ───────────────────────────────────────────────────────────────────

function Write-Step([string]$msg) {
    Write-Host "`n== $msg ==" -ForegroundColor Cyan
}

function Write-OK([string]$msg) {
    Write-Host "  OK  $msg" -ForegroundColor Green
}

function Write-Fail([string]$msg) {
    Write-Host "  FAIL  $msg" -ForegroundColor Red
    exit 1
}

function Prompt-Secret([string]$prompt) {
    $val = Read-Host $prompt
    if ([string]::IsNullOrWhiteSpace($val)) {
        Write-Fail "Value required."
    }
    return $val.Trim()
}

# ── Locate project root (one level above this script) ─────────────────────────

$ProjectRoot = Split-Path -Parent $PSScriptRoot
Push-Location $ProjectRoot

Write-Host ""
Write-Host "============================================================" -ForegroundColor White
Write-Host "  BTC Bias Engine — Deployment Setup" -ForegroundColor White
Write-Host "  Project: $ProjectRoot" -ForegroundColor White
Write-Host "============================================================" -ForegroundColor White

# ── 1. Python ─────────────────────────────────────────────────────────────────

Write-Step "Checking Python"

$PythonExe = $null
foreach ($candidate in @("python", "python3", "py")) {
    try {
        $ver = & $candidate --version 2>&1
        if ($ver -match "Python (\d+)\.(\d+)") {
            $major = [int]$Matches[1]; $minor = [int]$Matches[2]
            if ($major -ge 3 -and $minor -ge 11) {
                $PythonExe = (Get-Command $candidate).Source
                Write-OK "$ver at $PythonExe"
                break
            } else {
                Write-Host "  Found $ver but need 3.11+, skipping." -ForegroundColor Yellow
            }
        }
    } catch { }
}

if (-not $PythonExe) {
    Write-Fail "Python 3.11+ not found. Install from https://www.python.org/downloads/windows/ and re-run."
}

# ── 2. Virtual environment ────────────────────────────────────────────────────

Write-Step "Setting up virtual environment"

$VenvDir = Join-Path $ProjectRoot "venv"
if (-not (Test-Path "$VenvDir\Scripts\python.exe")) {
    Write-Host "  Creating venv..."
    & $PythonExe -m venv $VenvDir
    Write-OK "venv created at $VenvDir"
} else {
    Write-OK "venv already exists"
}

$VenvPython = "$VenvDir\Scripts\python.exe"
$VenvPip    = "$VenvDir\Scripts\pip.exe"

# ── 3. Dependencies ───────────────────────────────────────────────────────────

Write-Step "Installing dependencies"

& $VenvPip install --upgrade pip --quiet
& $VenvPip install -r "$ProjectRoot\requirements.txt"
Write-OK "Dependencies installed"

# ── 4. Data directory ─────────────────────────────────────────────────────────

Write-Step "Creating data directory"

$DataDir = Join-Path $ProjectRoot "data"
New-Item -ItemType Directory -Force -Path $DataDir | Out-Null
Write-OK "data/ ready at $DataDir"

# ── 5. Verify connectivity (non-blocking) ─────────────────────────────────────

Write-Step "Testing outbound connectivity"

foreach ($host_port in @("api.elections.kalshi.com:443", "stream.binance.us:9443")) {
    $parts = $host_port.Split(":")
    $h = $parts[0]; $p = [int]$parts[1]
    try {
        $tcp = New-Object System.Net.Sockets.TcpClient
        $conn = $tcp.BeginConnect($h, $p, $null, $null)
        $ok = $conn.AsyncWaitHandle.WaitOne(3000, $false)
        $tcp.Close()
        if ($ok) { Write-OK "$host_port reachable" }
        else      { Write-Host "  WARN  $host_port timed out — check EC2 security group outbound rules" -ForegroundColor Yellow }
    } catch {
        Write-Host "  WARN  $host_port unreachable: $_" -ForegroundColor Yellow
    }
}

# ── 6. Secrets ────────────────────────────────────────────────────────────────

if (-not $SkipService) {

    Write-Step "Secrets"

    if ([string]::IsNullOrWhiteSpace($ApiKey)) {
        $ApiKey = Prompt-Secret "  Kalshi API key ID (UUID)"
    } else {
        Write-OK "API key provided via parameter"
    }

    if ([string]::IsNullOrWhiteSpace($KeyPath)) {
        $KeyPath = Prompt-Secret "  Path to Kalshi private key PEM (e.g. C:\secrets\kalshi.pem)"
    }

    $KeyPath = $KeyPath.Trim('"').Trim("'")
    if (-not (Test-Path $KeyPath)) {
        Write-Fail "Private key not found at: $KeyPath"
    }
    Write-OK "Private key found at $KeyPath"

    # ── 7. NSSM ───────────────────────────────────────────────────────────────

    Write-Step "Locating NSSM"

    $NssmExe = $null

    # Try PATH first
    try {
        $NssmExe = (Get-Command "nssm" -ErrorAction SilentlyContinue).Source
    } catch { }

    # Try winget install location
    if (-not $NssmExe) {
        $wingetNssm = Get-ChildItem "$env:LOCALAPPDATA\Microsoft\WinGet\Packages" -Recurse -Filter "nssm.exe" -ErrorAction SilentlyContinue |
                      Where-Object { $_.FullName -match "win64" } |
                      Select-Object -First 1
        if ($wingetNssm) { $NssmExe = $wingetNssm.FullName }
    }

    # Download if not found
    if (-not $NssmExe) {
        Write-Host "  NSSM not found — downloading..."
        $NssmDir  = Join-Path $ProjectRoot "tools"
        $NssmZip  = Join-Path $NssmDir "nssm.zip"
        $NssmExe  = Join-Path $NssmDir "nssm.exe"
        New-Item -ItemType Directory -Force -Path $NssmDir | Out-Null

        $url = "https://nssm.cc/release/nssm-2.24.zip"
        try {
            Invoke-WebRequest -Uri $url -OutFile $NssmZip -UseBasicParsing
            Expand-Archive -Path $NssmZip -DestinationPath $NssmDir -Force
            $extracted = Get-ChildItem $NssmDir -Recurse -Filter "nssm.exe" |
                         Where-Object { $_.FullName -match "win64" } |
                         Select-Object -First 1
            if (-not $extracted) { Write-Fail "Could not find nssm.exe in downloaded archive." }
            Copy-Item $extracted.FullName $NssmExe -Force
            Remove-Item $NssmZip -Force
            Write-OK "NSSM downloaded to $NssmExe"
        } catch {
            Write-Fail "Failed to download NSSM: $_`nInstall manually: https://nssm.cc/download"
        }
    } else {
        Write-OK "NSSM at $NssmExe"
    }

    # ── 8. Install service ────────────────────────────────────────────────────

    Write-Step "Installing Windows service: $ServiceName"

    # Remove existing service if present
    $existing = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
    if ($existing) {
        Write-Host "  Stopping and removing existing service..."
        if ($existing.Status -eq "Running") {
            & $NssmExe stop $ServiceName confirm | Out-Null
        }
        & $NssmExe remove $ServiceName confirm | Out-Null
        Start-Sleep -Seconds 2
        Write-OK "Old service removed"
    }

    $LogFile = Join-Path $DataDir "engine.log"

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

    # Secrets via environment — never stored in the service binary, only in the registry
    # key for the service (HKLM\SYSTEM\...\Services\BTCBiasEngine\Parameters\AppEnvironmentExtra)
    $envBlock = "KALSHI_API_KEY=$ApiKey`0KALSHI_PRIVATE_KEY_PATH=$KeyPath`0EXECUTE_TRADES=true`0KALSHI_DEMO=false`0PYTHONUNBUFFERED=1`0DAILY_LOSS_LIMIT=$DailyLossLimit"
    & $NssmExe set $ServiceName AppEnvironmentExtra $envBlock

    Write-OK "Service installed"

    # ── 9. Start ──────────────────────────────────────────────────────────────

    Write-Step "Starting service"

    & $NssmExe start $ServiceName
    Start-Sleep -Seconds 4

    $svc = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
    if ($svc -and $svc.Status -eq "Running") {
        Write-OK "Service is RUNNING"
    } else {
        Write-Host "  Service did not start cleanly. Check log:" -ForegroundColor Yellow
        Write-Host "  $LogFile" -ForegroundColor Yellow
    }
}

# ── Done ──────────────────────────────────────────────────────────────────────

Write-Host ""
Write-Host "============================================================" -ForegroundColor White
Write-Host "  Setup complete." -ForegroundColor Green
Write-Host ""
Write-Host "  Useful commands:" -ForegroundColor White
Write-Host "    View live log  :  Get-Content '$DataDir\engine.log' -Wait -Tail 50"
Write-Host "    Stop service   :  Stop-Service $ServiceName"
Write-Host "    Start service  :  Start-Service $ServiceName"
Write-Host "    Service status :  Get-Service $ServiceName"
Write-Host "    Diagnostics    :  cd '$ProjectRoot'; .\venv\Scripts\python.exe diagnose.py"
Write-Host "============================================================" -ForegroundColor White
Write-Host ""

Pop-Location
