"""只听某个程序的声音：真的启动两个播放正弦波的程序，检查只采到选中的那个、程序列表里有它、程序退出时结束。

需要 Windows 11（按进程回环采集）和一个播放设备，不满足时跳过。播放的声音很轻（-50 dBFS），一般听不到。
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time

import numpy as np
import pytest

from tests.test_audio_files import dominant_frequency
from voicedetect.asr.base import SAMPLE_RATE
from voicedetect.audio import apps
from voicedetect.audio.apps import AppCaptureError, AppSource, ProcessInfo, ancestry, list_apps, process_table
from voicedetect.monitor.manager import SourceSpec, build_source


def has_speaker() -> bool:
    try:
        import soundcard

        return soundcard.default_speaker() is not None
    except Exception:
        return False


requires_app_capture = pytest.mark.skipif(
    not apps.supported() or not has_speaker(), reason="需要 Windows 11 和播放设备"
)

# 写下自己的进程 ID，然后循环播放正弦波；最多放 30 秒，测试中途出错也不会一直响。
PLAYER = """
import os, sys, time
import numpy as np
import soundcard as sc

frequency, pid_file = float(sys.argv[1]), sys.argv[2]
with open(pid_file + ".tmp", "w") as handle:
    handle.write(str(os.getpid()))
os.replace(pid_file + ".tmp", pid_file)
rate = 48000
t = np.arange(rate // 2) / rate
tone = (0.003 * np.sin(2 * np.pi * frequency * t)).astype(np.float32)
deadline = time.monotonic() + 30
with sc.default_speaker().player(samplerate=rate, channels=1) as player:
    while time.monotonic() < deadline:
        player.play(tone)
"""


@pytest.fixture
def play(tmp_path):
    """启动一个播放 frequency 赫兹正弦波的程序，返回它的进程 ID。"""
    started: list[int] = []
    script = tmp_path / "player.py"
    script.write_text(PLAYER, encoding="utf-8")

    def start(frequency: int) -> int:
        pid_file = tmp_path / f"player-{frequency}.pid"
        # 经 cmd 的 start 启动：中间的 cmd 随即退出，播放器不在测试进程的进程树里，和用户自己打开的程序一样
        command = ["cmd", "/c", "start", "", "/b", sys.executable, str(script), str(frequency), str(pid_file)]
        subprocess.run(command, check=True)
        deadline = time.monotonic() + 15
        while not pid_file.is_file():
            assert time.monotonic() < deadline, "播放器没有启动"
            time.sleep(0.1)
        pid = int(pid_file.read_text())
        started.append(pid)
        time.sleep(1.5)  # 等它开始出声
        return pid

    yield start
    for pid in started:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass


def band(samples: np.ndarray, frequency: float) -> float:
    """frequency 附近 ±10 Hz 的频谱峰值。"""
    spectrum = np.abs(np.fft.rfft(samples * np.hanning(len(samples))))
    freqs = np.fft.rfftfreq(len(samples), 1 / SAMPLE_RATE)
    return float(spectrum[(freqs > frequency - 10) & (freqs < frequency + 10)].max())


def test_sessions_are_grouped_under_the_program_the_user_started():
    table = {
        1: ProcessInfo(0, "explorer.exe", 100),
        10: ProcessInfo(1, "msedge.exe", 200),  # 浏览器主进程
        11: ProcessInfo(10, "msedge.exe", 210),  # 播放声音的音频服务子进程
        20: ProcessInfo(1, "WeChat.exe", 300),
        21: ProcessInfo(20, "WeChatAppEx.exe", 310),  # 视频号在微信的网页内核子进程里播放
        5: ProcessInfo(4, "svchost.exe", 50),
        30: ProcessInfo(5, "PhoneExperienceHost.exe", 400),  # 由系统服务启动
        40: ProcessInfo(99, "game.exe", 500),  # 父进程已经退出
        51: ProcessInfo(1, "later.exe", 700),
        50: ProcessInfo(51, "early.exe", 600),  # 父进程 ID 被后来启动的进程重新用上了
        60: ProcessInfo(61, "x.exe"),
        61: ProcessInfo(60, "y.exe"),  # 记录有误形成的环
    }
    assert ancestry(11, table) == [11, 10]
    assert ancestry(21, table) == [21, 20]
    assert ancestry(30, table) == [30]
    assert ancestry(40, table) == [40]
    assert ancestry(50, table) == [50]
    assert ancestry(60, table) == [60, 61]


def test_choosing_no_program_is_rejected():
    with pytest.raises(ValueError, match="请选择要听的程序"):
        build_source(SourceSpec(kind="app"))


@pytest.mark.skipif(not apps.supported(), reason="需要 Windows 11")
def test_a_program_that_is_gone_is_reported_before_starting():
    process = subprocess.Popen([sys.executable, "-c", "pass"])
    process.wait()
    with pytest.raises(AppCaptureError, match=r"测试程序.*重新选择"):
        AppSource(process.pid, name="测试程序").prepare()


@requires_app_capture
def test_only_the_chosen_program_is_heard(play):
    chosen = play(523)
    play(880)  # 同时在放的另一个程序
    source = AppSource(chosen, name="测试播放器")
    source.prepare()
    source.open()
    try:
        started = time.monotonic()
        audio = np.concatenate([source.read() for _ in range(20)])  # 2 秒
        elapsed = time.monotonic() - started
    finally:
        source.close()
    assert elapsed >= 1.5  # 按实际时间送来
    tail = audio[4000:]
    assert dominant_frequency(tail) == pytest.approx(523, abs=5)
    assert band(tail, 880) < band(tail, 523) / 100  # 另一个程序的声音低 40 dB 以上，等于没有


@requires_app_capture
def test_programs_playing_sound_are_listed(play):
    pid = play(523)
    root = ancestry(pid, process_table())[-1]
    listed = {app["pid"]: app for app in list_apps()}
    assert listed[root]["active"] is True
    assert listed[root]["exe"].lower() == "python.exe"
    assert listed[root]["name"]
    assert os.getpid() not in listed  # 不列出本程序自己


@requires_app_capture
def test_monitoring_stops_with_a_notice_when_the_program_closes(play):
    pid = play(523)
    source = AppSource(pid, name="测试播放器")
    source.open()
    try:
        assert source.read() is not None
        os.kill(pid, signal.SIGTERM)
        started = time.monotonic()
        while source.read() is not None:
            assert time.monotonic() - started < 5, "程序关闭后没有结束"
    finally:
        source.close()
    [notice] = source.take_notices()
    assert notice[:3] == ("app_closed", "high", "测试播放器已经关闭")
