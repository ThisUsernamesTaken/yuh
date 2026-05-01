@echo off
:: Launches the BTC Bias Engine Control Panel
cd /d "%~dp0"
start "" pythonw CONTROL_PANEL.pyw 2>nul
if %errorlevel% neq 0 (
    python CONTROL_PANEL.pyw 2>nul
    if %errorlevel% neq 0 (
        echo.
        echo   Could not launch Control Panel.
        echo   Make sure Python is installed: python.org/downloads
        echo   CHECK "Add Python to PATH" during install!
        echo.
        pause
    )
)
