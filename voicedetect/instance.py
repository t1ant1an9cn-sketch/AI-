"""同一个数据库同时只有一个“主实例”。

双击两次图标会开出两个程序，它们共用一个数据库；后开的那个启动时会把前一个正在盯的场次当成上次异常退出留下的，
标成“已中断”。主实例拿着数据库所在文件夹里 running.lock 的文件锁，程序退出时系统自动放开（崩溃也一样）。
"""

from __future__ import annotations

from pathlib import Path
import sys
from typing import IO

_held: dict[Path, IO[bytes]] = {}


def acquire(folder: Path) -> bool:
    """拿到锁返回 True；别的进程拿着时返回 False。同一个进程再次调用仍返回 True。"""
    folder.mkdir(parents=True, exist_ok=True)
    path = (folder / "running.lock").resolve()
    if path in _held:
        return True
    handle = path.open("a+b")
    try:
        if sys.platform == "win32":
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return False
    _held[path] = handle  # 一直开着，锁才一直有效
    return True
