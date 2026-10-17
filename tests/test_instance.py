"""同一个数据库只有一个主实例：再开一个程序时，它不去改前一个正在进行的场次；在浏览器里打开时网页关掉后自己退出。"""

from __future__ import annotations

import subprocess
import sys
import threading
from types import SimpleNamespace

from voicedetect import desktop, instance
from voicedetect.app import Services
from voicedetect.frameworks import Framework

TRY_LOCK = """
import sys
from pathlib import Path
from voicedetect import instance
print(instance.acquire(Path(sys.argv[1])))
"""
SECOND_PROGRAM = """
import sys
from pathlib import Path
from voicedetect.app import Services
services = Services.create(Path(sys.argv[1]))
print(services.primary, services.storage.get_session("s1")["status"])
services.close()
"""


def test_only_one_process_holds_the_lock(tmp_path):
    folder = tmp_path / "data"
    assert instance.acquire(folder)
    assert instance.acquire(folder)  # 同一个进程再要一次也行
    for target, expected in ((folder, "False"), (tmp_path / "elsewhere", "True")):
        other = subprocess.run(
            [sys.executable, "-c", TRY_LOCK, str(target)], capture_output=True, text=True, check=True
        )
        assert other.stdout.strip() == expected


def test_a_second_program_leaves_running_sessions_alone(tmp_path):
    database = tmp_path / "data.db"
    services = Services.create(database)
    try:
        assert services.primary
        services.storage.create_session(
            session_id="s1",
            title="正在盯的一场",
            framework=Framework(name="框架"),
            source="麦克风",
            engine="SenseVoice（本地）",
            window_seconds=300,
            interval_seconds=60,
        )
        other = subprocess.run(
            [sys.executable, "-c", SECOND_PROGRAM, str(database)], capture_output=True, text=True, check=True
        )
        assert other.stdout.split() == ["False", "running"]  # 不是主实例，没有把它标成“已中断”
    finally:
        services.close()


def test_browser_mode_exits_after_the_page_is_closed(monkeypatch):
    monkeypatch.setattr(desktop.webbrowser, "open", lambda url: True)
    services = SimpleNamespace(bus=SimpleNamespace(listeners=1), monitor=SimpleNamespace(current=None))
    done = threading.Event()

    def run():
        desktop._open_browser("http://127.0.0.1:1/", services, idle_exit=1.5)
        done.set()

    threading.Thread(target=run, daemon=True).start()
    assert not done.wait(3)  # 网页开着就一直运行
    services.bus.listeners = 0
    services.monitor.current = SimpleNamespace(finished=False)
    assert not done.wait(3)  # 网页关了，但还在盯播
    services.monitor.current.finished = True
    assert done.wait(5)  # 盯播结束、网页也关了，过一会儿自己退出
