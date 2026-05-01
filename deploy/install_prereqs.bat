@echo off
:: =========================================
::  DOUBLE CLICK ME FIRST (I install stuff)
:: =========================================

:: Self-elevate to admin if not already
net session >nul 2>&1
if %errorlevel% neq 0 (
    echo Requesting admin access...
    powershell -Command "Start-Process cmd -ArgumentList '/c \"%~f0\"' -Verb RunAs"
    exit /b
)

:: Keep the window open so user can see output
cd /d "%~dp0"

echo.
echo   ______  _______ _______    ______  _____ _______ _______
echo   ^|_____] ^|______    ^|       ^|_____] ^|   ^| ^|_____| ^|______
echo   ^|_____] ^|______    ^|       ^|_____] ^|___^| ^|     ^| ______|
echo.
echo   money printer go brrrr (installing prerequisites)
echo.
echo   sit back, this takes like 2 minutes
echo.

:: Check if Python is already installed
python --version >nul 2>&1
if %errorlevel% equ 0 (
    echo [OK] Python found:
    python --version
    goto :check_nssm
)

echo [!] Python not found. Installing Python 3.12...
echo.

:: Download Python installer
echo Downloading Python...
powershell -Command "Invoke-WebRequest -Uri 'https://www.python.org/ftp/python/3.12.8/python-3.12.8-amd64.exe' -OutFile '%TEMP%\python_installer.exe'"

if not exist "%TEMP%\python_installer.exe" (
    echo [ERROR] Failed to download Python.
    echo Please install Python 3.12+ manually from https://python.org
    pause
    exit /b 1
)

:: Install Python silently with PATH
echo Installing Python (this may take a minute)...
"%TEMP%\python_installer.exe" /quiet InstallAllUsers=1 PrependPath=1 Include_pip=1

:: Refresh PATH
set "PATH=%PATH%;C:\Program Files\Python312;C:\Program Files\Python312\Scripts"

:: Verify
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo [ERROR] Python installation may require a restart.
    echo Please restart your computer and run this script again.
    pause
    exit /b 1
)
echo [OK] Python installed successfully.
echo.

:check_nssm
:: Check if NSSM is available
nssm version >nul 2>&1
if %errorlevel% equ 0 (
    echo [OK] NSSM found.
    goto :install_pip
)

echo [!] NSSM not found. Installing via winget...
winget install --id nssm.nssm --accept-package-agreements --accept-source-agreements >nul 2>&1

:: Refresh PATH
for /f "tokens=2*" %%a in ('reg query "HKLM\SYSTEM\CurrentControlSet\Control\Session Manager\Environment" /v Path 2^>nul') do set "PATH=%%b;%PATH%"

nssm version >nul 2>&1
if %errorlevel% neq 0 (
    echo [WARNING] NSSM not installed automatically.
    echo Download from https://nssm.cc/download and add to PATH.
    echo The engine can still run manually without NSSM.
)
echo.

:install_pip
:: Install Python dependencies
echo Installing Python packages...
python -m pip install --upgrade pip --quiet
python -m pip install aiohttp aiosqlite cryptography psutil websockets --quiet

if %errorlevel% equ 0 (
    echo [OK] All packages installed.
) else (
    echo [WARNING] Some packages may have failed. The setup will retry.
)
echo.

:: Create credentials directory
echo Creating directories...
if not exist "%~dp0credentials" mkdir "%~dp0credentials"
echo [OK] Directories ready.
echo.

:: Files stay where they are — no copy needed. Run from extracted folder.
if not exist "%~dp0data" mkdir "%~dp0data"
if not exist "%~dp0user_config.py" (
    if exist "%~dp0user_config.example.py" (
        copy "%~dp0user_config.example.py" "%~dp0user_config.py" >nul
        echo [OK] Created user_config.py from example.
    )
)
echo [OK] Directory ready.
echo.

:: Associate .pyw files with Python (some fresh installs miss this)
echo Registering .pyw file association...
assoc .pyw=Python.NoConFile >nul 2>&1
ftype Python.NoConFile=pythonw.exe "%%1" %%* >nul 2>&1
echo [OK] .pyw files registered.
echo.

echo.
echo   ===================================
echo    NICE. everything installed.
echo    launching the control panel now...
echo   ===================================
echo.

:: Launch the control panel from wherever the ZIP was extracted
start "" pythonw "%~dp0CONTROL_PANEL.pyw"

if %errorlevel% neq 0 (
    echo [!] control panel didn't open. try this:
    echo     double click 2_CONTROL_PANEL.bat
)

pause
