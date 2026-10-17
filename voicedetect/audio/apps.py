"""只听某个程序的声音（Windows 的按进程回环采集，需要 Windows 11）。

没有网页版的直播（例如微信里的视频号，或者用投屏软件把手机投到电脑上）只能在电脑上的某个程序里播放。
“整个播放设备”会把电脑上所有的声音一起识别；这里只采集选中的程序（连同它启动的子进程）播放的声音，
其他程序的声音和系统提示音都不会混进来。

- 列出程序：枚举各播放设备上的音频会话，每个会话归到用户启动的那个程序（沿父进程往上找，到资源管理器或
  系统服务为止）。浏览器、微信的声音其实由子进程播放，这样也显示成浏览器、微信。名称取程序文件里的“文件说明”。
- 采集：ActivateAudioInterfaceAsync("VAD\\Process_Loopback") 拿到只含这个进程树声音的 IAudioClient，
  直接要 16 kHz 单声道 16 位，由系统完成重采样和混音。程序不出声时系统照样送静音，时间轴不中断。
  实测只能采到程序送到默认播放设备的声音，送到其他设备的只有静音。
- 程序退出时提醒并结束盯播。

用到的都是 Windows 的 COM 接口，用 ctypes 按虚函数表的位置调用，不需要额外的依赖。
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import ExitStack, contextmanager
import ctypes
from dataclasses import dataclass
from functools import cache
import logging
import os
from pathlib import Path
import sys
import threading
import time
from typing import Any
import uuid

import numpy as np

from ..asr.base import SAMPLE_RATE
from .files import BLOCK_SAMPLES

log = logging.getLogger(__name__)

MIN_BUILD = 20348  # 按进程回环采集从这一版开始提供（Windows 11 是 22000 起）
LAG_SECONDS = 0.6  # 系统一时没送来声音时，落后超过这么多就补静音，时间轴不停
ALIVE_CHECK_SECONDS = 1.0
# 沿父进程往上找到这些程序就停：它们只是把程序启动起来，不是用户要听的程序
LAUNCHERS = {
    "explorer.exe",
    "svchost.exe",
    "services.exe",
    "wininit.exe",
    "winlogon.exe",
    "csrss.exe",
    "smss.exe",
    "sihost.exe",
    "runtimebroker.exe",
    "dllhost.exe",
    "taskhostw.exe",
    "userinit.exe",
    "system",
    "[system process]",
}


class AppCaptureError(ValueError):
    """采集不了这个程序的声音。说明文字直接展示给用户。"""


def supported() -> bool:
    return sys.platform == "win32" and sys.getwindowsversion().build >= MIN_BUILD


# —— 程序列表 ——


@dataclass
class ProcessInfo:
    parent: int
    exe: str
    created: int = 0  # 创建时间（FILETIME，100 纳秒为单位）；读不到时为 0


def ancestry(pid: int, table: dict[int, ProcessInfo]) -> list[int]:
    """这个进程和它的各级父进程，到用户启动它的地方（资源管理器、系统服务）为止；最后一个是用户启动的程序。"""
    chain = [pid]
    while True:
        info = table[chain[-1]]
        parent = table.get(info.parent)
        if parent is None or info.parent in chain or parent.exe.lower() in LAUNCHERS:
            return chain
        if parent.created and info.created and parent.created > info.created:
            return chain  # 原来的父进程早已退出，这个进程 ID 被后来的进程重新用上了
        chain.append(info.parent)


def list_apps() -> list[dict[str, Any]]:
    """用着声音的程序（在某个播放设备上有音频会话）：正在出声的排前面。不包括本程序自己。"""
    if not supported():
        return []
    with _com():
        sessions = _sessions()
    table = process_table()
    # 本程序自己（界面里的提示音）。打包成单个 exe 时外面还套着一层启动进程，所以按“最上层”比较。
    own = ancestry(os.getpid(), table)[-1] if os.getpid() in table else os.getpid()
    apps: dict[int, dict[str, Any]] = {}
    for pid, active in sessions:
        if pid not in table:
            continue
        root = ancestry(pid, table)[-1]
        if root == own:
            continue
        if root not in apps:
            path = image_path(root)
            exe = table[root].exe
            apps[root] = {"pid": root, "name": describe(path) or Path(exe).stem, "exe": exe, "active": False}
        apps[root]["active"] = apps[root]["active"] or active
    return sorted(apps.values(), key=lambda app: (not app["active"], app["name"].lower()))


def process_table() -> dict[int, ProcessInfo]:
    """所有进程的父进程、程序文件名和创建时间。"""
    win = _win32()

    entry = _ProcessEntry()
    entry.dwSize = ctypes.sizeof(_ProcessEntry)
    snapshot = win.CreateToolhelp32Snapshot(0x2, 0)  # TH32CS_SNAPPROCESS
    if snapshot in (None, _INVALID_HANDLE):
        raise ctypes.WinError(ctypes.get_last_error())
    table: dict[int, ProcessInfo] = {}
    try:
        ok = win.Process32FirstW(snapshot, ctypes.byref(entry))
        while ok:
            table[entry.th32ProcessID] = ProcessInfo(entry.th32ParentProcessID, entry.szExeFile)
            ok = win.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        win.CloseHandle(snapshot)
    for pid, info in table.items():
        info.created = _created(pid)
    return table


def image_path(pid: int) -> str:
    win = _win32()
    handle = win.OpenProcess(_QUERY_LIMITED, False, pid)
    if not handle:
        return ""
    try:
        size = ctypes.c_ulong(1024)
        buffer = ctypes.create_unicode_buffer(size.value)
        return buffer.value if win.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)) else ""
    finally:
        win.CloseHandle(handle)


def describe(path: str) -> str:
    """程序文件里的“文件说明”，例如 msedge.exe 是“Microsoft Edge”。读不到时返回空字符串。"""
    if not path:
        return ""
    version = _version_dll()
    size = version.GetFileVersionInfoSizeW(path, None)
    if not size:
        return ""
    data = ctypes.create_string_buffer(size)
    if not version.GetFileVersionInfoW(path, 0, size, data):
        return ""
    pointer = ctypes.c_void_p()
    length = ctypes.c_uint()
    codes: list[str] = []
    if version.VerQueryValueW(data, "\\VarFileInfo\\Translation", ctypes.byref(pointer), ctypes.byref(length)):
        words = (ctypes.c_uint16 * (length.value // 2)).from_address(pointer.value)
        codes = [f"{words[i]:04x}{words[i + 1]:04x}" for i in range(0, len(words) - 1, 2)]
    for code in [*codes, "080404b0", "040904b0", "040904e4"]:
        key = f"\\StringFileInfo\\{code}\\FileDescription"
        if version.VerQueryValueW(data, key, ctypes.byref(pointer), ctypes.byref(length)) and length.value > 1:
            text = ctypes.wstring_at(pointer.value).strip()
            if text:
                return text
    return ""


# —— 采集 ——


class AppSource:
    """采集一个程序（含它的子进程）播放的声音。open、read、close 要在同一个线程里调用（会话的采集线程）。"""

    live = True
    # 实测：程序把声音送到默认播放设备以外的设备时，按进程采集只能收到静音
    silence_hint = (
        "确认这个程序正在播放直播、没有在音量合成器里被静音，并且声音是从默认播放设备放出来的"
        "（Windows 只能单独采集送到默认播放设备的声音）。"
    )

    def __init__(self, process_id: int, *, name: str = "") -> None:
        self.process_id = process_id
        self.name = name or "这个程序"
        self.label = f"程序声音：{name}" if name else "程序声音"
        self.discontinuities = 0
        self._com_stack: ExitStack | None = None
        self._client = ctypes.c_void_p()
        self._capture = ctypes.c_void_p()
        self._event = None
        self._process = None  # 进程句柄，用来发现程序退出
        self._next_check = 0.0
        self._buffer = np.zeros(0, dtype=np.float32)
        self._clock = 0.0
        self._produced = 0
        self._notices: list[tuple[str, str, str, str, str]] = []
        self._methods: dict[str, Callable[..., int]] = {}

    def prepare(self) -> None:
        """开播前检查：系统支不支持、程序还在不在。不在就直接报错，不留下一场空记录。"""
        if not supported():
            raise AppCaptureError("只听某个程序的声音需要 Windows 11。请改用“整个播放设备”。")
        handle = _win32().OpenProcess(_SYNCHRONIZE | _QUERY_LIMITED, False, self.process_id)
        if not handle:
            raise AppCaptureError(f"找不到{self.name}，可能已经关闭了。请刷新程序列表重新选择。")
        try:
            if _exited(handle):
                raise AppCaptureError(f"{self.name}已经关闭了。请刷新程序列表重新选择。")
        finally:
            _win32().CloseHandle(handle)

    def open(self) -> None:
        self.prepare()
        win = _win32()
        self._com_stack = ExitStack()
        self._com_stack.enter_context(_com())
        self._process = win.OpenProcess(_SYNCHRONIZE | _QUERY_LIMITED, False, self.process_id)
        self._client = _activate(self.process_id)
        client = self._client
        audio_format = _WaveFormat(1, 1, SAMPLE_RATE, SAMPLE_RATE * 2, 2, 16, 0)  # 16 kHz 单声道 16 位 PCM
        flags = _LOOPBACK | _EVENT_CALLBACK | _AUTO_CONVERT_PCM | _SRC_DEFAULT_QUALITY
        initialize = _method(
            client,
            3,
            ctypes.c_int,
            ctypes.c_ulong,
            ctypes.c_longlong,
            ctypes.c_longlong,
            ctypes.POINTER(_WaveFormat),
            ctypes.c_void_p,
        )
        _check(initialize(0, flags, 2_000_000, 0, ctypes.byref(audio_format), None), "设置采集格式")  # 缓冲 0.2 秒
        self._event = win.CreateEventW(None, False, False, None)
        _check(_method(client, 13, ctypes.c_void_p)(self._event), "设置采集通知")
        get_service = _method(client, 14, ctypes.POINTER(_GUID), ctypes.POINTER(ctypes.c_void_p))
        self._capture = _get(get_service, ctypes.byref(_IID_CAPTURE_CLIENT), what="开始采集")
        capture = self._capture
        self._methods = {
            "next": _method(capture, 5, ctypes.POINTER(ctypes.c_uint32)),
            "get": _method(
                capture,
                3,
                ctypes.POINTER(ctypes.c_void_p),
                ctypes.POINTER(ctypes.c_uint32),
                ctypes.POINTER(ctypes.c_ulong),
                ctypes.c_void_p,
                ctypes.c_void_p,
            ),
            "release": _method(capture, 4, ctypes.c_uint32),
        }
        _check(_method(client, 10)(), "开始采集")
        self._clock = time.monotonic()
        self._next_check = self._clock + ALIVE_CHECK_SECONDS

    def read(self) -> np.ndarray | None:
        while len(self._buffer) < BLOCK_SAMPLES:
            now = time.monotonic()
            if now >= self._next_check:
                self._next_check = now + ALIVE_CHECK_SECONDS
                if self._process and _exited(self._process):
                    self._notices.append(
                        (
                            "app_closed",
                            "high",
                            f"{self.name}已经关闭",
                            "这个程序退出了，收不到它的声音，盯播到此停止。",
                            "重新打开程序和直播后，再开始一场盯播。",
                        )
                    )
                    return None
            behind = (now - self._clock) * SAMPLE_RATE - self._produced
            if behind > LAG_SECONDS * SAMPLE_RATE:
                padding = np.zeros(BLOCK_SAMPLES - len(self._buffer), dtype=np.float32)
                self._buffer = np.concatenate([self._buffer, padding])
                break
            _win32().WaitForSingleObject(self._event, 50)
            self._drain()
        block, self._buffer = self._buffer[:BLOCK_SAMPLES], self._buffer[BLOCK_SAMPLES:]
        self._produced += len(block)
        return block

    def take_notices(self) -> list[tuple[str, str, str, str, str]]:
        notices, self._notices = self._notices, []
        return notices

    def close(self) -> None:
        win = _win32()
        if self._client:
            _method(self._client, 11)()  # Stop
        for pointer in (self._capture, self._client):
            if pointer:
                _release(pointer)
        self._capture = ctypes.c_void_p()
        self._client = ctypes.c_void_p()
        self._methods = {}
        for name in ("_event", "_process"):
            handle = getattr(self, name)
            if handle:
                win.CloseHandle(handle)
            setattr(self, name, None)
        if self._com_stack is not None:
            self._com_stack.close()
            self._com_stack = None

    def _drain(self) -> None:
        """取出系统已经送来的全部声音。"""
        size = ctypes.c_uint32()
        data = ctypes.c_void_p()
        frames = ctypes.c_uint32()
        flags = ctypes.c_ulong()
        chunks = [self._buffer]
        while True:
            _check(self._methods["next"](ctypes.byref(size)), "读取程序声音")
            if not size.value:
                break
            _check(
                self._methods["get"](ctypes.byref(data), ctypes.byref(frames), ctypes.byref(flags), None, None),
                "读取程序声音",
            )
            try:
                if flags.value & _SILENT or not data.value:
                    chunks.append(np.zeros(frames.value, dtype=np.float32))
                else:
                    pcm = np.frombuffer(ctypes.string_at(data.value, frames.value * 2), dtype="<i2")
                    chunks.append(pcm.astype(np.float32) / 32768)
                self.discontinuities += bool(flags.value & _DISCONTINUITY)
            finally:
                self._methods["release"](frames.value)
        if len(chunks) > 1:
            self._buffer = np.concatenate(chunks)


# —— COM 和 Win32 ——

_HRESULT = ctypes.c_long
_INVALID_HANDLE = ctypes.c_void_p(-1).value
_QUERY_LIMITED = 0x1000  # PROCESS_QUERY_LIMITED_INFORMATION
_SYNCHRONIZE = 0x00100000
_CLSCTX_ALL = 0x17
_LOOPBACK = 0x00020000
_EVENT_CALLBACK = 0x00040000
_AUTO_CONVERT_PCM = 0x80000000
_SRC_DEFAULT_QUALITY = 0x08000000
_SILENT = 0x2
_DISCONTINUITY = 0x1
_E_NOINTERFACE = -0x7FFFBFFE  # 0x80004002
_RPC_E_CHANGED_MODE = -0x7FFEFEFA  # 0x80010106：这个线程已经是单线程套间，照样能用


class _GUID(ctypes.Structure):
    _fields_ = [("data", ctypes.c_ubyte * 16)]


def _guid(text: str) -> _GUID:
    return _GUID.from_buffer_copy(uuid.UUID(text).bytes_le)


_CLSID_DEVICE_ENUMERATOR = _guid("BCDE0395-E52F-467C-8E3D-C4579291692E")
_IID_DEVICE_ENUMERATOR = _guid("A95664D2-9614-4F35-A746-DE8DB63617E6")
_IID_SESSION_MANAGER2 = _guid("77AA99A0-1BD6-484F-8BC7-2C654C9A9B6F")
_IID_SESSION_CONTROL2 = _guid("BFB7FF88-7239-4FC9-8FA2-07C950BE9C6D")
_IID_AUDIO_CLIENT = _guid("1CB9AD4C-DBFA-4C32-B178-C2F568A703B2")
_IID_CAPTURE_CLIENT = _guid("C8ADBD64-E71E-48A0-A4DE-185C395CD317")
_HANDLER_IIDS = {
    bytes(_guid("00000000-0000-0000-C000-000000000046")),  # IUnknown
    bytes(_guid("94EA2B94-E9CC-49E0-C0FF-EE64CA8F5B90")),  # IAgileObject：系统在别的线程回调
    bytes(_guid("41D949AB-9862-444A-80F6-C261334DA5EB")),  # IActivateAudioInterfaceCompletionHandler
}


class _ProcessEntry(ctypes.Structure):
    _fields_ = [
        ("dwSize", ctypes.c_ulong),
        ("cntUsage", ctypes.c_ulong),
        ("th32ProcessID", ctypes.c_ulong),
        ("th32DefaultHeapID", ctypes.c_size_t),
        ("th32ModuleID", ctypes.c_ulong),
        ("cntThreads", ctypes.c_ulong),
        ("th32ParentProcessID", ctypes.c_ulong),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", ctypes.c_ulong),
        ("szExeFile", ctypes.c_wchar * 260),
    ]


class _FileTime(ctypes.Structure):
    _fields_ = [("low", ctypes.c_ulong), ("high", ctypes.c_ulong)]


class _WaveFormat(ctypes.Structure):
    _pack_ = 1
    _fields_ = [
        ("wFormatTag", ctypes.c_ushort),
        ("nChannels", ctypes.c_ushort),
        ("nSamplesPerSec", ctypes.c_ulong),
        ("nAvgBytesPerSec", ctypes.c_ulong),
        ("nBlockAlign", ctypes.c_ushort),
        ("wBitsPerSample", ctypes.c_ushort),
        ("cbSize", ctypes.c_ushort),
    ]


class _LoopbackParams(ctypes.Structure):
    _fields_ = [("TargetProcessId", ctypes.c_ulong), ("ProcessLoopbackMode", ctypes.c_int)]


class _ActivationParams(ctypes.Structure):
    _fields_ = [("ActivationType", ctypes.c_int), ("ProcessLoopbackParams", _LoopbackParams)]


class _Blob(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_ulong), ("pBlobData", ctypes.c_void_p)]


class _PropVariant(ctypes.Structure):
    _fields_ = [
        ("vt", ctypes.c_ushort),
        ("reserved1", ctypes.c_ushort),
        ("reserved2", ctypes.c_ushort),
        ("reserved3", ctypes.c_ushort),
        ("blob", _Blob),
    ]


if sys.platform == "win32":
    _QUERY = ctypes.WINFUNCTYPE(_HRESULT, ctypes.c_void_p, ctypes.POINTER(_GUID), ctypes.POINTER(ctypes.c_void_p))
    _COUNT = ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)
    _COMPLETED = ctypes.WINFUNCTYPE(_HRESULT, ctypes.c_void_p, ctypes.c_void_p)

    class _HandlerTable(ctypes.Structure):
        _fields_ = [("QueryInterface", _QUERY), ("AddRef", _COUNT), ("Release", _COUNT), ("Completed", _COMPLETED)]

    class _HandlerObject(ctypes.Structure):
        _fields_ = [("table", ctypes.POINTER(_HandlerTable))]


_KEEP: list[Any] = []  # 交给系统的回调对象：系统可能在回调之后才释放它，所以一直留着（每次开播几百字节）


class _Handler:
    """IActivateAudioInterfaceCompletionHandler：系统准备好采集接口后在后台线程回调。"""

    def __init__(self, completed: Callable[[ctypes.c_void_p], None]) -> None:
        def query(this, riid, out):
            if bytes(riid.contents) in _HANDLER_IIDS:
                out[0] = this
                return 0
            out[0] = None
            return _E_NOINTERFACE

        def done(_this, operation):
            completed(operation)
            return 0

        self.functions = (_QUERY(query), _COUNT(lambda _this: 1), _COUNT(lambda _this: 1), _COMPLETED(done))
        self.table = _HandlerTable(*self.functions)
        self.com = _HandlerObject(ctypes.pointer(self.table))


def _activate(pid: int) -> ctypes.c_void_p:
    """拿到只采集这个进程（含子进程）声音的 IAudioClient。"""
    done = threading.Event()
    result: dict[str, Any] = {}

    def completed(operation) -> None:
        status = _HRESULT()
        client = ctypes.c_void_p()
        get_result = _method(operation, 3, ctypes.POINTER(_HRESULT), ctypes.POINTER(ctypes.c_void_p))
        result["call"] = get_result(ctypes.byref(status), ctypes.byref(client))
        result["status"], result["client"] = status.value, client
        done.set()

    handler = _Handler(completed)
    _KEEP.append(handler)
    params = _ActivationParams(1, _LoopbackParams(pid, 0))  # 采集这个进程和它的子进程
    blob = _Blob(ctypes.sizeof(params), ctypes.cast(ctypes.byref(params), ctypes.c_void_p))
    variant = _PropVariant(65, 0, 0, 0, blob)  # 65：VT_BLOB
    operation = ctypes.c_void_p()
    activate = _mmdevapi().ActivateAudioInterfaceAsync
    _check(
        activate(
            "VAD\\Process_Loopback",
            ctypes.byref(_IID_AUDIO_CLIENT),
            ctypes.byref(variant),
            ctypes.byref(handler.com),
            ctypes.byref(operation),
        ),
        "开始采集程序声音",
    )
    try:
        if not done.wait(5):
            raise AppCaptureError("系统没有响应采集请求，请重试。")
    finally:
        _release(operation)
    _check(result["call"], "开始采集程序声音")
    _check(result["status"], "开始采集程序声音")
    return result["client"]


def _sessions() -> list[tuple[int, bool]]:
    """各播放设备上的音频会话：(进程 ID, 是否正在出声)。系统提示音和已经结束的会话不算。"""
    found: list[tuple[int, bool]] = []
    with ExitStack() as stack:
        enumerator = ctypes.c_void_p()
        _check(
            _ole32().CoCreateInstance(
                ctypes.byref(_CLSID_DEVICE_ENUMERATOR),
                None,
                _CLSCTX_ALL,
                ctypes.byref(_IID_DEVICE_ENUMERATOR),
                ctypes.byref(enumerator),
            ),
            "读取播放设备",
        )
        stack.callback(_release, enumerator)
        enumerate_devices = _method(enumerator, 3, ctypes.c_int, ctypes.c_ulong, ctypes.POINTER(ctypes.c_void_p))
        devices = _get(enumerate_devices, 0, 1, what="读取播放设备")  # 正在使用的播放设备
        stack.callback(_release, devices)
        count = ctypes.c_uint()
        _check(_method(devices, 3, ctypes.POINTER(ctypes.c_uint))(ctypes.byref(count)), "读取播放设备")
        for index in range(count.value):
            try:
                found.extend(_device_sessions(devices, index))
            except OSError as exc:  # 个别虚拟声卡不支持音频会话
                log.info("skipping audio device %d: %s", index, exc)
    return found


def _device_sessions(devices: ctypes.c_void_p, index: int) -> list[tuple[int, bool]]:
    found = []
    with ExitStack() as stack:
        device = _get(_method(devices, 4, ctypes.c_uint, ctypes.POINTER(ctypes.c_void_p)), index, what="读取播放设备")
        stack.callback(_release, device)
        activate = _method(
            device, 3, ctypes.POINTER(_GUID), ctypes.c_ulong, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)
        )
        manager = _get(activate, ctypes.byref(_IID_SESSION_MANAGER2), _CLSCTX_ALL, None, what="读取音频会话")
        stack.callback(_release, manager)
        sessions = _get(_method(manager, 5, ctypes.POINTER(ctypes.c_void_p)), what="读取音频会话")
        stack.callback(_release, sessions)
        count = ctypes.c_int()
        _check(_method(sessions, 3, ctypes.POINTER(ctypes.c_int))(ctypes.byref(count)), "读取音频会话")
        get_session = _method(sessions, 4, ctypes.c_int, ctypes.POINTER(ctypes.c_void_p))
        for number in range(count.value):
            with ExitStack() as inner:
                control = _get(get_session, number, what="读取音频会话")
                inner.callback(_release, control)
                query = _method(control, 0, ctypes.POINTER(_GUID), ctypes.POINTER(ctypes.c_void_p))
                control2 = _get(query, ctypes.byref(_IID_SESSION_CONTROL2), what="读取音频会话")
                inner.callback(_release, control2)
                if _method(control2, 15)() == 0:  # IsSystemSoundsSession：S_OK 表示系统提示音
                    continue
                pid = ctypes.c_ulong()
                state = ctypes.c_int()
                if _method(control2, 14, ctypes.POINTER(ctypes.c_ulong))(ctypes.byref(pid)) < 0:
                    continue
                _check(_method(control2, 3, ctypes.POINTER(ctypes.c_int))(ctypes.byref(state)), "读取音频会话")
                if pid.value and state.value != 2:  # 2：会话已经结束
                    found.append((pid.value, state.value == 1))
    return found


def _method(pointer: ctypes.c_void_p, index: int, *argtypes: Any) -> Callable[..., int]:
    """COM 接口的第 index 个方法（按虚函数表里的位置），返回 HRESULT。"""
    table = ctypes.cast(pointer, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    function = ctypes.WINFUNCTYPE(_HRESULT, ctypes.c_void_p, *argtypes)(table[index])
    return lambda *args: function(pointer, *args)


def _get(method: Callable[..., int], *args: Any, what: str) -> ctypes.c_void_p:
    """调用最后一个参数是“输出接口指针”的方法，返回这个指针。"""
    result = ctypes.c_void_p()
    _check(method(*args, ctypes.byref(result)), what)
    return result


def _release(pointer: ctypes.c_void_p) -> None:
    if pointer:
        _method(pointer, 2)()


def _check(result: int, what: str) -> None:
    if result < 0:
        raise OSError(f"{what}失败（错误码 0x{result & 0xFFFFFFFF:08X}）")


@contextmanager
def _com():
    """在当前线程初始化 COM（多线程套间），用完再释放。"""
    result = _ole32().CoInitializeEx(None, 0)
    if result < 0 and result != _RPC_E_CHANGED_MODE:
        raise OSError(f"初始化 COM 失败（错误码 0x{result & 0xFFFFFFFF:08X}）")
    try:
        yield
    finally:
        if result >= 0:
            _ole32().CoUninitialize()


def _exited(handle) -> bool:
    return _win32().WaitForSingleObject(handle, 0) == 0  # WAIT_OBJECT_0：进程已经结束


def _created(pid: int) -> int:
    win = _win32()
    handle = win.OpenProcess(_QUERY_LIMITED, False, pid)
    if not handle:
        return 0
    try:
        times = [_FileTime() for _ in range(4)]
        if not win.GetProcessTimes(handle, *(ctypes.byref(item) for item in times)):
            return 0
        return times[0].high << 32 | times[0].low
    finally:
        win.CloseHandle(handle)


@cache
def _win32():
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    signatures = {
        "OpenProcess": (wintypes.HANDLE, [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]),
        "CloseHandle": (wintypes.BOOL, [wintypes.HANDLE]),
        "CreateToolhelp32Snapshot": (wintypes.HANDLE, [wintypes.DWORD, wintypes.DWORD]),
        "Process32FirstW": (wintypes.BOOL, [wintypes.HANDLE, ctypes.POINTER(_ProcessEntry)]),
        "Process32NextW": (wintypes.BOOL, [wintypes.HANDLE, ctypes.POINTER(_ProcessEntry)]),
        "QueryFullProcessImageNameW": (
            wintypes.BOOL,
            [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)],
        ),
        "GetProcessTimes": (wintypes.BOOL, [wintypes.HANDLE, *[ctypes.POINTER(_FileTime)] * 4]),
        "WaitForSingleObject": (wintypes.DWORD, [wintypes.HANDLE, wintypes.DWORD]),
        "CreateEventW": (wintypes.HANDLE, [ctypes.c_void_p, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR]),
    }
    for name, (restype, argtypes) in signatures.items():
        function = getattr(kernel32, name)
        function.restype, function.argtypes = restype, argtypes
    return kernel32


@cache
def _ole32():
    ole32 = ctypes.WinDLL("ole32")
    ole32.CoInitializeEx.restype = _HRESULT
    ole32.CoInitializeEx.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
    ole32.CoUninitialize.restype = None
    ole32.CoCreateInstance.restype = _HRESULT
    ole32.CoCreateInstance.argtypes = [
        ctypes.POINTER(_GUID),
        ctypes.c_void_p,
        ctypes.c_ulong,
        ctypes.POINTER(_GUID),
        ctypes.POINTER(ctypes.c_void_p),
    ]
    return ole32


@cache
def _mmdevapi():
    mmdevapi = ctypes.WinDLL("mmdevapi")
    activate = mmdevapi.ActivateAudioInterfaceAsync
    activate.restype = _HRESULT
    activate.argtypes = [
        ctypes.c_wchar_p,
        ctypes.POINTER(_GUID),
        ctypes.POINTER(_PropVariant),
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_void_p),
    ]
    return mmdevapi


@cache
def _version_dll():
    version = ctypes.WinDLL("version")
    version.GetFileVersionInfoSizeW.restype = ctypes.c_ulong
    version.GetFileVersionInfoSizeW.argtypes = [ctypes.c_wchar_p, ctypes.c_void_p]
    version.GetFileVersionInfoW.restype = ctypes.c_int
    version.GetFileVersionInfoW.argtypes = [ctypes.c_wchar_p, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_void_p]
    version.VerQueryValueW.restype = ctypes.c_int
    version.VerQueryValueW.argtypes = [
        ctypes.c_void_p,
        ctypes.c_wchar_p,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_uint),
    ]
    return version
