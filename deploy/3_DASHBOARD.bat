@echo off
:: Launches the BTC Bias Engine Live Dashboard
cd /d "%~dp0"
start "" pythonw dashboard.pyw 2>nul
if %errorlevel% neq 0 (
    python dashboard.pyw 2>nul
    if %errorlevel% neq 0 (
        echo.
        echo   Could not launch Dashboard.
        echo   Make sure Python is installed: python.org/downloads
        echo.
        pause
    )
)
