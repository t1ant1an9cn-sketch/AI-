import sqlite3

import pytest

from tests.fakes import BlockSource, Events, ScriptedRecognizer
from voicedetect.analysis.report import Alert, LlmReview, Segment
from voicedetect.frameworks import Framework, Point, Stage, builtin_templates
from voicedetect.monitor.session import MonitorSession, SessionOptions
from voicedetect.storage import DatabaseVersionError, Storage


@pytest.fixture
def storage(tmp_path):
    store = Storage(tmp_path / "data.db")
    yield store
    store.close()


def test_templates_are_seeded_once_and_deleted_ones_stay_deleted(storage):
    templates = builtin_templates()
    storage.seed_templates(templates)
    assert {item["id"] for item in storage.list_frameworks()} == {"tpl-general", "tpl-thermos-cup"}
    assert storage.delete_framework("tpl-general")
    storage.seed_templates(templates)
    assert [item["id"] for item in storage.list_frameworks()] == ["tpl-thermos-cup"]


def test_framework_save_update_and_summary(storage):
    framework = Framework(
        id="f1", name="我的框架", stages=[Stage(name="产品", points=[Point(name="材质", keywords=["钢"])])]
    )
    storage.save_framework(framework)
    storage.save_framework(framework.model_copy(update={"name": "改过的名字"}))
    (summary,) = storage.list_frameworks()
    assert (summary["name"], summary["stage_count"], summary["point_count"]) == ("改过的名字", 1, 1)
    assert storage.get_framework("f1").stages[0].points[0].keywords == ["钢"]
    assert storage.get_framework("missing") is None


def test_settings_round_trip(storage):
    assert storage.get_setting("app") is None
    storage.set_setting("app", {"llm": {"model": "qwen-plus"}})
    storage.set_setting("app", {"llm": {"model": "deepseek-chat"}})
    assert storage.get_setting("app") == {"llm": {"model": "deepseek-chat"}}


def test_a_real_session_is_persisted_and_can_be_deleted(storage):
    framework = builtin_templates()[1]
    storage.create_session(
        session_id="s1",
        title="周五晚场",
        framework=framework,
        source="测试音频",
        window_seconds=60,
        interval_seconds=30,
    )
    session = MonitorSession(
        session_id="s1",
        title="周五晚场",
        framework=framework,
        source=BlockSource(70),
        recognizer_factory=lambda: ScriptedRecognizer(
            [(1, 5, "欢迎宝宝们，杯子是316不锈钢的"), (40, 44, "全网最低价")]
        ),
        emit=Events(),
        options=SessionOptions(window_seconds=60, interval_seconds=30),
        store=storage,
    )
    session.start()
    assert session.wait(10)

    detail = storage.get_session("s1")
    assert (detail["status"], detail["duration"], detail["framework_name"]) == ("stopped", 70, "保温杯讲解（示例）")
    assert [s["text"] for s in detail["segments"]] == ["欢迎宝宝们，杯子是316不锈钢的", "全网最低价"]
    assert [(r["start"], r["end"]) for r in detail["reports"]] == [(0, 30), (0, 60), (10, 70)]
    assert detail["reports"][0]["id"] == 1
    assert detail["summary"]["final"] is True
    assert any(a["kind"] == "forbidden" for a in detail["alerts"])

    sessions, total = storage.list_sessions()
    assert total == 1 and sessions[0]["alert_count"] == len(detail["alerts"]) > 0

    assert storage.delete_session("s1")
    assert storage.get_session("s1") is None
    with sqlite3.connect(storage.path) as raw:
        assert raw.execute("select count(*) from segments").fetchone() == (0,)


def test_llm_review_is_merged_into_the_stored_report(storage):
    framework = Framework(name="x")
    storage.create_session(
        session_id="s1", title="t", framework=framework, source="x", window_seconds=60, interval_seconds=30
    )
    session = MonitorSession(
        session_id="s1",
        title="t",
        framework=framework,
        source=BlockSource(31),
        recognizer_factory=lambda: ScriptedRecognizer([(1, 2, "你好")]),
        emit=Events(),
        options=SessionOptions(window_seconds=60, interval_seconds=30),
        store=storage,
    )
    session.start()
    assert session.wait(10)
    report_id = storage.get_session("s1")["reports"][0]["id"]
    storage.set_report_llm(report_id, LlmReview(status="done", summary="不错"))
    assert storage.get_session("s1")["reports"][0]["llm"]["summary"] == "不错"


def test_interrupted_sessions_are_marked_on_next_start(storage):
    storage.create_session(
        session_id="s1", title="t", framework=Framework(name="x"), source="x", window_seconds=60, interval_seconds=30
    )
    storage.add_segment("s1", Segment(id=1, start=0, end=1, text="你好"))
    storage.add_alert(Alert(session_id="s1", time=0, source="system", kind="x", severity="low", title="t", detail="d"))
    assert storage.mark_interrupted() == 1
    assert storage.get_session("s1")["status"] == "interrupted"


def test_newer_database_is_refused(tmp_path):
    path = tmp_path / "data.db"
    Storage(path).close()
    with sqlite3.connect(path) as raw:
        raw.execute("PRAGMA user_version = 99")
    with pytest.raises(DatabaseVersionError, match="更新版本"):
        Storage(path)
