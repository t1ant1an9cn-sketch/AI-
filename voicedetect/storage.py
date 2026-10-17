"""本地数据：话术框架、历史场次（转写、窗口报告、提醒）和设置，都在一个 SQLite 文件里。

数据库版本记在 PRAGMA user_version。以后改表结构时，在 _MIGRATIONS 里追加一步即可，
旧数据会在启动时按顺序升级。
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
import json
from pathlib import Path
from typing import Any, ClassVar

from sqlalchemy import (
    JSON,
    Boolean,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    create_engine,
    delete,
    event,
    func,
    select,
    text,
    update,
)
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

from .analysis.report import Alert, LlmReview, Segment, WindowReport, now_text
from .frameworks import Framework


class Base(DeclarativeBase):
    type_annotation_map: ClassVar[dict[Any, Any]] = {dict[str, Any]: JSON, list[Any]: JSON}


class FrameworkRow(Base):
    __tablename__ = "frameworks"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    data: Mapped[dict[str, Any]]
    created_at: Mapped[str] = mapped_column(String(40))
    updated_at: Mapped[str] = mapped_column(String(40))


class SessionRow(Base):
    __tablename__ = "sessions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    title: Mapped[str] = mapped_column(String(200))
    framework_id: Mapped[str] = mapped_column(String(64))
    framework: Mapped[dict[str, Any]]  # 开播时框架的完整副本，之后改框架不影响历史
    source: Mapped[str] = mapped_column(String(500))
    engine: Mapped[str] = mapped_column(String(100), default="")
    window_seconds: Mapped[float] = mapped_column(Float)
    interval_seconds: Mapped[float] = mapped_column(Float)
    started_at: Mapped[str] = mapped_column(String(40), index=True)
    ended_at: Mapped[str | None] = mapped_column(String(40), nullable=True)
    duration: Mapped[float] = mapped_column(Float, default=0.0)
    status: Mapped[str] = mapped_column(String(20), default="running")
    error: Mapped[str] = mapped_column(Text, default="")
    summary: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)


class SegmentRow(Base):
    __tablename__ = "segments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("sessions.id"), index=True)
    seq: Mapped[int] = mapped_column(Integer)
    start: Mapped[float] = mapped_column(Float)
    end: Mapped[float] = mapped_column(Float)
    text: Mapped[str] = mapped_column(Text)
    emotion: Mapped[str | None] = mapped_column(String(40), nullable=True)


class ReportRow(Base):
    __tablename__ = "reports"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("sessions.id"), index=True)
    start: Mapped[float] = mapped_column(Float)
    end: Mapped[float] = mapped_column(Float)
    final: Mapped[bool] = mapped_column(Boolean, default=False)
    data: Mapped[dict[str, Any]]


class AlertRow(Base):
    __tablename__ = "alerts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("sessions.id"), index=True)
    time: Mapped[float] = mapped_column(Float)
    severity: Mapped[str] = mapped_column(String(10))
    data: Mapped[dict[str, Any]]


class SettingRow(Base):
    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[dict[str, Any]]


SCHEMA_VERSION = 1
# 第 n 项把数据库从版本 n 升级到 n+1。
_MIGRATIONS: list[Callable[[Engine], None]] = []


class DatabaseVersionError(RuntimeError):
    pass


class Storage:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.engine = create_engine(
            f"sqlite:///{self.path}",
            connect_args={"check_same_thread": False, "timeout": 15},
            json_serializer=lambda value: json.dumps(value, ensure_ascii=False),
        )
        event.listen(self.engine, "connect", _configure_connection)
        self._sessions = sessionmaker(self.engine, expire_on_commit=False)
        self._upgrade()

    def _upgrade(self) -> None:
        with self.engine.connect() as connection:
            version = connection.execute(text("PRAGMA user_version")).scalar_one()
        if version > SCHEMA_VERSION:
            raise DatabaseVersionError(f"数据文件来自更新版本的程序（版本 {version}），请升级程序后再打开。")
        if version == 0:
            Base.metadata.create_all(self.engine)
        else:
            for migrate in _MIGRATIONS[version - 1 :]:
                migrate(self.engine)
        with self.engine.begin() as connection:
            connection.execute(text(f"PRAGMA user_version = {SCHEMA_VERSION}"))

    @contextmanager
    def _db(self) -> Iterator[Session]:
        with self._sessions() as db, db.begin():
            yield db

    def close(self) -> None:
        self.engine.dispose()

    # —— 设置 ——

    def get_setting(self, key: str) -> dict[str, Any] | None:
        with self._db() as db:
            row = db.get(SettingRow, key)
            return dict(row.value) if row else None

    def set_setting(self, key: str, value: dict[str, Any]) -> None:
        with self._db() as db:
            row = db.get(SettingRow, key)
            if row:
                row.value = value
            else:
                db.add(SettingRow(key=key, value=value))

    # —— 话术框架 ——

    def list_frameworks(self) -> list[dict[str, Any]]:
        with self._db() as db:
            rows = db.scalars(select(FrameworkRow).order_by(FrameworkRow.updated_at.desc())).all()
            return [_framework_summary(row) for row in rows]

    def get_framework(self, framework_id: str) -> Framework | None:
        with self._db() as db:
            row = db.get(FrameworkRow, framework_id)
            return Framework.model_validate(row.data) if row else None

    def save_framework(self, framework: Framework) -> Framework:
        now = now_text()
        data = framework.model_dump(mode="json")
        with self._db() as db:
            row = db.get(FrameworkRow, framework.id)
            if row:
                row.name, row.data, row.updated_at = framework.name, data, now
            else:
                db.add(FrameworkRow(id=framework.id, name=framework.name, data=data, created_at=now, updated_at=now))
        return framework

    def delete_framework(self, framework_id: str) -> bool:
        with self._db() as db:
            return db.execute(delete(FrameworkRow).where(FrameworkRow.id == framework_id)).rowcount > 0

    def seed_templates(self, templates: list[Framework]) -> None:
        """首次见到的内置模板写入数据库；用户删掉的模板不会再被加回来。"""
        seen = set((self.get_setting("seeded_templates") or {}).get("ids", []))
        for template in templates:
            if template.id not in seen and self.get_framework(template.id) is None:
                self.save_framework(template)
            seen.add(template.id)
        self.set_setting("seeded_templates", {"ids": sorted(seen)})

    # —— 场次 ——

    def create_session(
        self,
        *,
        session_id: str,
        title: str,
        framework: Framework,
        source: str,
        window_seconds: float,
        interval_seconds: float,
        engine: str = "",
    ) -> None:
        with self._db() as db:
            db.add(
                SessionRow(
                    id=session_id,
                    title=title,
                    framework_id=framework.id,
                    framework=framework.model_dump(mode="json"),
                    source=source,
                    engine=engine,
                    window_seconds=window_seconds,
                    interval_seconds=interval_seconds,
                    started_at=now_text(),
                )
            )

    def add_segment(self, session_id: str, segment: Segment) -> None:
        with self._db() as db:
            db.add(
                SegmentRow(
                    session_id=session_id,
                    seq=segment.id,
                    start=segment.start,
                    end=segment.end,
                    text=segment.text,
                    emotion=segment.emotion,
                )
            )

    def add_report(self, report: WindowReport) -> int:
        with self._db() as db:
            row = ReportRow(
                session_id=report.session_id,
                start=report.start,
                end=report.end,
                final=report.final,
                data=report.model_dump(mode="json", exclude={"id"}),
            )
            db.add(row)
            db.flush()
            return row.id

    def set_report_llm(self, report_id: int, review: LlmReview) -> None:
        with self._db() as db:
            row = db.get(ReportRow, report_id)
            if row:
                row.data = {**row.data, "llm": review.model_dump(mode="json")}

    def add_alert(self, alert: Alert) -> int:
        with self._db() as db:
            row = AlertRow(
                session_id=alert.session_id,
                time=alert.time,
                severity=alert.severity,
                data=alert.model_dump(mode="json", exclude={"id"}),
            )
            db.add(row)
            db.flush()
            return row.id

    def finish_session(
        self, session_id: str, *, status: str, duration: float, summary: WindowReport | None, error: str
    ) -> None:
        with self._db() as db:
            row = db.get(SessionRow, session_id)
            if row:
                row.status = status
                row.duration = duration
                row.error = error
                row.ended_at = now_text()
                row.summary = summary.model_dump(mode="json") if summary else None

    def mark_interrupted(self) -> int:
        """程序上次异常退出时仍标记为进行中的场次，改为“已中断”。"""
        with self._db() as db:
            result = db.execute(update(SessionRow).where(SessionRow.status == "running").values(status="interrupted"))
            return result.rowcount

    def list_sessions(self, *, limit: int = 50, offset: int = 0) -> tuple[list[dict[str, Any]], int]:
        with self._db() as db:
            total = db.scalar(select(func.count()).select_from(SessionRow)) or 0
            rows = db.scalars(
                select(SessionRow).order_by(SessionRow.started_at.desc()).limit(limit).offset(offset)
            ).all()
            alert_counts = dict(
                db.execute(
                    select(AlertRow.session_id, func.count())
                    .where(AlertRow.session_id.in_([row.id for row in rows]))
                    .group_by(AlertRow.session_id)
                ).all()
            )
            return [_session_summary(row, alert_counts.get(row.id, 0)) for row in rows], total

    def get_session(self, session_id: str) -> dict[str, Any] | None:
        with self._db() as db:
            row = db.get(SessionRow, session_id)
            if row is None:
                return None
            segments = db.scalars(
                select(SegmentRow).where(SegmentRow.session_id == session_id).order_by(SegmentRow.start)
            ).all()
            reports = db.scalars(
                select(ReportRow).where(ReportRow.session_id == session_id).order_by(ReportRow.end)
            ).all()
            alerts = db.scalars(
                select(AlertRow).where(AlertRow.session_id == session_id).order_by(AlertRow.time, AlertRow.id)
            ).all()
            detail = _session_summary(row, len(alerts))
            detail.update(
                framework=row.framework,
                summary=row.summary,
                segments=[
                    {"id": s.seq, "start": s.start, "end": s.end, "text": s.text, "emotion": s.emotion}
                    for s in segments
                ],
                reports=[{**r.data, "id": r.id} for r in reports],
                alerts=[{**a.data, "id": a.id} for a in alerts],
            )
            return detail

    def delete_session(self, session_id: str) -> bool:
        with self._db() as db:
            for table in (SegmentRow, ReportRow, AlertRow):
                db.execute(delete(table).where(table.session_id == session_id))
            return db.execute(delete(SessionRow).where(SessionRow.id == session_id)).rowcount > 0


def _configure_connection(connection, _record) -> None:
    cursor = connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")  # 写转写时，界面仍能读历史
    cursor.execute("PRAGMA synchronous=NORMAL")
    cursor.close()


def _framework_summary(row: FrameworkRow) -> dict[str, Any]:
    data = row.data
    points = sum(len(stage.get("points", [])) for stage in data.get("stages", []))
    return {
        "id": row.id,
        "name": row.name,
        "description": data.get("description", ""),
        "stage_count": len(data.get("stages", [])),
        "point_count": points,
        "forbidden_count": sum(len(rule.get("keywords", [])) for rule in data.get("forbidden", [])),
        "updated_at": row.updated_at,
    }


def _session_summary(row: SessionRow, alert_count: int) -> dict[str, Any]:
    findings = (row.summary or {}).get("findings", [])
    return {
        "id": row.id,
        "title": row.title,
        "framework_id": row.framework_id,
        "framework_name": row.framework.get("name", ""),
        "source": row.source,
        "engine": row.engine,
        "window_seconds": row.window_seconds,
        "interval_seconds": row.interval_seconds,
        "started_at": row.started_at,
        "ended_at": row.ended_at,
        "duration": row.duration,
        "status": row.status,
        "error": row.error,
        "alert_count": alert_count,
        "finding_count": len(findings),
    }
