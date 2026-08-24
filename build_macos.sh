#!/usr/bin/env bash
# 在 macOS 上打包成 MovieMakerTool.app，雙擊即可開啟 GUI。
# 用法：
#   chmod +x build_macos.sh && ./build_macos.sh
set -euo pipefail
cd "$(dirname "$0")"

python3 -m venv .buildvenv
source .buildvenv/bin/activate
pip install -q --upgrade pip
pip install -q pyinstaller requests Pillow

pyinstaller --noconfirm --windowed --name "MovieMakerTool" \
  --osx-bundle-identifier com.moviemakertool.app \
  gui_launcher.py

echo
echo "完成：dist/MovieMakerTool.app"
echo "把 .env（或 .env.example 改名填金鑰）跟 MovieMakerTool.app 放在同一個資料夾，雙擊即可使用。"
echo "首次開啟若被 Gatekeeper 擋下（未簽章），在 Finder 對 App 按右鍵 → 開啟 一次即可。"
