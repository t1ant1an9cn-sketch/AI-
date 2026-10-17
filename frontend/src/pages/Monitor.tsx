import {
  AudioLines,
  Bot,
  Download,
  FileAudio,
  FolderOpen,
  History as HistoryIcon,
  Link,
  LoaderCircle,
  LogIn,
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
import { AlertList, CoverageChips, CoverageList, FindingList, LlmPanel, MetricTiles } from "../components/Report";
import { RateChart } from "../components/RateChart";
import { Transcript, TranscriptLegend } from "../components/Transcript";
import { Empty, LevelBars, Meter, Notice, Status } from "../components/ui";
import { useApp } from "../context";
import { clock, dateTime, megabytes, span } from "../format";
import { clearPrefill, readPrefill } from "../prefill";
import { unlockSound } from "../sound";
import type {
  Alert,
  Device,
  FrameworkSummary,
  LoginStatus,
  ModelStatus,
  Snapshot,
  SourceSpec,
  WindowReport,
} from "../types";

type SourceKind = "loopback" | "microphone" | "file" | "url" | "demo";

const SOURCES: { value: SourceKind; label: string; desc: string; icon: ReactNode }[] = [
  { value: "url", label: "直播间链接", desc: "粘贴链接就能盯", icon: <Link size={20} /> },
  { value: "loopback", label: "系统声音", desc: "电脑正在播放的声音", icon: <Speaker size={20} /> },
  { value: "microphone", label: "麦克风", desc: "主播自己的声音", icon: <Mic size={20} /> },
  { value: "file", label: "音频文件", desc: "复盘录播或录音", icon: <FileAudio size={20} /> },
  { value: "demo", label: "演示音频", desc: "3 分钟示例讲解", icon: <Sparkles size={20} /> },
];
const WINDOWS = [60, 120, 180, 300, 600, 900, 1800];
const INTERVALS = [15, 30, 60, 120, 300];
// 直播间链接、分享口令里的链接、没写 https:// 的“live.douyin.com/…”，或者 flv/m3u8/rtmp 直播流地址
const LINK = /(https?|rtmps?|rtsp|srt):\/\/\S+|^[a-z0-9-]+(\.[a-z0-9-]+)+\//i;

export function MonitorPage() {
  const { live, route, navigate } = useApp();
  const [choosing, setChoosing] = useState(false);
  const fresh = route[1] === "new"; // #/monitor/new：从复盘页跳来开一场新的
  if (!live.session || choosing || fresh) {
    return (
      <Setup
        onStarted={() => {
          setChoosing(false);
          if (fresh) navigate("/monitor");
        }}
      />
    );
  }
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
  const [prefill] = useState(readPrefill); // “用这段录音重新分析”带过来的录音和框架
  const [frameworks, setFrameworks] = useState<FrameworkSummary[] | null>(null);
  const [frameworkId, setFrameworkId] = useState(
    () => prefill?.frameworkId ?? localStorage.getItem("last-framework") ?? "",
  );
  const [kind, setKind] = useState<SourceKind>(() =>
    prefill ? "file" : (localStorage.getItem("last-source") as SourceKind) || "demo",
  );
  const [devices, setDevices] = useState<Device[] | null>(null);
  const [deviceError, setDeviceError] = useState("");
  const [deviceChoice, setDeviceChoice] = useState<Record<"loopback" | "microphone", string>>({
    loopback: "",
    microphone: "",
  });
  const [file, setFile] = useState<{ path: string; name: string } | null>(() => prefill?.file ?? null);
  const [uploading, setUploading] = useState(false);
  const [fileRealtime, setFileRealtime] = useState(false);
  const [demoRealtime, setDemoRealtime] = useState(true);
  const [url, setUrl] = useState("");
  const [windowSeconds, setWindowSeconds] = useState(() => prefill?.windowSeconds ?? 300);
  const [intervalSeconds, setIntervalSeconds] = useState(() => prefill?.intervalSeconds ?? 60);
  const [title, setTitle] = useState(() => prefill?.title ?? "");
  const [model, setModel] = useState<ModelStatus | null>(null);
  const [starting, setStarting] = useState(false);
  const [error, setError] = useState("");
  const fileInput = useRef<HTMLInputElement>(null);

  useEffect(() => clearPrefill(), []);

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
    if (!settings || prefill) return; // 重新分析时沿用原场的分析方式
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
  const deviceKind = kind === "loopback" || kind === "microphone";

  let sourceProblem = "";
  if (deviceKind) {
    if (devices !== null && !listed.length) sourceProblem = kind === "loopback" ? "没有找到可采集的播放设备" : "没有找到麦克风";
  } else if (kind === "file" && !file) sourceProblem = "请先选择音频文件";
  else if (kind === "url" && !LINK.test(url.trim())) sourceProblem = "请粘贴直播间链接或直播流地址";

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
    if (deviceKind) {
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
  // 只录实时来源；文件本来就在磁盘上
  const liveSource = deviceKind || kind === "url";
  const recordHint: { tone: "good" | "neutral"; label: string } = !settings?.monitor.save_audio
    ? { tone: "neutral", label: "不保存" }
    : liveSource
      ? { tone: "good", label: "保存到本机" }
      : { tone: "neutral", label: "文件不另存" };
  const sourceDetail =
    kind === "url"
      ? url.trim() || "还没填链接"
      : kind === "file"
        ? (file?.name ?? "还没选文件")
        : kind === "demo"
          ? "保温杯讲解（合成语音）"
          : (listed.find((device) => device.id === deviceChoice[kind])?.name ?? "还没选设备");

  return (
    <div className="page">
      <div className="setup">
        <div className="setup-main">
          <header className="page-title">
            <h1>开始盯播</h1>
            <p className="secondary">选好从哪里听、按什么标准盯，就可以开始。</p>
          </header>

          <section className="card step">
            <div className="step-head">
              <span className="step-no">1</span>
              <div className="grow">
                <h2>听哪里</h2>
                <p className="muted small">直播间链接最省事：不用开着浏览器，也不会混进电脑上的其他声音。</p>
              </div>
            </div>
            <div className="source-grid" role="radiogroup" aria-label="音频来源">
              {SOURCES.map((option) => (
                <button
                  key={option.value}
                  type="button"
                  role="radio"
                  aria-checked={option.value === kind}
                  className="source-tile"
                  onClick={() => setKind(option.value)}
                >
                  <span className="tile-icon">{option.icon}</span>
                  <span className="tile-name">
                    {option.label}
                    {option.value === "url" && <span className="reco">推荐</span>}
                  </span>
                  <span className="tile-desc">{option.desc}</span>
                </button>
              ))}
            </div>
            <div className="source-pane">
              {deviceKind && (
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
                  <span className="help">
                    {kind === "loopback"
                      ? "先在浏览器或直播伴侣里打开要盯的直播间，确认声音从这个设备播放出来。电脑上的其他声音也会被一起识别。适合需要登录才能看、或者没有网页版的直播。"
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
                    <span className="grow secondary ellipsis">
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
                    className="input large"
                    aria-label="直播间链接"
                    placeholder="粘贴直播间链接，例如 https://live.douyin.com/…，分享口令也可以"
                    value={url}
                    onChange={(event) => setUrl(event.target.value)}
                  />
                  <span className="help">
                    抖音、快手、B 站、斗鱼、虎牙等平台的网页直播间都可以，打开需要几秒到二十几秒。也可以直接填 flv、m3u8、rtmp
                    直播流地址。
                  </span>
                  <LoginRow url={url} />
                </>
              )}
              {kind === "demo" && (
                <>
                  <span>
                    内置一段约 3 分钟的保温杯带货讲解（电脑合成语音），其中故意包含违规词、站外导流、冷场和漏讲的要点，用来体验各种提醒。
                  </span>
                  <span className="help">建议搭配“保温杯讲解（示例）”框架，分析过去 1 分钟。</span>
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
          </section>

          <section className="card step">
            <div className="step-head">
              <span className="step-no">2</span>
              <div className="grow">
                <h2>按什么标准盯</h2>
                <p className="muted small">话术框架规定要讲哪些要点、哪些话不能说。</p>
              </div>
              <button className="btn small ghost" type="button" onClick={() => navigate("/frameworks")}>
                管理话术框架
              </button>
            </div>
            <div className="field">
              <label htmlFor="framework">话术框架</label>
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
              <span className="field-label">分析节奏</span>
              <div className="cadence">
                每
                <select
                  id="interval"
                  className="select inline"
                  aria-label="每隔多久分析一次"
                  value={intervalSeconds}
                  onChange={(event) => setIntervalSeconds(Number(event.target.value))}
                >
                  {withCurrent(INTERVALS, intervalSeconds).map((value) => (
                    <option key={value} value={value}>
                      {span(value)}
                    </option>
                  ))}
                </select>
                分析一次过去
                <select
                  id="window"
                  className="select inline"
                  aria-label="每次分析过去多久"
                  value={windowSeconds}
                  onChange={(event) => setWindowSeconds(Number(event.target.value))}
                >
                  {withCurrent(WINDOWS, windowSeconds).map((value) => (
                    <option key={value} value={value}>
                      {span(value)}
                    </option>
                  ))}
                </select>
                的讲解
              </div>
              {intervalSeconds > windowSeconds ? (
                <Notice tone="warning">分析间隔不能大于分析窗口。</Notice>
              ) : (
                <span className="help">
                  每次分析语速、冷场、说话占比、口头禅和要点有没有讲到；违规表达和冷场出现时立即提醒，不等分析。
                </span>
              )}
            </div>
          </section>
        </div>

        <aside className="card launch">
          <h2>准备开播</h2>
          <dl className="launch-facts">
            <div>
              <dt>听</dt>
              <dd>
                <span>{SOURCES.find((option) => option.value === kind)?.label}</span>
                <span className="sub ellipsis" title={sourceDetail}>
                  {sourceDetail}
                </span>
              </dd>
            </div>
            <div>
              <dt>标准</dt>
              <dd>{selected?.name ?? "还没选话术框架"}</dd>
            </div>
            <div>
              <dt>节奏</dt>
              <dd>
                每 {span(intervalSeconds)}分析过去 {span(windowSeconds)}
              </dd>
            </div>
          </dl>
          <div className="checks">
            <EngineStatus
              engine={engine}
              model={modelStatus}
              ready={engineReady}
              onDownload={() => api.downloadModel().then(setModel).catch((reason: Error) => setError(reason.message))}
              onSettings={() => navigate("/settings")}
            />
            <div className="check-row" title="开启后，大模型会额外判断说错价格、卖点讲得含糊等关键词发现不了的问题">
              <Bot size={16} aria-hidden />
              <span className="grow">大模型审核</span>
              {settings?.llm.enabled ? (
                <Status tone="good">{settings.llm.model}</Status>
              ) : (
                <Status tone="neutral">未开启</Status>
              )}
            </div>
            <div className="check-row" title="录音只存在本机，复盘时可以边听边核对，也能用它重新分析。约每小时 115 MB。">
              <AudioLines size={16} aria-hidden />
              <span className="grow">录音</span>
              <Status tone={recordHint.tone}>{recordHint.label}</Status>
            </div>
            <button className="link-btn small" type="button" onClick={() => navigate("/settings")}>
              修改设置
            </button>
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
          <button className="btn primary xl" type="button" disabled={!canStart} onClick={start}>
            {starting ? <LoaderCircle size={18} className="spin" /> : <Play size={18} />}
            {starting ? (kind === "url" ? "正在打开直播间……" : "正在启动……") : "开始盯播"}
          </button>
          {sourceProblem && (!deviceKind || devices !== null) && <span className="launch-hint">{sourceProblem}</span>}
        </aside>
      </div>
    </div>
  );
}

// 登录窗口只能打开网页，rtmp 这类直播流地址不行
const PAGE_LINK = /https?:\/\/\S+|^[a-z0-9-]+(\.[a-z0-9-]+)+\//i;

/** 要登录才能看的直播间：弹出浏览器窗口让用户登录一次，之后打开直播间时自动带上登录信息。 */
function LoginRow({ url }: { url: string }) {
  const { live } = useApp();
  const [status, setStatus] = useState<LoginStatus | null>(null);
  const [opening, setOpening] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    api.logins().then(setStatus).catch(() => undefined);
  }, []);

  const current = live.logins ?? status;
  const page = PAGE_LINK.test(url.trim());
  const open = async () => {
    setOpening(true);
    setError("");
    try {
      setStatus(await api.openLogin(url.trim()));
    } catch (reason) {
      setError((reason as Error).message);
    } finally {
      setOpening(false);
    }
  };

  return (
    <>
      <div className="row wrap">
        <button
          className="btn small"
          type="button"
          onClick={open}
          disabled={!page || opening}
          title={page ? "在弹出的浏览器窗口里打开这个直播间，登录后关掉窗口" : "先粘贴直播间链接"}
        >
          {opening ? <LoaderCircle size={14} className="spin" /> : <LogIn size={14} />}
          登录平台账号
        </button>
        <span className="help grow">
          {current?.open
            ? "登录窗口已打开：在窗口里登录（一般是用手机 App 扫码），能看到直播画面后关掉窗口，再开始盯播。"
            : current?.saved_at
              ? `已保存登录信息（${dateTime(new Date(current.saved_at * 1000).toISOString())}），打开直播间时会自动带上。`
              : "要登录才能看的直播间（例如淘宝直播），先点这里，在弹出的窗口里登录一次。"}
        </span>
      </div>
      {error && <Notice tone="critical">{error}</Notice>}
    </>
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
      <div className="check-row">
        <Mic size={16} aria-hidden />
        <span className="grow">识别：讯飞（云端）</span>
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
    <>
      <div className="check-row">
        <Mic size={16} aria-hidden />
        <span className="grow">识别：SenseVoice（本地）</span>
        {ready && <Status tone="good">已就绪</Status>}
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
        <span className="help">第一次使用需要下载识别模型，下载后完全在本机运行。</span>
      )}
    </>
  );
}

const STATE_LABEL: Record<Snapshot["state"], string> = {
  starting: "正在启动",
  running: "盯播中",
  stopping: "正在收尾",
  stopped: "已结束",
  failed: "出错了",
};

const SEVERITY_ORDER = ["high", "medium", "low"] as const;
const SEVERITY_LABEL = { high: "严重", medium: "注意", low: "提示" };

function SeverityCounts({ alerts }: { alerts: Alert[] }) {
  const counts = { high: 0, medium: 0, low: 0 };
  for (const alert of alerts) counts[alert.severity] += 1;
  if (!alerts.length) return <span className="muted small">还没有</span>;
  return (
    <span className="sev-counts">
      {SEVERITY_ORDER.filter((severity) => counts[severity]).map((severity) => (
        <span key={severity} className={`sev-count ${severity}`}>
          {SEVERITY_LABEL[severity]} {counts[severity]}
        </span>
      ))}
    </span>
  );
}

type Tab = "transcript" | "analysis" | "rate";

function LiveView({ session, level, onNewSession }: { session: Snapshot; level: number; onNewSession(): void }) {
  const { settings, navigate, setSession } = useApp();
  const [stopping, setStopping] = useState(false);
  const [choice, setChoice] = useState<string>("latest");
  const [tab, setTab] = useState<Tab>("transcript");
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
  // 顶部的数字和要点：盯播中看最新一次分析，结束后看全场总结
  const overview: WindowReport | null = finished && session.summary ? session.summary : latest;
  const report: WindowReport | null =
    choice === "summary"
      ? session.summary
      : choice === "latest"
        ? latest
        : (session.reports.find((r) => String(r.id) === choice) ?? latest);
  const untilNext = clock(Math.max(0, session.next_report_at - now));
  const alertTile = (
    <div className="stat">
      <span className="label">提醒</span>
      <span className="value">
        {session.alerts.length}
        <small>条</small>
      </span>
      <SeverityCounts alerts={session.alerts} />
    </div>
  );

  return (
    <div className="page live">
      <section className={`live-header ${session.state}`}>
        <span className={`on-air ${session.state}`}>
          {session.state === "running" && <span className="dot live pulse" />}
          {(session.state === "starting" || session.state === "stopping") && (
            <LoaderCircle size={14} className="spin" aria-hidden />
          )}
          {session.state === "failed" && <span className="dot critical" />}
          {session.state === "stopped" && <span className="dot" />}
          {STATE_LABEL[session.state]}
        </span>
        <div className="live-title grow">
          <h1 title={session.title}>{session.title}</h1>
          <div className="facts">
            <span>{session.framework.name}</span>
            {session.source !== session.title && <span>{session.source}</span>}
            {session.engine && <span>{session.engine}</span>}
            <span>
              每 {span(session.interval_seconds)}分析过去 {span(session.window_seconds)}
            </span>
            {session.recording && <span>{running ? "录音中" : "已录音"}（只存本机）</span>}
          </div>
        </div>
        {running && <LevelBars level={level} />}
        <div className="live-figure">
          <span className="label">已盯播</span>
          <span className="value" title="已盯播时长（按音频计算）">
            {clock(now)}
          </span>
        </div>
        {session.state === "running" && (
          <div className="live-figure secondary-figure">
            <span className="label">下次分析</span>
            <span className="value">{untilNext}</span>
          </div>
        )}
        {running && (
          <button className="btn danger large" type="button" onClick={stop} disabled={stopping || session.state !== "running"}>
            <Square size={15} />
            停止
          </button>
        )}
        {finished && (
          <div className="row">
            <button className="btn large" type="button" onClick={() => navigate(`/history/${session.session_id}`)}>
              <HistoryIcon size={16} />
              查看复盘
            </button>
            <button className="btn primary large" type="button" onClick={onNewSession}>
              <Plus size={16} />
              再来一场
            </button>
          </div>
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

      {overview ? (
        <MetricTiles metrics={overview.metrics} pace={pace} wide extra={alertTile} />
      ) : (
        <div className="kpis pending">
          {["语速", "说话时间占比", "冷场", "口头禅"].map((label) => (
            <div className="stat" key={label}>
              <span className="label">{label}</span>
              <span className="value muted">—</span>
              <span className="muted small">{running ? `${untilNext} 后第一次分析` : "没有分析"}</span>
            </div>
          ))}
          {alertTile}
        </div>
      )}

      <section className="card coverage-card">
        <div className="coverage-head">
          <h3>话术要点</h3>
          {overview && (
            <span className="muted small tabular">
              {overview.final ? "全场" : `${clock(overview.start)}–${clock(overview.end)}`}
              {!overview.full && !overview.final && `，窗口未满 ${span(session.window_seconds)}，暂不判断漏讲`}
            </span>
          )}
        </div>
        {overview ? (
          <CoverageChips coverage={overview.coverage} judged={overview.full || overview.final} />
        ) : (
          <span className="muted small">第一次分析后，这里显示每个要点讲了没有。</span>
        )}
      </section>

      <div className="live-main">
        <section className="card alerts-card">
          <div className="card-head">
            <h3>提醒</h3>
            <SeverityCounts alerts={session.alerts} />
          </div>
          <div className="card-body scroll">
            <AlertList alerts={session.alerts} limit={100} highlightNew={running} />
          </div>
        </section>

        <section className="card tabs-card">
          <div className="card-head tabs-head">
            <div className="tabs" role="tablist" aria-label="详细内容">
              {(
                [
                  ["transcript", "实时转写"],
                  ["analysis", report?.final ? "全场总结" : "分析详情"],
                  ["rate", "语速变化"],
                ] as [Tab, string][]
              ).map(([value, label]) => (
                <button key={value} type="button" role="tab" aria-selected={tab === value} onClick={() => setTab(value)}>
                  {label}
                </button>
              ))}
            </div>
            {tab === "transcript" && <TranscriptLegend />}
            {tab === "analysis" && (session.reports.length > 0 || session.summary) && (
              <select
                className="select compact"
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
          {tab === "transcript" && (
            <Transcript lines={session.segments} partial={session.partial} startedAt={session.started_at} live={running} />
          )}
          {tab === "analysis" && (
            <div className="card-body scroll stack">
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
                    <h4 className="sub-title">发现的问题</h4>
                    <FindingList findings={report.findings} />
                  </div>
                  <div>
                    <h4 className="sub-title">话术要点</h4>
                    <CoverageList coverage={report.coverage} judged={report.full || report.final} />
                  </div>
                  {!report.final && <LlmPanel review={report.llm} enabled={Boolean(settings?.llm.enabled)} />}
                </>
              ) : (
                <Empty>
                  {running
                    ? `第一次分析在 ${clock(session.interval_seconds)}，还有 ${untilNext}`
                    : "这一场太短，没有生成分析"}
                </Empty>
              )}
            </div>
          )}
          {tab === "rate" && (
            <div className="card-body">
              <RateChart data={session.minute_rates} band={{ min: pace.min_rate, max: pace.max_rate }} />
            </div>
          )}
        </section>
      </div>
    </div>
  );
}
