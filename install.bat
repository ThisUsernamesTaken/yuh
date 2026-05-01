@echo off
:: ============================================================================
:: BTC Bias Engine - One-click installer (calls scripts/setup.ps1)
::
:: What this does (so you don't have to think about PowerShell ceremony):
::   1. Elevates to Administrator (NSSM service install needs admin)
::   2. Bypasses PowerShell execution policy for this run only
::   3. Runs scripts/setup.ps1 from the engine root directory
::
:: Usage: just double-click install.bat. Click "Yes" on the UAC prompt.
::
:: Prerequisites the script will check for and install if missing:
::   - Python 3.11+ (must be installed manually first; download from python.org)
::   - NSSM (auto-downloaded if absent)
::
:: You will need:
::   - Your Kalshi API key UUID
::   - Your Kalshi private key (.pem file) somewhere on disk
::   The script prompts for both interactively.
:: ============================================================================

setlocal enabledelayedexpansion

:: Capture this directory (where the .bat lives)
set "ENGINE_ROOT=%~dp0"
set "SETUP_PS1=%ENGINE_ROOT%scripts\setup.ps1"

if not exist "%SETUP_PS1%" (
    echo.
    echo  ERROR: setup.ps1 not found at:
    echo    %SETUP_PS1%
    echo.
    echo  This installer must live in the engine's root directory,
    echo  alongside the scripts/ folder.
    echo.
    pause
    exit /b 1
)

:: Check for admin rights; re-launch elevated if not
net session >nul 2>&1
if %errorlevel% neq 0 (
    echo.
    echo  Requesting Administrator privileges...
    echo  Click "Yes" on the UAC prompt.
    echo.
    powershell -NoProfile -Command "Start-Process -Verb RunAs -FilePath '%~f0'"
    exit /b
)

:: We're admin. Run setup.ps1 with execution-policy bypass.
echo.
echo ============================================================
echo   BTC Bias Engine - Installation
echo   Running setup.ps1 with admin + execution policy bypass
echo ============================================================
echo.

powershell -NoProfile -ExecutionPolicy Bypass -File "%SETUP_PS1%"

set "EXIT_CODE=%errorlevel%"

echo.
echo ============================================================
if %EXIT_CODE% equ 0 (
    echo   Installation completed successfully.
    echo.
    echo   The BTCBiasEngine service is now installed and will
    echo   start automatically when Windows boots.
    echo.
    echo   Useful commands ^(open a normal PowerShell window^):
    echo     nssm status BTCBiasEngine
    echo     Get-Content data\engine_history.log -Tail 50 -Wait
) else (
    echo   Installation FAILED with exit code %EXIT_CODE%.
    echo   Scroll up to see what went wrong.
)
echo ============================================================
echo.

pause
exit /b %EXIT_CODE%
