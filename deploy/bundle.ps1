# =============================================================================
# BTC Bias Engine - Bundle Script
# =============================================================================
# Creates a clean, SECRET-FREE single-EXE deployment package.
#
# Usage:
#   .\deploy\bundle.ps1
#
# Output: btc-bias-engine-deploy.zip containing:
#   - BTC_Engine.exe       (single-file, engine + monitor + setup in one)
#   - Setup.bat            (first-run credential wizard)
#   - Run_Engine.bat       (start trading)
#   - Run_Monitor.bat      (live terminal dashboard)
#   - README.txt           (5-minute setup guide)
#   - credentials/         (empty, with kalshi.env.example)
#   - data/                (empty)
#
# No Python install needed on target PC. No secrets. Plug in your own UUID + PEM.
# =============================================================================

param(
    [string]$OutputDir = "C:\Trading\btc-bias-engine\deploy",
    [string]$BundleName = "btc-bias-engine-deploy"
)

$ErrorActionPreference = "Stop"
$SourceDir = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)

Write-Host ""
Write-Host "Bundling BTC Bias Engine for deployment..." -ForegroundColor Cyan
Write-Host "  Source: $SourceDir" -ForegroundColor Gray
Write-Host ""

$DestDir = Join-Path $OutputDir $BundleName

# Keep the existing batch files + README by reusing the dir; but refresh the EXE
$ExePath = Join-Path $SourceDir "dist\BTC_Engine.exe"
if (-not (Test-Path $ExePath)) {
    Write-Host "  ERROR: dist\BTC_Engine.exe not found. Run PyInstaller first:" -ForegroundColor Red
    Write-Host "    python -m PyInstaller BTC_Engine.spec --noconfirm" -ForegroundColor Yellow
    exit 1
}

# Ensure payload dir exists
New-Item -ItemType Directory -Force -Path $DestDir | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $DestDir "credentials") | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $DestDir "data") | Out-Null

# Remove old onedir leftovers
$InternalDir = Join-Path $DestDir "_internal"
if (Test-Path $InternalDir) {
    Remove-Item -Recurse -Force $InternalDir
    Write-Host "  Removed stale _internal/ (onedir leftover)" -ForegroundColor Gray
}

# Copy fresh EXE
Copy-Item -Force $ExePath (Join-Path $DestDir "BTC_Engine.exe")
$exeSize = [math]::Round((Get-Item (Join-Path $DestDir "BTC_Engine.exe")).Length / 1MB, 1)
Write-Host "  Copied BTC_Engine.exe ($exeSize MB)" -ForegroundColor Green

# Ensure credentials has example env (but no real env file)
$EnvExample = Join-Path $DestDir "credentials\kalshi.env.example"
if (-not (Test-Path $EnvExample)) {
    @"
# Rename this file to kalshi.env and fill in your values,
# OR run Setup.bat to have the wizard create it for you.
KALSHI_API_KEY=<your-kalshi-uuid-here>
KALSHI_PRIVATE_KEY_PATH=credentials/kalshi_key.pem
KALSHI_DEMO=false
EXECUTE_TRADES=true
"@ | Set-Content $EnvExample
}

# Security check: no secrets in the bundle
Write-Host ""
Write-Host "  Security check..." -ForegroundColor Yellow
$leaked = $false
$realEnv = Join-Path $DestDir "credentials\kalshi.env"
if (Test-Path $realEnv) {
    Write-Host "  WARNING: credentials\kalshi.env is in the bundle -- removing" -ForegroundColor Red
    Remove-Item -Force $realEnv
}
Get-ChildItem -Path $DestDir -Recurse -Filter "*.pem" -ErrorAction SilentlyContinue | ForEach-Object {
    Write-Host "  WARNING: PEM found, removing: $($_.FullName)" -ForegroundColor Red
    Remove-Item -Force $_.FullName
    $leaked = $true
}
Get-ChildItem -Path $DestDir -Recurse -Filter "*.key" -ErrorAction SilentlyContinue | ForEach-Object {
    Write-Host "  WARNING: key found, removing: $($_.FullName)" -ForegroundColor Red
    Remove-Item -Force $_.FullName
    $leaked = $true
}
# Check trades.db / balance history doesn't ship
$dataDir = Join-Path $DestDir "data"
Get-ChildItem -Path $dataDir -ErrorAction SilentlyContinue | ForEach-Object {
    if ($_.Name -ne ".gitkeep") {
        Remove-Item -Recurse -Force $_.FullName
    }
}
if (-not $leaked) {
    Write-Host "  OK: No secrets detected" -ForegroundColor Green
}

# Create ZIP
$zipPath = Join-Path $OutputDir "$BundleName.zip"
if (Test-Path $zipPath) { Remove-Item $zipPath }
Compress-Archive -Path $DestDir -DestinationPath $zipPath
$zipSize = [math]::Round((Get-Item $zipPath).Length / 1MB, 1)

Write-Host ""
Write-Host "========================================" -ForegroundColor Cyan
Write-Host "  Bundle Complete" -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan
Write-Host ""
Write-Host "  Folder: $DestDir" -ForegroundColor White
Write-Host "  ZIP:    $zipPath ($zipSize MB)" -ForegroundColor White
Write-Host ""
Write-Host "  To deploy to another PC:" -ForegroundColor Yellow
Write-Host "    1. Copy the ZIP to the target PC and extract anywhere" -ForegroundColor Gray
Write-Host "    2. Double-click Setup.bat -- paste Kalshi UUID + .pem path" -ForegroundColor Gray
Write-Host "    3. Double-click Run_Engine.bat  (trades)" -ForegroundColor Gray
Write-Host "    4. Optionally: Run_Monitor.bat  (live dashboard)" -ForegroundColor Gray
Write-Host ""
