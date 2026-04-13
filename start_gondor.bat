@echo off
title Gondor Audio System
echo ================================
echo    Gondor Audio System
echo ================================
echo.
echo Starting audio streaming system...
echo Auto-restart enabled for maximum reliability
echo Press Ctrl+C to stop the system completely
echo.

cd /d "%~dp0"
python stealth.py

echo.
echo Audio system has stopped.
pause
