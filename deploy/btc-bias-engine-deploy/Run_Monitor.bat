@echo off
REM BTC Bias Engine -- live terminal monitor (read-only)
REM Displays real-time pressure, position, P&L, and logs.
REM Run this in a second window alongside Run_Engine.bat.
title BTC Monitor
mode con: cols=130 lines=50
"%~dp0BTC_Engine.exe" --monitor
pause
