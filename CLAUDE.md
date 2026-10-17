# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目

AI 盯播助手：桌面工具，实时识别主播讲解（本地 SenseVoice 或讯飞实时转写），按话术框架每隔 N 秒分析过去 M 秒，提醒违规表达、冷场、漏讲要点和节奏问题；可选大模型语义审核。用户文档见 `README.md`，模块分工和设计取舍见 `docs/architecture.md`，协作规则见 `AGENTS.md`（必须遵守：讨论不等于实施授权、汇报实际运行的命令和结果、代码完成后用中文解释、提交说明写实际结果）。文档、注释、docstring、界面文案和提交说明都用中文。

## 常用命令

在仓库根目录运行。Bash 工具是 Git Bash，Python 用 `uv run` 或 `.venv/Scripts/python.exe`；不要用裸 `python`，PATH 上的是 Anaconda。

```bash
uv sync                                    # 安装依赖（含 dev 组：pytest、ruff）
uv run pytest -q                           # 全部后端测试
uv run pytest -q tests/test_window.py::test_fast_and_slow_speech_rate   # 单个测试
uv run ruff check . && uv run ruff format --check .
cd frontend && npm test && npm run typecheck && npm run build   # 前端测试、类型检查、构建到 voicedetect/web/

uv run voicedetect                         # 桌面窗口（pywebview）
uv run voicedetect --browser               # 浏览器打开
uv run voicedetect serve --token dev       # 只起本地服务（8765），配合 `npm run dev` 开发界面
uv run voicedetect analyze voicedetect/assets/demo/thermos-demo.ogg --framework tpl-thermos-cup --window 60 --interval 20
uv run voicedetect models download|status|import <dir>
```

- `analyze` 和桌面程序都会把场次写入数据目录。测试或试跑时设 `VOICEDETECT_HOME` 指到临时目录，避免污染用户的历史记录；`VOICEDETECT_MODELS` 可单独指向已下载的模型。
- `tests/conftest.py` 自动把每个测试的数据目录换成临时目录，模型目录沿用本机已下载的那份。`test_sensevoice.py` 在没有模型时自动跳过。

## 架构要点

- **分层**：`analysis/`（纯逻辑，不碰设备/模型/数据库）← `asr/`、`audio/`（外部边界）← `monitor/`（会话编排）← `api.py` / `desktop.py` / `__main__.py`（入口）。`storage.py` 和 `config.py` 被入口和 `app.Services` 组装。
- **会话**（`monitor/session.py`）：采集线程 → 有界队列 → 处理线程（识别、逐句 `inspect`、按音频时间调度窗口分析、即时提醒）→ 大模型审核线程。时间一律按已处理的音频样本数计，不看墙上时钟，所以回放文件和实时采集共用一套逻辑。
- **识别接口**（`asr/base.py`）：`accept(samples) -> [AsrEvent]`、`flush()`、`speech_since`；事件分 `partial` 和 `final`，`final` 文字为空表示有人说话但没认出字（只用于停顿和说话时间）。
- **事件**：后台线程经 `EventBus` 推给 WebSocket；界面用 `frontend/src/live.ts` 的 `applyEvent` 合并，重连时先收 `hello` 快照（`MonitorSession.snapshot()`）。新增事件或快照字段时，同时改 `frontend/src/types.ts`。
- **数据模型**：`analysis/report.py` 和 `frameworks.py` 的 Pydantic 模型是接口、数据库 JSON 和前端类型的共同来源。数据库升级在 `storage.py` 的 `_MIGRATIONS` 追加函数并提高 `SCHEMA_VERSION`。

## 测试约定

- 外部边界才替换：`tests/fakes.py` 的 `ScriptedRecognizer`（按脚本在指定音频时间输出句子）、`BlockSource`（按块输出音频，可模拟实时节奏和设备故障）、`MemoryStore`；讯飞用本地 WebSocket 模拟服务器，大模型和模型下载用 `httpx.MockTransport`。其余代码真实运行。
- 断言具体数值和原文证据，不只断言“有结果”。修缺陷先写能失败的测试。
- 真实服务（讯飞、百炼）不进自动化测试；需要验证时写一次性脚本放 `.local/`（已被 git 忽略），密钥从 `.local/keys.env` 读取，绝不打印或写进代码。

## Windows 与编码

- 打印中文的子进程设置 `PYTHONIOENCODING=utf-8` 或用 `python -X utf8`（注意 `-I` 会忽略 `PYTHON*` 环境变量）。
- 含中文的文件用 Edit/Write 工具写，不要经 shell 或 PowerShell 的 stdin 传输。
- `soundcard` 在 Windows 导入时初始化 COM 多线程套间，桌面程序在主线程先导入它（`desktop._prepare_audio`）。
- `sherpa-onnx` 必须和同版本的 `sherpa-onnx-core` 一起安装（后者提供 onnxruntime 原生库），`pyproject.toml` 里两者固定同一版本。

## Git

- 暂存时列出具体路径或用 `git add -u`，提交前看 `git status`；`.local/`、`local-recordings/`、`.idea/`、`voicedetect/web/`、`frontend/node_modules/` 都被忽略，不要强制添加。
- 仓库公开，提交前确认没有密钥、真实直播录音或转写。
