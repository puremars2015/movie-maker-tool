@echo off
chcp 65001 >nul
cd /d "%~dp0"
python -m movie_maker_tool gui
if errorlevel 1 pause
