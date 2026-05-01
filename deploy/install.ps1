# =============================================================================
# BTC Bias Engine — Automated Installer for New Machines
# =============================================================================
# Run this script in an ELEVATED (Admin) PowerShell prompt:
#
#   Right-click PowerShell -> "Run as Administrator"
#   cd C:\Trading\btc-bias-engine
#   .\install.ps1
#
# This script will:
#   1. Check Python 3.11+ is installed
#   2. Install pip dependencies
#   3. Create the data/ directory
#   4. Prompt you for Kalshi API credentials
#   5. Install NSSM (service manager)
#   6. Register BTCBiasEngine as a Windows service
#   7. Configure environment variables on the service
#   8. Start the engine
#
# NO SECRETS ARE STORED IN THIS FILE.
# =============================================================================

param(
    [string]$InstallDir = "C:\Trading\btc-bias-engine",
    [switch]$DemoMode,
    [switch]$SkipService
)

$ErrorActionPreference = "Stop"

Write-Host ""
Write-Host "========================================" -ForegroundColor Cyan
Write-Host "  BTC Bias Engine — Installer" -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan
Write-Host ""

# ── Step 1: Verify Python ──────────────────────────────────────────────────
Write-Host "[1/7] Checking Python..." -ForegroundColor Yellow
try {
    $pyVersion = & python --version 2>&1
    if ($pyVersion -match "Python (\d+)\.(\d+)") {
        $major = [int]$Matches[1]
        $minor = [int]$Matches[2]
        if ($major -lt 3 -or ($major -eq 3 -and $minor -lt 11)) {
            Write-Host "  ERROR: Python 3.11+ required, found $pyVersion" -ForegroundColor Red
            exit 1
        }
        Write-Host "  OK: $pyVersion" -ForegroundColor Green
    }
} catch {
    Write-Host "  ERROR: Python not found. Install Python 3.11+ from python.org" -ForegroundColor Red
    exit 1
}

# ── Step 2: Install dependencies ───────────────────────────────────────────
Write-Host "[2/7] Installing Python dependencies..." -ForegroundColor Yellow
$reqFile = Join-Path $InstallDir "requirements.txt"
if (Test-Path $reqFile) {
    & python -m pip install -r $reqFile --quiet
    Write-Host "  OK: Dependencies installed" -ForegroundColor Green
} else {
    Write-Host "  WARNING: requirements.txt not found at $reqFile" -ForegroundColor Yellow
    Write-Host "  Installing manually: aiohttp aiosqlite cryptography psutil websockets"
    & python -m pip install aiohttp aiosqlite cryptography psutil websockets --quiet
}

# ── Step 3: Create data directory ──────────────────────────────────────────
Write-Host "[3/7] Creating data directory..." -ForegroundColor Yellow
$dataDir = Join-Path $InstallDir "data"
if (-not (Test-Path $dataDir)) {
    New-Item -ItemType Directory -Path $dataDir | Out-Null
}
Write-Host "  OK: $dataDir" -ForegroundColor Green

# ── Step 4: Collect Kalshi credentials ─────────────────────────────────────
Write-Host "[4/7] Kalshi API Configuration" -ForegroundColor Yellow
Write-Host ""
Write-Host "  You need your Kalshi API key and RSA private key." -ForegroundColor White
Write-Host "  Get these from: https://kalshi.com/account/api" -ForegroundColor Gray
Write-Host ""

$apiKey = Read-Host "  Enter your Kalshi API Key (UUID)"
if ([string]::IsNullOrWhiteSpace($apiKey)) {
    Write-Host "  ERROR: API key is required." -ForegroundColor Red
    exit 1
}

$keyPath = Read-Host "  Enter path to your RSA private key .pem file"
if ([string]::IsNullOrWhiteSpace($keyPath) -or -not (Test-Path $keyPath)) {
    Write-Host "  ERROR: Private key file not found at '$keyPath'" -ForegroundColor Red
    exit 1
}
Write-Host "  OK: Credentials collected" -ForegroundColor Green

# ── Write kalshi.env for health-check tooling ─────────────────────────────
$credDir = "C:\Trading\credentials"
if (-not (Test-Path $credDir)) { New-Item -ItemType Directory -Path $credDir | Out-Null }
$envFile = Join-Path $credDir "kalshi.env"

# ── Step 5: Create user_config.py from example ────────────────────────────
Write-Host "[5/7] Setting up user_config.py..." -ForegroundColor Yellow
$userConfig = Join-Path $InstallDir "user_config.py"
$exampleConfig = Join-Path $InstallDir "user_config.example.py"
if (-not (Test-Path $userConfig)) {
    if (Test-Path $exampleConfig) {
        Copy-Item $exampleConfig $userConfig
        # Update ENGINE_DIR to match install location
        (Get-Content $userConfig) -replace 'ENGINE_DIR = r".*"', "ENGINE_DIR = r`"$InstallDir`"" | Set-Content $userConfig
        Write-Host "  OK: Created from example. Edit later to tune sizing/risk." -ForegroundColor Green
    } else {
        Write-Host "  WARNING: No example config found. You'll need to create user_config.py manually." -ForegroundColor Yellow
    }
} else {
    Write-Host "  OK: user_config.py already exists (keeping existing)" -ForegroundColor Green
}

# ── Step 6: Trading mode ──────────────────────────────────────────────────
Write-Host ""
if ($DemoMode) {
    $demo = "true"
    $execute = "false"
    Write-Host "  Mode: DEMO (paper trading, no real money)" -ForegroundColor Yellow
} else {
    Write-Host "  Select trading mode:" -ForegroundColor White
    Write-Host "    [1] DEMO  — paper trading, no real money (recommended first)" -ForegroundColor Gray
    Write-Host "    [2] LIVE  — real money, signal only (log signals, don't execute)" -ForegroundColor Gray
    Write-Host "    [3] LIVE  — real money, EXECUTE trades" -ForegroundColor Gray
    $modeChoice = Read-Host "  Enter choice (1/2/3)"
    switch ($modeChoice) {
        "1" { $demo = "true";  $execute = "false"; Write-Host "  -> DEMO mode" -ForegroundColor Yellow }
        "2" { $demo = "false"; $execute = "false"; Write-Host "  -> LIVE signal-only" -ForegroundColor Yellow }
        "3" { $demo = "false"; $execute = "true";  Write-Host "  -> LIVE EXECUTE" -ForegroundColor Red }
        default { $demo = "true"; $execute = "false"; Write-Host "  -> Defaulting to DEMO" -ForegroundColor Yellow }
    }
}

# ── Step 7: Install as Windows service via NSSM ───────────────────────────
if ($SkipService) {
    Write-Host "[6/7] Skipping service install (manual mode)" -ForegroundColor Yellow
    Write-Host "[7/7] Skipped" -ForegroundColor Yellow
} else {
    Write-Host "[6/7] Installing NSSM (service manager)..." -ForegroundColor Yellow

    # Check if nssm is available
    $nssmPath = Get-Command nssm -ErrorAction SilentlyContinue
    if (-not $nssmPath) {
        Write-Host "  NSSM not found. Attempting install via winget..." -ForegroundColor Yellow
        try {
            winget install --id nssm.nssm --accept-package-agreements --accept-source-agreements 2>&1 | Out-Null
            # Refresh PATH
            $env:Path = [System.Environment]::GetEnvironmentVariable("Path","Machine") + ";" + [System.Environment]::GetEnvironmentVariable("Path","User")
            $nssmPath = Get-Command nssm -ErrorAction SilentlyContinue
        } catch {}

        if (-not $nssmPath) {
            Write-Host "  ERROR: Could not install NSSM automatically." -ForegroundColor Red
            Write-Host "  Download manually from https://nssm.cc/download" -ForegroundColor Yellow
            Write-Host "  Place nssm.exe in your PATH and re-run this script." -ForegroundColor Yellow
            Write-Host ""
            Write-Host "  Alternatively, run manually:" -ForegroundColor White
            Write-Host "    cd $InstallDir" -ForegroundColor Gray
            Write-Host "    python main.py" -ForegroundColor Gray
            exit 1
        }
    }
    Write-Host "  OK: NSSM available" -ForegroundColor Green

    Write-Host "[7/7] Registering BTCBiasEngine service..." -ForegroundColor Yellow

    $serviceName = "BTCBiasEngine"
    $pythonPath = (Get-Command python).Source
    $mainPy = Join-Path $InstallDir "main.py"

    # Remove existing service if present
    $existing = Get-Service -Name $serviceName -ErrorAction SilentlyContinue
    if ($existing) {
        Write-Host "  Removing existing service..." -ForegroundColor Yellow
        nssm stop $serviceName 2>&1 | Out-Null
        nssm remove $serviceName confirm 2>&1 | Out-Null
        Start-Sleep -Seconds 2
    }

    # Install service
    nssm install $serviceName $pythonPath $mainPy
    nssm set $serviceName AppDirectory $InstallDir
    nssm set $serviceName DisplayName "BTC Bias Engine"
    nssm set $serviceName Description "Cross-venue BTC 15m binary contract trading engine"
    nssm set $serviceName Start SERVICE_AUTO_START

    # Set environment variables on the service
    $envString = "KALSHI_API_KEY=$apiKey"
    $envString += "`nKALSHI_PRIVATE_KEY_PATH=$keyPath"
    $envString += "`nKALSHI_DEMO=$demo"
    $envString += "`nEXECUTE_TRADES=$execute"
    $envString += "`nDAILY_LOSS_LIMIT=15"
    $envString += "`nPYTHONUNBUFFERED=1"
    nssm set $serviceName AppEnvironmentExtra $envString

    # Write kalshi.env so health-check tooling can read credentials without hardcoding
    $keyPathFwd = $keyPath.Replace('\', '/')
@"
# Kalshi API credentials — used by health-check tooling and scripts
# DO NOT commit this file or share it. Keep it alongside your private key.
#
# To update for a new account, run:
#   powershell -ExecutionPolicy Bypass -File $InstallDir\deploy\swap-credentials.ps1
#
KALSHI_API_KEY=$apiKey
KALSHI_PRIVATE_KEY_PATH=$keyPathFwd
KALSHI_DEMO=$demo
EXECUTE_TRADES=$execute
"@ | Set-Content $envFile -Encoding UTF8
    Write-Host "  OK: Credentials written to $envFile" -ForegroundColor Green

    # Stdout/stderr to log files
    $stdoutLog = Join-Path $dataDir "service_stdout.log"
    $stderrLog = Join-Path $dataDir "service_stderr.log"
    nssm set $serviceName AppStdout $stdoutLog
    nssm set $serviceName AppStderr $stderrLog
    nssm set $serviceName AppStdoutCreationDisposition 4  # Append
    nssm set $serviceName AppStderrCreationDisposition 4  # Append

    # Auto-restart on failure
    nssm set $serviceName AppExit Default Restart
    nssm set $serviceName AppRestartDelay 5000  # 5 second delay before restart

    Write-Host "  OK: Service registered" -ForegroundColor Green

    # Start it
    Write-Host ""
    $startNow = Read-Host "  Start the engine now? (y/n)"
    if ($startNow -eq "y") {
        nssm start $serviceName
        Start-Sleep -Seconds 3
        $svc = Get-Service -Name $serviceName -ErrorAction SilentlyContinue
        if ($svc -and $svc.Status -eq "Running") {
            Write-Host "  OK: Engine is RUNNING" -ForegroundColor Green
        } else {
            Write-Host "  WARNING: Service may not have started. Check logs at:" -ForegroundColor Yellow
            Write-Host "    $stdoutLog" -ForegroundColor Gray
            Write-Host "    $(Join-Path $dataDir 'engine_history.log')" -ForegroundColor Gray
        }
    }
}

# ── Done ───────────────────────────────────────────────────────────────────
Write-Host ""
Write-Host "========================================" -ForegroundColor Cyan
Write-Host "  Installation Complete!" -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan
Write-Host ""
Write-Host "  Engine directory:  $InstallDir" -ForegroundColor White
Write-Host "  Logs:              $(Join-Path $dataDir 'engine_history.log')" -ForegroundColor White
Write-Host "  Config:            $(Join-Path $InstallDir 'user_config.py')" -ForegroundColor White
Write-Host ""
Write-Host "  Useful commands:" -ForegroundColor Yellow
Write-Host "    nssm status BTCBiasEngine          # Check if running" -ForegroundColor Gray
Write-Host "    nssm restart BTCBiasEngine          # Restart engine" -ForegroundColor Gray
Write-Host "    nssm stop BTCBiasEngine             # Stop engine" -ForegroundColor Gray
Write-Host "    Get-Content '$dataDir\engine_history.log' -Tail 50  # View logs" -ForegroundColor Gray
Write-Host ""
Write-Host "  First run takes ~10 minutes to score Polymarket wallets." -ForegroundColor Yellow
Write-Host "  Subsequent restarts load from cache (~2 seconds)." -ForegroundColor Yellow
Write-Host ""
Write-Host "  Edit user_config.py to adjust position sizing and risk." -ForegroundColor White
Write-Host ""
