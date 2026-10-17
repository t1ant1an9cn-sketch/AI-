"""打包后的程序从这里启动（PyInstaller 的入口脚本）。和 `uv run voicedetect` 走同一个 main。"""

from voicedetect.__main__ import main

if __name__ == "__main__":
    raise SystemExit(main())
