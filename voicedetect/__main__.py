"""命令入口。

voicedetect                      打开桌面窗口
voicedetect --browser            在浏览器中打开（不使用桌面窗口）
voicedetect serve --token X      只启动本地服务（前端开发用）
voicedetect analyze 录音.mp3     不开界面，直接分析一个音频文件
voicedetect devices              列出可采集的音频设备
voicedetect models download      下载本地识别模型
"""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path
import sys
import uuid


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")  # Windows 终端默认编码打不出中文
    parser = argparse.ArgumentParser(prog="voicedetect", description="AI 盯播助手")
    parser.add_argument("--browser", action="store_true", help="在浏览器中打开界面")
    parser.add_argument("--port", type=int, default=0, help="本地服务端口，默认随机")
    parser.add_argument("--debug", action="store_true", help="输出调试日志")
    commands = parser.add_subparsers(dest="command")

    serve = commands.add_parser("serve", help="只启动本地服务")
    serve.add_argument("--port", type=int, default=8765)
    serve.add_argument("--token", required=True, help="接口访问令牌，前端需带上同一个令牌")

    analyze = commands.add_parser("analyze", help="分析一个音频文件")
    analyze.add_argument("file", help="音频或视频文件，也可以是直播流地址")
    analyze.add_argument("--framework", default="tpl-general", help="话术框架 ID 或 JSON 文件路径")
    analyze.add_argument("--window", type=float, default=300, help="每次分析过去多少秒（默认 300）")
    analyze.add_argument("--interval", type=float, default=60, help="每隔多少秒分析一次（默认 60）")
    analyze.add_argument("--realtime", action="store_true", help="按实际速度播放，模拟盯直播")
    analyze.add_argument("--report", type=Path, help="把复盘报告写入这个 Markdown 文件")
    analyze.add_argument("--json", type=Path, help="把全部结果写入这个 JSON 文件")
    analyze.add_argument("--title", default="", help="场次名称")

    commands.add_parser("devices", help="列出可采集的音频设备")

    models = commands.add_parser("models", help="管理本地识别模型")
    model_actions = models.add_subparsers(dest="action", required=True)
    download = model_actions.add_parser("download", help="下载模型")
    download.add_argument("--source", choices=["auto", "hf-mirror", "huggingface"], default="auto")
    importer = model_actions.add_parser("import", help="从文件夹导入已下载的模型文件")
    importer.add_argument("folder", type=Path)
    model_actions.add_parser("status", help="查看模型状态")

    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.WARNING, format="%(levelname)s %(name)s: %(message)s"
    )
    try:
        if args.command == "serve":
            from .desktop import serve_forever

            return serve_forever(port=args.port, token=args.token)
        if args.command == "analyze":
            return _analyze(args)
        if args.command == "devices":
            return _devices()
        if args.command == "models":
            return _models(args)
        from .desktop import run_app

        return run_app(browser=args.browser, port=args.port)
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        if args.debug:
            raise
        print(f"错误：{exc}", file=sys.stderr)
        return 1


def _analyze(args: argparse.Namespace) -> int:
    from .analysis.report import Alert
    from .app import Services
    from .export import clock, session_markdown
    from .frameworks import Framework
    from .monitor.session import SessionOptions

    services = Services.create()
    framework_path = Path(args.framework)
    if framework_path.suffix == ".json" and framework_path.is_file():
        framework = Framework.model_validate_json(framework_path.read_text(encoding="utf-8"))
    else:
        framework = services.storage.get_framework(args.framework)
        if framework is None:
            raise SystemExit(f"找不到话术框架：{args.framework}")

    def show(event: dict) -> None:
        kind = event["type"]
        if kind == "segment":
            segment = event["segment"]
            print(f"[{clock(segment['start'])}] {segment['text']}")
        elif kind == "alert":
            alert = Alert.model_validate(event["alert"])
            print(f"  ⚠ [{clock(alert.time)}] {alert.title}：{alert.detail}")
        elif kind == "state" and event["state"] == "failed":
            print(f"失败：{event['error']}", file=sys.stderr)

    session = services.monitor.start_file(
        session_id=uuid.uuid4().hex[:12],
        title=args.title or Path(args.file).name,
        framework=framework,
        location=args.file,
        realtime=args.realtime,
        options=SessionOptions(window_seconds=args.window, interval_seconds=args.interval),
        listener=show,
    )
    try:
        session.wait()
    except KeyboardInterrupt:
        session.stop()
        session.wait()
    detail = services.storage.get_session(session.session_id)
    print()
    print(f"共识别 {len(detail['segments'])} 句，提醒 {len(detail['alerts'])} 条。历史记录里可以查看这一场。")
    if args.report:
        args.report.write_text(session_markdown(detail), encoding="utf-8")
        print(f"复盘报告：{args.report}")
    if args.json:
        args.json.write_text(json.dumps(detail, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"完整结果：{args.json}")
    return 0 if session.state == "stopped" else 1


def _devices() -> int:
    from .audio.devices import list_devices

    kinds = {"loopback": "系统声音", "microphone": "麦克风"}
    for device in list_devices():
        mark = "（默认）" if device["default"] else ""
        print(f"{kinds[device['kind']]}  {device['name']}{mark}\n    ID: {device['id']}")
    return 0


def _models(args: argparse.Namespace) -> int:
    from .asr.models import SENSEVOICE, ModelStore
    from .config import models_dir

    store = ModelStore(models_dir(), SENSEVOICE)
    if args.action == "status":
        print(f"{SENSEVOICE.name}：{'已安装' if store.installed() else '未安装'}\n位置：{store.path}")
        return 0
    if args.action == "import":
        store.import_from(args.folder)
        print("导入完成")
        return 0

    def progress(done: int, total: int) -> None:
        print(f"\r下载中 {done / total:6.1%}  {done / 1e6:6.1f}/{total / 1e6:.1f} MB", end="", flush=True)

    store.download(source=args.source, progress=progress)
    print("\n下载完成")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
