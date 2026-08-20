@echo off
REM Windows build script: packages gui_launcher.py into Seedance.exe.
REM Requires Python 3.9+ (check "Add python.exe to PATH" during install).
REM Usage: double-click this file, or run build_windows.bat from a command prompt.

cd /d "%~dp0"

python -m venv .buildvenv
call .buildvenv\Scripts\activate.bat
pip install -q --upgrade pip
pip install -q pyinstaller requests Pillow

pyinstaller --noconfirm --windowed --onefile --name "Seedance" gui_launcher.py

echo.
echo Done: dist\Seedance.exe
echo Put .env (or copy .env.example and fill in your keys) next to Seedance.exe, then double-click to run.
pause
