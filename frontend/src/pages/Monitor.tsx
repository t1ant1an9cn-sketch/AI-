import {
  Bot,
  Download,
  FileAudio,
  FolderOpen,
  History as HistoryIcon,
  Link,
  LoaderCircle,
  Mic,
  Play,
  Plus,
  RefreshCw,
  Sparkles,
  Speaker,
  Square,
} from "lucide-react";
import { type ReactNode, useEffect, useRef, useState } from "react";

import { api, desktop } from "../api";
import { AlertList, CoverageList, FindingList, LlmPanel, MetricTiles } from "../components/Report";
import { RateChart } from "../components/RateChart";
import { Transcript, TranscriptLegend } from "../components/Transcript";
import { Empty, Meter, Notice, Segmented, Status } from "../components/ui";
import { useApp } from "../context";
import { clock, megabytes, span } from "../format";
import { unlockSound } from "../sound";
import type { Device, FrameworkSummary, ModelStatus, Snapshot, SourceSpec, WindowReport } from "../types";

type SourceKind = "loopback" | "microphone" | "file" | "url" | "demo";

const SOURCES: { value: SourceKind; label: string; icon: ReactNode }[] = [
  { value: "loopback", label: "系统声音", icon: <Speaker size={15} /> },
  { value: "microphone", label: "麦克风", icon: <Mic size={15} /> },
  { value: "file", label: "音频文件", icon: <FileAudio size={15} /> },
  { value: "url", label: "直播流地址", icon: <Link size={15} /> },
  { value: "demo", label: "演示音频", icon: <Sparkles size={15} /> },
];
const WINDOWS = [60, 120, 180, 300, 600, 900, 1800];
const INTERVALS = [15, 30, 60, 120, 300];
const STREAM = /^(https?|rtmps?|rtsp|srt):\/\//i;

export function MonitorPage() {
  const { live } = useApp();
  const [choosing, setChoosing] = useState(false);
  if (!live.session || choosing) return <Setup onStarted={() => setChoosing(false)} />;
  return <LiveView session={live.session} level={live.level} onNewSession={() => setChoosing(true)} />;
}

function withCurrent(choices: number[], value: number): number[] {
  return choices.includes(value) ? choices : [...choices, value].sort((a, b) => a - b);
}

function basename(path: string): string {
  return path.split(/[\\/]/).pop() || path;
}

function Setup({ onStarted }: { onStarted(): void }) {
  const { settings, info, live, setSession, navigate } = useApp();
  const [frameworks, setFrameworks] = useState<FrameworkSummary[] | null>(null);
  const [frameworkId, setFrameworkId] = useState(() => localStorage.getItem("last-framework") ?? "");
  const [kind, setKind] = useState<SourceKind>(() => (localStorage.getItem("last-source") as SourceKind) || "demo");
  const [devices, setDevices] = useState<Device[] | null>(null);
  const [deviceError, setDeviceError] = useState("");
  const [deviceChoice, setDeviceChoice] = useState<Record<"loopback" | "microphone", string>>({
    loopback: "",
    microphone: "",
  });
  const [file, setFile] = useState<{ path: string; name: string } | null>(null);
  const [uploading, setUploading] = useState(false);
  const [fileRealtime, setFileRealtime] = useState(false);
  const [demoRealtime, setDemoRealtime] = useState(true);
  const [url, setUrl] = useState("");
  const [windowSeconds, setWindowSeconds] = useState(300);
  const [intervalSeconds, setIntervalSeconds] = useState(60);
  const [title, setTitle] = useState("");
  const [model, setModel] = useState<ModelStatus | null>(null);
  const [starting, setStarting] = useState(false);
  const [error, setError] = useState("");
  const fileInput = useRef<HTMLInputElement>(null);

  useEffect(() => {
    api
      .frameworks()
      .then((items) => {
        setFrameworks(items);
        setFrameworkId((current) =>
          items.some((item) => item.id === current)
            ? current
            : (items.find((item) => item.id === "tpl-thermos-cup") ?? items[0])?.id ?? "",
        );
      })
      .catch((reason: Error) => setError(reason.message));
    api.model().then(setModel).catch(() => undefined);
  }, []);

  useEffect(() => {
    if (!settings) return;
    setWindowSeconds(settings.monitor.window_seconds);
    setIntervalSeconds(settings.monitor.interval_seconds);
  }, [settings]);

  const loadDevices = () => {
    setDeviceError("");
    api
      .devices()
      .then((items) => {
        setDevices(items);
        setDeviceChoice((current) => {
          const pick = (type: Device["kind"]) =>
            items.find((d) => d.id === current[type] && d.kind === type)?.id ??
            (items.find((d) => d.kind === type && d.default) ?? items.find((d) => d.kind === type))?.id ??
            "";
          return { loopback: pick("loopback"), microphone: pick("microphone") };
        });
      })
      .catch((reason: Error) => setDeviceError(reason.message));
  };

  useEffect(() => {
    if ((kind === "loopback" || kind === "microphone") && devices === null) loadDevices();
  }, [kind]);

  const modelStatus = live.model ?? model;
  const engine = settings?.asr.engine ?? "sensevoice";
  const engineReady =
    engine === "xfyun" ? Boolean(settings?.asr.xfyun_app_id && settings?.asr.xfyun_api_key) : Boolean(modelStatus?.installed);
  const listed = (devices ?? []).filter((device) => device.kind === kind);

  let sourceProblem = "";
  if (kind === "loopback" || kind === "microphone") {
    if (devices !== null && !listed.length) sourceProblem = kind === "loopback" ? "没有找到可采集的播放设备" : "没有找到麦克风";
  } else if (kind === "file" && !file) sourceProblem = "请先选择音频文件";
  else if (kind === "url" && !STREAM.test(url.trim())) sourceProblem = "请填写以 http(s)://、rtmp:// 等开头的直播流地址";

  const canStart =
    !starting && engineReady && Boolean(frameworkId) && !sourceProblem && intervalSeconds <= windowSeconds && !uploading;

  const chooseFile = async () => {
    const bridge = desktop();
    if (bridge) {
      const path = await bridge.pick_audio_file();
      if (path) setFile({ path, name: basename(path) });
    } else {
      fileInput.current?.click();
    }
  };

  const upload = async (picked: File | undefined) => {
    if (!picked) return;
    setUploading(true);
    setError("");
    try {
      setFile(await api.upload(picked));
    } catch (reason) {
      setError((reason as Error).message);
    } finally {
      setUploading(false);
    }
  };

  const start = async () => {
    unlockSound();
    setStarting(true);
    setError("");
    let source: SourceSpec;
    if (kind === "loopback" || kind === "microphone") {
      const device = listed.find((d) => d.id === deviceChoice[kind]);
      source = { kind: "device", device_id: device?.id ?? null, device_name: device?.name ?? "", loopback: kind === "loopback" };
    } else if (kind === "file") source = { kind: "file", path: file!.path, realtime: fileRealtime };
    else if (kind === "url") source = { kind: "url", url: url.trim() };
    else source = { kind: "demo", realtime: demoRealtime };
    try {
      const snapshot = await api.start({
        framework_id: frameworkId,
        title: title.trim(),
        source,
        window_seconds: windowSeconds,
        interval_seconds: intervalSeconds,
      });
      localStorage.setItem("last-framework", frameworkId);
      localStorage.setItem("last-source", kind);
      setSession(snapshot);
      onStarted();
    } catch (reason) {
      setError((reason as Error).message);
    } finally {
      setStarting(false);
    }
  };

  const selected = frameworks?.find((item) => item.id === frameworkId);

  return (
    <div className="page">
      <div className="setup">
        <section className="card">
          <div className="card-head">
            <h2>开始盯播</h2>
          </div>
          <div className="card-body stack loose">
            <div className="field">
              <div className="row between">
                <label htmlFor="framework">话术框架</label>
                <button className="link-btn small" type="button" onClick={() => navigate("/frameworks")}>
                  管理话术框架
                </button>
              </div>
              <select
                id="framework"
                className="select"
                value={frameworkId}
                onChange={(event) => setFrameworkId(event.target.value)}
                disabled={!frameworks}
              >
                {!frameworks && <option>加载中……</option>}
                {frameworks?.map((item) => (
                  <option key={item.id} value={item.id}>
                    {item.name}
                  </option>
                ))}
              </select>
              {selected && (
                <span className="help">
                  {selected.stage_count} 个环节、{selected.point_count} 个要点、{selected.forbidden_count} 个违规词
                  {selected.description ? ` · ${selected.description}` : ""}
                </span>
              )}
              {frameworks?.length === 0 && (
                <Notice tone="warning">
                  还没有话术框架，
                  <button className="link-btn" type="button" onClick={() => navigate("/frameworks")}>
                    先去新建一个
                  </button>
                  。
                </Notice>
              )}
            </div>

            <div className="field">
              <span className="field-label">音频来源</span>
              <Segmented<SourceKind> label="音频来源" value={kind} options={SOURCES} onChange={setKind} />
              <div className="source-pane">
                {(kind === "loopback" || kind === "microphone") && (
                  <>
                    {kind === "loopback" && info && !info.loopback_supported && (
                      <Notice tone="warning">
                        macOS 不能直接采集系统声音。请安装 BlackHole 等虚拟声卡，把直播间声音输出到它，再在“麦克风”里选择它。
                      </Notice>
                    )}
                    <div className="row">
                      <select
                        className="select grow"
                        aria-label="选择设备"
                        value={deviceChoice[kind]}
                        onChange={(event) => setDeviceChoice({ ...deviceChoice, [kind]: event.target.value })}
                        disabled={!devices || !listed.length}
                      >
                        {devices === null && <option>正在读取设备……</option>}
                        {listed.map((device) => (
                          <option key={device.id} value={device.id}>
                            {device.name}
                            {device.default ? "（默认）" : ""}
                          </option>
                        ))}
                      </select>
                      <button className="icon-btn" type="button" title="重新读取设备" onClick={loadDevices}>
                        <RefreshCw size={16} />
                      </button>
                    </div>
                    {deviceError && <Notice tone="critical">{deviceError}</Notice>}
                    <span className="help secondary small">
                      {kind === "loopback"
                        ? "先在浏览器或直播伴侣里打开要盯的直播间，确认声音从这个设备播放出来。注意：电脑上的其他声音也会被一起识别。"
                        : "适合主播在自己的电脑上使用，直接采集主播的声音。"}
                    </span>
                  </>
                )}
                {kind === "file" && (
                  <>
                    <div className="row">
                      <button className="btn" type="button" onClick={chooseFile} disabled={uploading}>
                        {uploading ? <LoaderCircle size={15} className="spin" /> : <FolderOpen size={15} />}
                        {uploading ? "正在读取……" : "选择文件"}
                      </button>
                      <span className="grow secondary nowrap" style={{ overflow: "hidden", textOverflow: "ellipsis" }}>
                        {file ? file.name : "支持 mp3、wav、m4a、flac，以及 mp4、flv 等视频文件"}
                      </span>
                    </div>
                    <input
                      ref={fileInput}
                      type="file"
                      accept="audio/*,video/*,.flv,.ts,.mkv"
                      hidden
                      onChange={(event) => upload(event.target.files?.[0])}
                    />
                    <label className="check">
                      <input
                        type="checkbox"
                        checked={fileRealtime}
                        onChange={(event) => setFileRealtime(event.target.checked)}
                      />
                      按实际速度播放（模拟盯直播；不勾选则尽快分析完，适合复盘录播）
                    </label>
                  </>
                )}
                {kind === "url" && (
                  <>
                    <input
                      className="input"
                      aria-label="直播流地址"
                      placeholder="https://…/live.flv 或 .m3u8 地址"
                      value={url}
                      onChange={(event) => setUrl(event.target.value)}
                    />
                    <span className="help secondary small">
                      需要直播流的直接地址（flv、m3u8、rtmp），直播间网页地址不能直接使用。实验功能，不同平台的地址需要逐个验证。
                    </span>
                  </>
                )}
                {kind === "demo" && (
                  <>
                    <span>
                      内置一段约 3 分钟的保温杯带货讲解（电脑合成语音），其中故意包含违规词、站外导流、冷场和漏讲的要点，用来体验各种提醒。
                    </span>
                    <span className="help secondary small">建议搭配“保温杯讲解（示例）”框架，分析窗口选 1 分钟。</span>
                    <label className="check">
                      <input
                        type="checkbox"
                        checked={demoRealtime}
                        onChange={(event) => setDemoRealtime(event.target.checked)}
                      />
                      按实际速度播放（不勾选则几秒内分析完）
                    </label>
                  </>
                )}
              </div>
            </div>

            <div className="field">
              <label htmlFor="title">场次名称（可选）</label>
              <input
                id="title"
                className="input"
                placeholder="例如：周五晚场·保温杯专场"
                value={title}
                maxLength={100}
                onChange={(event) => setTitle(event.target.value)}
              />
            </div>

            {error && <Notice tone="critical">{error}</Notice>}
            <div className="row">
              <button className="btn primary large" type="button" disabled={!canStart} onClick={start}>
                {starting ? <LoaderCircle size={17} className="spin" /> : <Play size={17} />}
                {starting ? "正在启动……" : "开始盯播"}
              </button>
              {sourceProblem && devices !== null && <span className="muted small">{sourceProblem}</span>}
            </div>
          </div>
        </section>

        <div className="stack">
          <section className="card">
            <div className="card-head">
              <h3>分析方式</h3>
            </div>
            <div className="card-body stack">
              <div className="grid-2">
                <div className="field">
                  <label htmlFor="interval">每隔</label>
                  <select
                    id="interval"
                    className="select"
                    value={intervalSeconds}
                    onChange={(event) => setIntervalSeconds(Number(event.target.value))}
                  >
                    {withCurrent(INTERVALS, intervalSeconds).map((value) => (
                      <option key={value} value={value}>
                        {span(value)}
                      </option>
                    ))}
                  </select>
                </div>
                <div className="field">
                  <label htmlFor="window">分析过去</label>
                  <select
                    id="window"
                    className="select"
                    value={windowSeconds}
                    onChange={(event) => setWindowSeconds(Number(event.target.value))}
                  >
                    {withCurrent(WINDOWS, windowSeconds).map((value) => (
                      <option key={value} value={value}>
                        {span(value)}
                      </option>
                    ))}
                  </select>
                </div>
              </div>
              {intervalSeconds > windowSeconds ? (
                <Notice tone="warning">分析间隔不能大于分析窗口。</Notice>
              ) : (
                <p className="secondary">
                  每 {span(intervalSeconds)}对过去 {span(windowSeconds)}的讲解做一次分析：语速、冷场、说话占比、口头禅、要点有没有讲到。违规表达和冷场不等分析，出现时立即提醒。
                </p>
              )}
            </div>
          </section>

          <section className="card">
            <div className="card-head">
              <h3>识别与审核</h3>
              <button className="link-btn small" type="button" onClick={() => navigate("/settings")}>
                设置
              </button>
            </div>
            <div className="card-body stack">
              <EngineStatus
                engine={engine}
                model={modelStatus}
                ready={engineReady}
                onDownload={() => api.downloadModel().then(setModel).catch((reason: Error) => setError(reason.message))}
                onSettings={() => navigate("/settings")}
              />
              <div className="row">
                <Bot size={16} aria-hidden />
                <span className="grow">大模型语义审核</span>
                {settings?.llm.enabled ? (
                  <Status tone="good">已开启（{settings.llm.model}）</Status>
                ) : (
                  <Status tone="neutral">未开启</Status>
                )}
              </div>
              {!settings?.llm.enabled && (
                <span className="help secondary small">
                  只靠关键词也能工作。开启后，大模型会额外判断说错价格、卖点讲得含糊等关键词发现不了的问题。
                </span>
              )}
            </div>
          </section>
        </div>
      </div>
    </div>
  );
}

function EngineStatus({
  engine,
  model,
  ready,
  onDownload,
  onSettings,
}: {
  engine: "sensevoice" | "xfyun";
  model: ModelStatus | null;
  ready: boolean;
  onDownload(): void;
  onSettings(): void;
}) {
  if (engine === "xfyun") {
    return (
      <div className="row">
        <span className="grow">语音识别：讯飞实时转写（云端）</span>
        {ready ? (
          <Status tone="good">已配置</Status>
        ) : (
          <button className="btn small" type="button" onClick={onSettings}>
            填写账号
          </button>
        )}
      </div>
    );
  }
  return (
    <div className="stack tight">
      <div className="row">
        <span className="grow">语音识别：SenseVoice（本地，不联网）</span>
        {ready && <Status tone="good">模型已就绪</Status>}
        {!ready && model && !model.downloading && (
          <button className="btn small primary" type="button" onClick={onDownload}>
            <Download size={14} />
            下载模型（{megabytes(model.size)}）
          </button>
        )}
      </div>
      {model?.downloading && (
        <>
          <Meter value={model.total ? model.done / model.total : 0} label="模型下载进度" tall />
          <span className="muted small tabular">
            正在下载 {megabytes(model.done)} / {megabytes(model.total)}
          </span>
        </>
      )}
      {!ready && model?.error && <Notice tone="critical">{model.error}</Notice>}
      {!ready && !model?.downloading && (
        <span className="help secondary small">第一次使用需要下载识别模型，下载后完全在本机运行。</span>
      )}
    </div>
  );
}

const STATE_LABEL: Record<Snapshot["state"], string> = {
  starting: "正在启动",
  running: "盯播中",
  stopping: "正在收尾",
  stopped: "已结束",
  failed: "出错了",
};

function LiveView({ session, level, onNewSession }: { session: Snapshot; level: number; onNewSession(): void }) {
  const { settings, navigate, setSession } = useApp();
  const [stopping, setStopping] = useState(false);
  const [choice, setChoice] = useState<string>("latest");
  const running = session.state === "starting" || session.state === "running";
  const finished = session.state === "stopped" || session.state === "failed";
  const now = session.health.audio_time;
  const pace = session.framework.pace;

  useEffect(() => setChoice(finished && session.summary ? "summary" : "latest"), [finished, session.summary]);

  const stop = async () => {
    setStopping(true);
    try {
      const snapshot = await api.stop();
      if (snapshot) setSession(snapshot);
    } finally {
      setStopping(false);
    }
  };

  const latest = session.reports[session.reports.length - 1] ?? null;
  const report: WindowReport | null =
    choice === "summary"
      ? session.summary
      : choice === "latest"
        ? latest
        : (session.reports.find((r) => String(r.id) === choice) ?? latest);
  const levelRatio = Math.max(0, Math.min(1, (level + 60) / 60));

  return (
    <div className="page fill">
      <section className="card session-bar">
        <span className="pill">
          {session.state === "running" && <span className="dot good pulse" />}
          {(session.state === "starting" || session.state === "stopping") && (
            <LoaderCircle size={14} className="spin" aria-hidden />
          )}
          {session.state === "failed" && <span className="dot critical" />}
          {session.state === "stopped" && <span className="dot" />}
          {STATE_LABEL[session.state]}
        </span>
        <div className="grow">
          <div className="title" title={session.title}>
            {session.title}
          </div>
          <div className="facts">
            <span>框架：{session.framework.name}</span>
            <span>来源：{session.source}</span>
            {session.engine && <span>识别：{session.engine}</span>}
            <span>
              每 {span(session.interval_seconds)}分析过去 {span(session.window_seconds)}
            </span>
          </div>
        </div>
        {running && (
          <div className="stack tight" style={{ alignItems: "flex-end" }}>
            <div className="level" title="输入音量">
              <Speaker size={14} aria-hidden className="muted" />
              <Meter value={levelRatio} label="输入音量" />
            </div>
            {session.state === "running" && (
              <span className="muted small tabular">下次分析：{clock(Math.max(0, session.next_report_at - now))} 后</span>
            )}
          </div>
        )}
        <div className="clock" title="已盯播时长（按音频计算）">
          {clock(now)}
        </div>
        {running && (
          <button className="btn danger" type="button" onClick={stop} disabled={stopping || session.state !== "running"}>
            <Square size={15} />
            停止
          </button>
        )}
        {finished && (
          <>
            <button className="btn" type="button" onClick={() => navigate(`/history/${session.session_id}`)}>
              <HistoryIcon size={15} />
              查看复盘
            </button>
            <button className="btn primary" type="button" onClick={onNewSession}>
              <Plus size={15} />
              再来一场
            </button>
          </>
        )}
      </section>

      {session.state === "failed" && <Notice tone="critical">{session.error}</Notice>}
      {running && !session.health.signal && (
        <Notice tone="warning">没有收到声音：检查直播间是否在播放、播放器是否静音，或者是否选错了设备。</Notice>
      )}
      {session.health.dropped_seconds > 0 && (
        <Notice tone="warning">
          识别跟不上，已跳过 {session.health.dropped_seconds} 秒音频，这段讲解没有分析。请关闭其他占用 CPU 的程序。
        </Notice>
      )}

      <div className="live-grid">
        <section className="card transcript-card">
          <div className="card-head">
            <h3>实时转写</h3>
            <TranscriptLegend />
          </div>
          <Transcript lines={session.segments} partial={session.partial} startedAt={session.started_at} live={running} />
        </section>

        <div className="live-column">
          <section className="card">
            <div className="card-head">
              <h3>提醒</h3>
              <span className="hint">{session.alerts.length} 条</span>
            </div>
            <div className="card-body" style={{ maxHeight: 320, overflow: "auto", paddingTop: 4, paddingBottom: 4 }}>
              <AlertList alerts={session.alerts} limit={50} highlightNew={running} />
            </div>
          </section>

          <section className="card">
            <div className="card-head">
              <h3>{report?.final ? "全场总结" : "窗口分析"}</h3>
              {(session.reports.length > 0 || session.summary) && (
                <select
                  className="select"
                  style={{ width: "auto" }}
                  aria-label="选择要查看的分析"
                  value={choice}
                  onChange={(event) => setChoice(event.target.value)}
                >
                  {session.summary && <option value="summary">全场总结</option>}
                  <option value="latest">最新一次</option>
                  {[...session.reports].reverse().map((r) => (
                    <option key={r.id ?? r.end} value={String(r.id)}>
                      {clock(r.start)}–{clock(r.end)}
                    </option>
                  ))}
                </select>
              )}
            </div>
            <div className="card-body stack">
              {report ? (
                <>
                  <div className="row between">
                    <span className="secondary tabular">
                      {report.final ? "全场" : "时间段"} {clock(report.start)}–{clock(report.end)}
                    </span>
                    {!report.full && !report.final && (
                      <Status tone="neutral">窗口未满 {span(session.window_seconds)}，暂不判断漏讲</Status>
                    )}
                  </div>
                  <MetricTiles metrics={report.metrics} pace={pace} />
                  <div>
                    <h4 style={{ marginBottom: 6 }}>发现的问题</h4>
                    <FindingList findings={report.findings} />
                  </div>
                  <hr className="divider" />
                  <div>
                    <h4 style={{ marginBottom: 6 }}>话术要点</h4>
                    <CoverageList coverage={report.coverage} judged={report.full || report.final} />
                  </div>
                  {!report.final && (
                    <LlmPanel review={report.llm} enabled={Boolean(settings?.llm.enabled)} />
                  )}
                </>
              ) : (
                <Empty>
                  {running
                    ? `第一次分析在 ${clock(session.interval_seconds)}，还有 ${clock(Math.max(0, session.next_report_at - now))}`
                    : "这一场太短，没有生成分析"}
                </Empty>
              )}
            </div>
          </section>

          <section className="card">
            <div className="card-body">
              <RateChart data={session.minute_rates} band={{ min: pace.min_rate, max: pace.max_rate }} />
            </div>
          </section>
        </div>
      </div>
    </div>
  );
}
