"""打包執行檔的進入點：雙擊直接開圖形介面，不用打指令。"""
import sys

from movie_maker_tool.cli import main

if __name__ == "__main__":
    sys.exit(main(["gui"]))
