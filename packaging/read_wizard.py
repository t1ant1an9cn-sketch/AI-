"""打开 Windows 安装程序的向导（不安装），读出向导窗口里的文字，确认中文显示正常，然后关掉。

GitHub 打包时的静默安装不显示向导，看不出界面文字是不是乱码，所以单独检查一次：第一页应该是
“选择目标位置”，上面写着“安装程序将安装 AI 盯播助手 到下面的文件夹中。”。结果写成 GitHub 的标注。
用法（Windows）：python read_wizard.py <安装程序路径>
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import subprocess
import sys
import time

APP_NAME = "AI 盯播助手"
EXPECTED = [  # 第一页（选择目标位置）上应该出现的文字，来自 ChineseSimplified.isl
    f"安装 - {APP_NAME}",
    "选择目标位置",
    f"安装程序将安装 {APP_NAME} 到下面的文件夹中。",
    "下一步(&N) >",
    "取消",
]
TIMEOUT = 60

user32 = ctypes.WinDLL("user32", use_last_error=True)
ENUM = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
user32.EnumWindows.argtypes = [ENUM, wintypes.LPARAM]
user32.EnumChildWindows.argtypes = [wintypes.HWND, ENUM, wintypes.LPARAM]
user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.IsWindowVisible.argtypes = [wintypes.HWND]
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]


def text(hwnd) -> str:
    length = user32.GetWindowTextLengthW(hwnd)
    buffer = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buffer, length + 1)
    return buffer.value


def class_name(hwnd) -> str:
    buffer = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, buffer, 256)
    return buffer.value


def windows(parent=None) -> list[int]:
    found: list[int] = []

    @ENUM
    def collect(hwnd, _):
        found.append(hwnd)
        return True

    if parent is None:
        user32.EnumWindows(collect, 0)
    else:
        user32.EnumChildWindows(parent, collect, 0)
    return found


def escape(value: str) -> str:
    return value.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def main(installer: str) -> int:
    setup = subprocess.Popen([installer])
    wizard = None
    deadline = time.monotonic() + TIMEOUT
    while wizard is None and time.monotonic() < deadline:
        time.sleep(1)
        wizard = next(
            (hwnd for hwnd in windows() if user32.IsWindowVisible(hwnd) and class_name(hwnd) == "TWizardForm"),
            None,
        )
    try:
        if wizard is None:
            visible = [f"{text(hwnd)} [{class_name(hwnd)}]" for hwnd in windows() if user32.IsWindowVisible(hwnd)]
            detail = f"{TIMEOUT} 秒内没有找到向导窗口。可见窗口：{'；'.join(visible)}"
            print(f"::error title=没有出现安装向导::{escape(detail)}")
            return 1
        time.sleep(2)  # 等第一页画完
        texts = [text(wizard)]
        for hwnd in windows(wizard):
            value = text(hwnd).strip()
            if value and user32.IsWindowVisible(hwnd) and value not in texts:
                texts.append(value)
        missing = [item for item in EXPECTED if not any(item in value for value in texts)]
        shown = "\n".join(texts)
        if missing:
            detail = f"缺少：{'、'.join(missing)}\n实际显示：\n{shown}"
            print(f"::error title=安装向导的文字不对::{escape(detail)}")
            return 1
        print(f"::notice title=安装向导::{escape(shown)}")
        return 0
    finally:
        if wizard is not None:
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(wizard, ctypes.byref(pid))
            subprocess.run(["taskkill", "/PID", str(pid.value), "/T", "/F"], capture_output=True)
        setup.kill()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1]))
