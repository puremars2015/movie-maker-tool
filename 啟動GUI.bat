@echo off
chcp 65001 >nul
cd /d "%~dp0"
python -m seedance gui
if errorlevel 1 pause
