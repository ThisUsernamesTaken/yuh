@echo off
REM BTC Bias Engine -- headless trading loop
REM Logs to data\engine_history.log
title BTC Engine (trading)
"%~dp0BTC_Engine.exe" --engine
pause
