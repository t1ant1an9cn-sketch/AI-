from pathlib import Path

from fastapi.testclient import TestClient
import pytest
from starlette.websockets import WebSocketDisconnect

from tests.fakes import BlockSource, ScriptedRecognizer
from voicedetect.api import create_app
from voicedetect.app import Services
from voicedetect.audio.logins import load_cookies, save_cookies
from voicedetect.config import SECRET_MASK
from voicedetect.monitor.manager import SessionManager

TOKEN = "test-token"
LINES = [(1, 5, "欢迎宝宝们，杯子是316不锈钢的"), (8, 12, "全网最低价，赶紧下单")]


def make_services(tmp_path, *, seconds=20, live=False, pace=0.0, fake_engine=True):
    services = Services.create(tmp_path / "data.db")
    if fake_engine:
        services.monitor = SessionManager(
            storage=services.storage,
            bus=services.bus,
            settings=services.settings,
            make_source=lambda spec: BlockSource(seconds, live=live, pace=pace),
            make_recognizer=lambda settings, partials: lambda: ScriptedRecognizer(LINES),
            make_reviewer=lambda settings: None,
        )
    return services


@pytest.fixture
def services(tmp_path):
    services = make_services(tmp_path)
    yield services
    services.close()


def connect(services, *, host="127.0.0.1:8000", token=TOKEN):
    return TestClient(create_app(services, token=TOKEN), base_url=f"http://{host}", headers={"X-Token": token})


@pytest.fixture
def client(services):
    with connect(services) as client:
        yield client


START = {
    "framework_id": "tpl-thermos-cup",
    "title": "周五晚场",
    "source": {"kind": "demo"},
    "window_seconds": 60,
    "interval_seconds": 10,
}


def test_requests_need_the_token_and_a_local_host(services):
    with connect(services, token="wrong") as client:
        assert client.get("/api/info").status_code == 401
    with connect(services, host="evil.example") as client:
        assert client.get("/api/info").status_code == 403
    with connect(services) as client:
        assert client.get("/api/info").json()["version"]
        assert "界面还没有构建" in client.get("/").text


def test_framework_crud_and_readable_validation_errors(client):
    names = {item["id"] for item in client.get("/api/frameworks").json()}
    assert {"tpl-general", "tpl-thermos-cup"} <= names

    created = client.post("/api/frameworks", json={"id": "tpl-general", "name": "我的框架"}).json()
    assert created["id"] != "tpl-general"  # 导入时总是分配新 ID，不覆盖已有框架
    updated = client.put(f"/api/frameworks/{created['id']}", json={**created, "name": "改名"}).json()
    assert client.get(f"/api/frameworks/{created['id']}").json()["name"] == updated["name"] == "改名"

    bad = client.post("/api/frameworks", json={"name": "x", "stages": [{"name": "甲", "points": [{"name": ""}]}]})
    assert (bad.status_code, bad.json()["detail"]) == (422, "第 1 个环节 › 第 1 个要点 › 名称：不能为空")
    dup = client.post(
        "/api/frameworks", json={"name": "x", "stages": [{"id": "a", "name": "甲"}, {"id": "a", "name": "乙"}]}
    )
    assert dup.json()["detail"] == "环节、要点或违规规则的 ID 重复：a"

    assert client.delete(f"/api/frameworks/{created['id']}").status_code == 204
    assert client.get(f"/api/frameworks/{created['id']}").status_code == 404


def test_settings_hide_secrets_and_keep_them_when_masked(client, services):
    settings = client.get("/api/settings").json()
    settings["llm"].update(enabled=True, api_key="sk-secret", model="deepseek-chat")
    saved = client.put("/api/settings", json=settings).json()
    assert saved["llm"]["api_key"] == SECRET_MASK
    saved["monitor"]["interval_seconds"] = 30
    client.put("/api/settings", json=saved)
    assert services.settings().llm.api_key == "sk-secret"
    assert services.settings().monitor.interval_seconds == 30

    bad = client.put("/api/settings", json={**saved, "monitor": {"window_seconds": 60, "interval_seconds": 120}})
    assert bad.status_code == 422 and "分析间隔不能大于分析窗口" in bad.json()["detail"]


def events_socket(client, token=TOKEN):
    # TestClient 的 WebSocket 固定用 testserver 作为主机名，这里补上本机 Host 头。
    return client.websocket_connect(f"/api/events?token={token}", headers={"host": "127.0.0.1:8000"})


def collect_until_stopped(socket):
    events = []
    while True:
        event = socket.receive_json()
        events.append(event)
        if event["type"] == "state" and event["state"] in ("stopped", "failed"):
            return events


def test_full_session_over_http_and_websocket(client):
    with events_socket(client) as socket:
        assert socket.receive_json() == {"type": "hello", "session": None}
        started = client.post("/api/session/start", json=START).json()
        assert started["title"] == "周五晚场" and started["framework"]["id"] == "tpl-thermos-cup"
        events = collect_until_stopped(socket)

    kinds = {event["type"] for event in events}
    assert {"state", "segment", "report", "alert", "summary"} <= kinds
    forbidden = [e["alert"] for e in events if e["type"] == "alert" and e["alert"]["kind"] == "forbidden"]
    assert forbidden[0]["evidence"][0]["text"] == "全网最低"

    session_id = started["session_id"]
    assert client.get("/api/session").json()["state"] == "stopped"
    history = client.get("/api/sessions").json()
    assert history["total"] == 1 and history["items"][0]["status"] == "stopped"
    detail = client.get(f"/api/sessions/{session_id}").json()
    assert [s["text"] for s in detail["segments"]] == [line[2] for line in LINES]
    report = client.get(f"/api/sessions/{session_id}/report.md")
    assert report.headers["content-type"].startswith("text/markdown")
    assert report.text.startswith("# 盯播复盘：周五晚场")
    assert client.delete(f"/api/sessions/{session_id}").status_code == 204
    assert client.get(f"/api/sessions/{session_id}").status_code == 404


def test_only_one_session_at_a_time(tmp_path):
    services = make_services(tmp_path, seconds=60, live=True, pace=0.01)
    try:
        with connect(services) as client:
            assert client.post("/api/session/start", json=START).status_code == 200
            busy = client.post("/api/session/start", json=START)
            assert (busy.status_code, busy.json()["detail"]) == (409, "已经有一场盯播在进行，请先停止")
            session_id = client.get("/api/session").json()["session_id"]
            assert client.delete(f"/api/sessions/{session_id}").status_code == 409
            assert client.post("/api/session/stop").json()["state"] in ("stopping", "stopped")
            assert services.monitor.current.wait(10)
    finally:
        services.close()


def test_missing_model_is_reported_before_starting(tmp_path, monkeypatch):
    monkeypatch.setenv("VOICEDETECT_MODELS", str(tmp_path / "no-models"))
    services = make_services(tmp_path, fake_engine=False)
    try:
        with connect(services) as client:
            response = client.post("/api/session/start", json=START)
            assert response.status_code == 400 and "下载" in response.json()["detail"]
            assert client.get("/api/models").json()["installed"] is False
            assert client.get("/api/sessions").json()["total"] == 0
    finally:
        services.close()


def test_unknown_framework_and_bad_source_are_rejected(client):
    assert client.post("/api/session/start", json={**START, "framework_id": "nope"}).json()["detail"] == (
        "找不到这个话术框架，可能已被删除"
    )
    assert client.post("/api/session/start", json={**START, "source": {"kind": "cdrom"}}).status_code == 422


def test_upload_keeps_a_safe_file_name(client):
    response = client.put("/api/uploads", params={"name": "../第一场 录播.mp3"}, content=b"ID3fake")
    saved = Path(response.json()["path"])
    assert saved.read_bytes() == b"ID3fake"
    assert saved.name.endswith("第一场_录播.mp3") and ".." not in saved.name


def test_websocket_rejects_a_wrong_token_or_foreign_host(client):
    with pytest.raises(WebSocketDisconnect), events_socket(client, token="wrong") as socket:
        socket.receive_json()
    with pytest.raises(WebSocketDisconnect), client.websocket_connect(f"/api/events?token={TOKEN}") as socket:
        socket.receive_json()  # Host 为 testserver，不是本机


def test_llm_connection_errors_are_explained(client):
    body = {"enabled": True, "base_url": "http://127.0.0.1:9/v1", "api_key": "", "model": "qwen-plus"}
    response = client.post("/api/llm/test", json=body)
    assert response.status_code == 400 and "连不上大模型接口" in response.json()["detail"]


def run_session(client, services):
    """开启录音，跑完一场，返回场次 ID。"""
    settings = client.get("/api/settings").json()
    settings["monitor"]["save_audio"] = True
    assert client.put("/api/settings", json=settings).json()["monitor"]["save_audio"] is True
    session_id = client.post("/api/session/start", json=START).json()["session_id"]
    assert services.monitor.current.wait(10)
    return session_id


def test_live_sessions_are_recorded_and_can_be_played_with_seeking(tmp_path):
    services = make_services(tmp_path, seconds=3, live=True, pace=0.001)
    try:
        with connect(services) as client:
            session_id = run_session(client, services)
            recording = client.get(f"/api/sessions/{session_id}").json()["recording"]
            assert recording["bytes"] == 44 + 3 * 16000 * 2 and recording["seconds"] == 3.0
            assert Path(recording["path"]).is_file()

            # <audio> 元素没法加请求头，令牌放在网址里；其他接口不接受这种写法
            no_header = {"X-Token": ""}
            url = f"/api/sessions/{session_id}/audio"
            audio = client.get(url, params={"token": TOKEN}, headers=no_header)
            assert audio.status_code == 200 and audio.headers["content-type"] == "audio/wav"
            assert audio.content[:4] == b"RIFF" and len(audio.content) == recording["bytes"]
            part = client.get(url, params={"token": TOKEN}, headers={**no_header, "Range": "bytes=44-143"})
            assert part.status_code == 206 and len(part.content) == 100  # 拖动进度条
            assert client.get(url, headers=no_header).status_code == 401
            assert client.get("/api/info", params={"token": TOKEN}, headers=no_header).status_code == 401

            assert client.delete(f"/api/sessions/{session_id}").status_code == 204
            assert not Path(recording["path"]).exists()  # 删除场次时录音一起删
            assert client.get(url).status_code == 404
    finally:
        services.close()


def test_files_are_not_recorded_again(client, services):
    session_id = run_session(client, services)  # 来源是文件（非实时）
    assert client.get(f"/api/sessions/{session_id}").json()["recording"] is None
    assert client.get(f"/api/sessions/{session_id}/audio").status_code == 404


def test_login_status_and_clearing(client):
    assert client.get("/api/logins").json() == {"open": False, "saved_at": None}
    for text in ("没有链接", "rtmp://example.com/live/room"):
        response = client.post("/api/logins/open", json={"url": text})
        assert (response.status_code, response.json()["detail"]) == (400, "请先粘贴直播间的网页链接")
    save_cookies([{"name": "sessionid", "value": "1", "domain": ".douyin.com", "path": "/"}])
    assert client.get("/api/logins").json()["saved_at"] > 0
    assert client.delete("/api/logins").json() == {"open": False, "saved_at": None}
    assert load_cookies() == []


def test_recording_ids_cannot_point_outside_the_folder(client):
    response = client.get("/api/sessions/..%5Cvoicedetect/audio")
    assert (response.status_code, response.json()["detail"]) == (400, "场次 ID 无效")
