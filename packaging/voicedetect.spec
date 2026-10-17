# PyInstaller 打包配置。在仓库根目录运行（先构建界面：cd frontend && npm run build）：
#   uv run --group build pyinstaller packaging/voicedetect.spec --noconfirm
# Windows 输出 dist/AI盯播助手/：里面的 VoiceDetect.exe 双击就能用，不需要装 Python 或 Node。
# macOS 另外输出 dist/AI盯播助手.app。
# 用文件夹（onedir）而不是单个 exe：单个 exe 每次启动都要先把一百多 MB 解压到临时目录，慢而且容易被杀毒软件拦。

import re
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_dynamic_libs

ROOT = Path(SPECPATH).parent  # noqa: F821  SPECPATH 由 PyInstaller 提供
VERSION = re.search(r'__version__ = "([^"]+)"', (ROOT / "voicedetect" / "__init__.py").read_text("utf-8"))[1]
NAME = "AI 盯播助手"

if not (ROOT / "voicedetect" / "web" / "index.html").is_file():
    raise SystemExit("界面还没有构建：先运行 cd frontend && npm run build")

datas = [
    (str(ROOT / "voicedetect" / "web"), "voicedetect/web"),
    (str(ROOT / "voicedetect" / "assets"), "voicedetect/assets"),
]
# sherpa-onnx 的原生库（含 onnxruntime）在 sherpa_onnx/lib 里，和 .pyd 放在一起才找得到
binaries = collect_dynamic_libs("sherpa_onnx")
hiddenimports = ["sqlalchemy.dialects.sqlite", "anyio._backends._asyncio"]
# pywebview 在 Windows 上只用 WinForms + WebView2，其他平台的界面库不用带
excludes = ["tkinter", "pytest", "_pytest"]
if sys.platform == "win32":
    excludes += [f"webview.platforms.{name}" for name in ("android", "cef", "cocoa", "gtk", "qt")]

a = Analysis(  # noqa: F821
    [str(ROOT / "packaging" / "launcher.py")],
    pathex=[str(ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=excludes,
)
pyz = PYZ(a.pure)  # noqa: F821

version_file = None
if sys.platform == "win32":
    from PyInstaller.utils.win32.versioninfo import (
        FixedFileInfo,
        StringFileInfo,
        StringStruct,
        StringTable,
        VarFileInfo,
        VarStruct,
        VSVersionInfo,
    )

    numbers = tuple(int(part) for part in VERSION.split(".")) + (0,) * (4 - len(VERSION.split(".")))
    # “文件说明”会显示在任务管理器、音量合成器和“只听一个程序”的列表里
    version_file = VSVersionInfo(
        ffi=FixedFileInfo(filevers=numbers, prodvers=numbers),
        kids=[
            StringFileInfo(
                [
                    StringTable(
                        "080404B0",
                        [
                            StringStruct("FileDescription", NAME),
                            StringStruct("ProductName", NAME),
                            StringStruct("FileVersion", VERSION),
                            StringStruct("ProductVersion", VERSION),
                            StringStruct("OriginalFilename", "VoiceDetect.exe"),
                        ],
                    )
                ]
            ),
            VarFileInfo([VarStruct("Translation", [0x0804, 1200])]),
        ],
    )

icon = ROOT / "packaging" / ("icon.icns" if sys.platform == "darwin" else "icon.ico")
exe = EXE(  # noqa: F821
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="VoiceDetect",
    console=False,  # 不弹命令行窗口
    icon=str(icon),
    version=version_file,
)
coll = COLLECT(exe, a.binaries, a.datas, name="AI盯播助手")  # noqa: F821

if sys.platform == "darwin":
    app = BUNDLE(  # noqa: F821
        coll,
        name="AI盯播助手.app",
        icon=str(icon),
        bundle_identifier="io.github.t1ant1an9cn-sketch.voicedetect",
        version=VERSION,
        info_plist={
            "CFBundleName": NAME,
            "CFBundleDisplayName": NAME,
            "CFBundleShortVersionString": VERSION,
            "CFBundleVersion": VERSION,
            "LSMinimumSystemVersion": "11.0",
            "NSHighResolutionCapable": True,
            # 没有这句说明，macOS 不让程序用麦克风
            "NSMicrophoneUsageDescription": "用麦克风采集主播的讲解，实时识别并提醒。",
        },
    )
