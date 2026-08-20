@echo off
REM 在 Windows 上打包成 Seedance.exe，雙擊即可開啟 GUI。
REM 需先安裝 Python 3.9+（安裝時勾選 "Add python.exe to PATH"）。
REM 用法：雙擊本檔案，或在命令提示字元執行 build_windows.bat

cd /d "%~dp0"

python -m venv .buildvenv
call .buildvenv\Scripts\activate.bat
pip install -q --upgrade pip
pip install -q pyinstaller requests Pillow

pyinstaller --noconfirm --windowed --onefile --name "Seedance" gui_launcher.py

echo.
echo 完成：dist\Seedance.exe
echo 把 .env（或把 .env.example 改名填金鑰）跟 Seedance.exe 放在同一個資料夾，雙擊即可使用。
pause
