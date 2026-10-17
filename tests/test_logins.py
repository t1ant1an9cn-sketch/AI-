"""直播平台登录：在登录窗口里登录一次，之后后台浏览器打开要登录才能看的直播间时自动带上 Cookie。

用本机的 Edge/Chrome 打开模拟网站（tests/test_room.py 的 Site）；登录窗口用无界面模式，测试时不弹出窗口。
"""

from __future__ import annotations

import sys
import time

import pytest

from tests.test_audio_files import dominant_frequency
from tests.test_room import LOGIN_COOKIE, Site, read_seconds, requires_browser
from voicedetect.audio.logins import LoginWindow, cookies_path, load_cookies, logins_dir, save_cookies, to_params
from voicedetect.audio.room import RoomError, RoomSource


@pytest.fixture
def site(tmp_path):
    site = Site(tmp_path)
    yield site
    site.server.shutdown()


def log_in(site, window: LoginWindow) -> None:
    """在登录窗口里打开登录页，等登录 Cookie 存下来，再关掉窗口。"""
    try:
        window.open(site.url("/login"))
        deadline = time.monotonic() + 10
        while not any(cookie["name"] == "vd_login" for cookie in load_cookies()):
            assert time.monotonic() < deadline, "登录后的 Cookie 没有存下来"
            time.sleep(0.2)
    finally:
        window.close()


def test_saved_cookies_round_trip_and_are_encrypted_on_windows():
    cookies = [{"name": "sessionid", "value": "abc123-会话", "domain": ".douyin.com", "path": "/"}]
    save_cookies(cookies)
    assert load_cookies() == cookies
    if sys.platform == "win32":
        assert b"abc123" not in cookies_path().read_bytes()  # DPAPI 加密，文件里看不到原文


def test_unreadable_cookie_file_is_ignored():
    cookies_path().parent.mkdir(parents=True)
    cookies_path().write_bytes(b"not a cookie file")
    assert load_cookies() == []


def test_session_cookies_are_saved_without_an_expiry():
    read = [
        {
            "name": "cookie2",
            "value": "1",
            "domain": ".taobao.com",
            "path": "/",
            "expires": -1,
            "size": 8,
            "httpOnly": True,
            "secure": True,
            "session": True,
            "sameSite": "None",
            "priority": "Medium",
            "sourceScheme": "Secure",
            "sourcePort": 443,
        },
        {"name": "ttwid", "value": "2", "domain": "live.douyin.com", "path": "/", "expires": 1.9e9, "session": False},
    ]
    assert to_params(read) == [
        {"name": "cookie2", "value": "1", "domain": ".taobao.com", "path": "/", "secure": True, "httpOnly": True,
         "sameSite": "None"},
        {"name": "ttwid", "value": "2", "domain": "live.douyin.com", "path": "/", "expires": 1.9e9},
    ]  # fmt: skip


@requires_browser
def test_room_that_needs_login_works_after_logging_in_once(site):
    room = site.url("/private-room")
    with pytest.raises(RoomError, match="登录平台账号"):
        RoomSource(room, page_audio_after=2, resolve_timeout=4).prepare()

    events = []
    window = LoginWindow(headless=True, on_change=events.append)
    log_in(site, window)
    assert not window.is_open
    assert events[0]["open"] is True and events[-1]["open"] is False
    assert events[-1]["saved_at"] is not None
    [cookie] = [item for item in load_cookies() if item["name"] == "vd_login"]
    assert cookie["value"] == "1" and "expires" not in cookie  # 会话 Cookie 也存下来了

    source = RoomSource(room, resolve_timeout=20)
    try:
        source.prepare()
        assert source.mode == "stream"
        source.open()
        audio = read_seconds(source, 1.0)
        assert dominant_frequency(audio[4000:]) == pytest.approx(440, abs=5)
    finally:
        source.close()
    pages = [headers for path, headers in site.requests if path == "/private-room"]
    assert LOGIN_COOKIE not in pages[0].get("Cookie", "")  # 登录前
    assert LOGIN_COOKIE in pages[-1].get("Cookie", "")  # 登录后，后台浏览器带上了存下的 Cookie


@requires_browser
def test_reopening_the_login_window_keeps_session_cookies(site):
    window = LoginWindow(headless=True)
    log_in(site, window)
    before = len(site.requests)
    try:
        window.open(site.url("/private-room"))  # 浏览器重新启动过，会话 Cookie 要靠程序放回去
        deadline = time.monotonic() + 10
        while not any(path == "/private-room" for path, _ in site.requests[before:]):
            assert time.monotonic() < deadline, "登录窗口没有打开网页"
            time.sleep(0.2)
    finally:
        window.close()
    [headers] = [headers for path, headers in site.requests[before:] if path == "/private-room"]
    assert LOGIN_COOKIE in headers.get("Cookie", "")
    assert any(cookie["name"] == "vd_login" for cookie in load_cookies())  # 关窗口时没有丢掉


@requires_browser
def test_clearing_forgets_the_login(site):
    window = LoginWindow(headless=True)
    log_in(site, window)
    assert window.status()["saved_at"] is not None
    assert window.clear() == {"open": False, "saved_at": None}
    assert load_cookies() == [] and not logins_dir().exists()
