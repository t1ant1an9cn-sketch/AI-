"""桌面程序的启动：打包后双击打开时没有命令行窗口（sys.stdout、sys.stderr 是 None），本地服务也要能起来。"""

from __future__ import annotations

import sys

import httpx

from voicedetect.__main__ import main
from voicedetect.api import create_app
from voicedetect.app import Services
from voicedetect.desktop import LocalServer, free_port


def test_local_server_starts_without_a_console(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)
    services = Services.create(tmp_path / "data.db")
    server = LocalServer(create_app(services, token="t"), free_port())
    try:
        server.start()
        response = httpx.get(f"http://127.0.0.1:{server.port}/api/info", headers={"X-Token": "t"})
        assert response.status_code == 200
    finally:
        server.stop()
        services.close()


def test_commands_run_without_a_console(monkeypatch):
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)
    assert main(["models", "status"]) == 0
    assert sys.stdout is not None and sys.stderr is not None  # 换成了丢弃输出的文件，print 不会出错
