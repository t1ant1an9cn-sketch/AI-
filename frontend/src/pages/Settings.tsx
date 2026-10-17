import { Download, FolderOpen, LoaderCircle, PlugZap, Save, X } from "lucide-react";
import { useEffect, useState } from "react";

import { api, desktop } from "../api";
import { Meter, Notice, NumberInput, Segmented, Status } from "../components/ui";
import { useApp } from "../context";
import { megabytes, span } from "../format";
import type { ModelStatus, Settings, Severity } from "../types";

// 模型名称会随服务商更新，以对方文档为准；这里只是常用的默认值。
const LLM_PRESETS = [
  { name: "通义千问（阿里云百炼）", base_url: "https://dashscope.aliyuncs.com/compatible-mode/v1", model: "qwen-plus" },
  { name: "DeepSeek", base_url: "https://api.deepseek.com/v1", model: "deepseek-chat" },
  { name: "Kimi（月之暗面）", base_url: "https://api.moonshot.cn/v1", model: "moonshot-v1-8k" },
  { name: "智谱 GLM", base_url: "https://open.bigmodel.cn/api/paas/v4", model: "glm-4-flash" },
  { name: "OpenAI", base_url: "https://api.openai.com/v1", model: "gpt-4o-mini" },
  { name: "Ollama（本机模型）", base_url: "http://127.0.0.1:11434/v1", model: "qwen2.5:7b" },
];
const WINDOWS = [60, 120, 180, 300, 600, 900, 1800];
const INTERVALS = [15, 30, 60, 120, 300];

export function SettingsPage() {
  const { settings, setSettings, info, live, toast, setLeaveGuard } = useApp();
  const [draft, setDraft] = useState<Settings | null>(settings);
  const [model, setModel] = useState<ModelStatus | null>(null);
  const [error, setError] = useState("");
  const [saving, setSaving] = useState(false);
  const [testing, setTesting] = useState(false);
  const [testResult, setTestResult] = useState<{ ok: boolean; text: string } | null>(null);
  const [folder, setFolder] = useState("");

  useEffect(() => {
    if (settings && !draft) setDraft(settings);
  }, [settings]);

  useEffect(() => {
    api.model().then(setModel).catch(() => undefined);
  }, []);

  const dirty = Boolean(draft && settings && JSON.stringify(draft) !== JSON.stringify(settings));
  useEffect(() => {
    setLeaveGuard(dirty ? () => window.confirm("设置还没有保存，确定离开吗？") : null);
    return () => setLeaveGuard(null);
  }, [dirty]);

  if (!draft) return <div className="page muted">加载中……</div>;

  const modelStatus = live.model ?? model;
  const set = <K extends keyof Settings>(section: K, patch: Partial<Settings[K]>) =>
    setDraft({ ...draft, [section]: { ...draft[section], ...patch } });
  const preset = LLM_PRESETS.find((item) => item.base_url === draft.llm.base_url)?.name ?? "自定义";

  const save = async () => {
    setSaving(true);
    setError("");
    try {
      const saved = await api.saveSettings(draft);
      setSettings(saved);
      setDraft(saved);
      toast({ severity: "info", title: "设置已保存" });
    } catch (reason) {
      setError((reason as Error).message);
    } finally {
      setSaving(false);
    }
  };

  const test = async () => {
    setTesting(true);
    setTestResult(null);
    try {
      const result = await api.testLlm(draft.llm);
      setTestResult({ ok: true, text: `连接成功（${result.seconds} 秒）：${result.reply}` });
    } catch (reason) {
      setTestResult({ ok: false, text: (reason as Error).message });
    } finally {
      setTesting(false);
    }
  };

  const importModel = async () => {
    setError("");
    let path = folder.trim();
    const bridge = desktop();
    if (bridge) path = (await bridge.pick_folder()) ?? "";
    if (!path) return;
    try {
      setModel(await api.importModel(path));
      toast({ severity: "info", title: "模型已导入" });
    } catch (reason) {
      setError((reason as Error).message);
    }
  };

  return (
    <div className="page">
      <div className="narrow stack loose">
        <div className="page-head" style={{ marginBottom: 0 }}>
          <h2>设置</h2>
        </div>

        <section className="card">
          <div className="card-head">
            <h3>语音识别</h3>
          </div>
          <div className="card-body stack">
            <Segmented
              label="识别引擎"
              value={draft.asr.engine}
              onChange={(engine) => set("asr", { engine })}
              options={[
                { value: "sensevoice", label: "SenseVoice（本地，免费）" },
                { value: "xfyun", label: "讯飞实时转写（云端，收费）" },
              ]}
            />
            {draft.asr.engine === "sensevoice" ? (
              <>
                <div className="row between">
                  <div>
                    <div>{modelStatus?.name ?? "SenseVoice 识别模型"}</div>
                    <div className="muted small">
                      {modelStatus?.installed ? modelStatus.path : `首次使用需要下载，约 ${megabytes(modelStatus?.size ?? 241_862_836)}`}
                    </div>
                  </div>
                  {modelStatus?.installed && <Status tone="good">已安装</Status>}
                  {modelStatus && !modelStatus.installed && !modelStatus.downloading && (
                    <button
                      className="btn primary"
                      type="button"
                      onClick={() => api.downloadModel().then(setModel).catch((reason: Error) => setError(reason.message))}
                    >
                      <Download size={15} />
                      下载模型
                    </button>
                  )}
                  {modelStatus?.downloading && (
                    <button className="btn" type="button" onClick={() => api.cancelDownload().then(setModel)}>
                      <X size={15} />
                      取消
                    </button>
                  )}
                </div>
                {modelStatus?.downloading && (
                  <div className="stack tight">
                    <Meter value={modelStatus.total ? modelStatus.done / modelStatus.total : 0} label="下载进度" tall />
                    <span className="muted small tabular">
                      {megabytes(modelStatus.done)} / {megabytes(modelStatus.total)}
                    </span>
                  </div>
                )}
                {modelStatus?.error && !modelStatus.installed && <Notice tone="critical">{modelStatus.error}</Notice>}
                {!modelStatus?.installed && (
                  <div className="field">
                    <span className="field-label">或者从文件夹导入</span>
                    <div className="row">
                      {!desktop() && (
                        <input
                          className="input grow"
                          placeholder="含 model.int8.onnx、tokens.txt、silero_vad_v5.onnx 的文件夹路径"
                          value={folder}
                          onChange={(event) => setFolder(event.target.value)}
                        />
                      )}
                      <button className="btn" type="button" onClick={importModel}>
                        <FolderOpen size={15} />
                        {desktop() ? "选择文件夹导入" : "导入"}
                      </button>
                    </div>
                    <span className="help">适合无法联网的电脑：在其他电脑上下载好模型，把文件夹拷过来。</span>
                  </div>
                )}
                <div className="grid-3">
                  <div className="field">
                    <label htmlFor="lang">识别语言</label>
                    <select
                      id="lang"
                      className="select"
                      value={draft.asr.language}
                      onChange={(event) => set("asr", { language: event.target.value as Settings["asr"]["language"] })}
                    >
                      <option value="zh">普通话</option>
                      <option value="yue">粤语</option>
                      <option value="en">英语</option>
                      <option value="auto">自动判断</option>
                    </select>
                  </div>
                  <div className="field">
                    <label>CPU 线程数</label>
                    <NumberInput label="CPU 线程数" value={draft.asr.threads} min={1} max={16} onChange={(threads) => set("asr", { threads })} />
                  </div>
                  <div className="field">
                    <label htmlFor="source">模型下载源</label>
                    <select
                      id="source"
                      className="select"
                      value={draft.asr.model_source}
                      onChange={(event) =>
                        set("asr", { model_source: event.target.value as Settings["asr"]["model_source"] })
                      }
                    >
                      <option value="auto">自动（国内镜像优先）</option>
                      <option value="hf-mirror">hf-mirror.com 镜像</option>
                      <option value="huggingface">HuggingFace</option>
                    </select>
                  </div>
                </div>
                <label className="check">
                  <input
                    type="checkbox"
                    checked={draft.asr.partial_results}
                    onChange={(event) => set("asr", { partial_results: event.target.checked })}
                  />
                  边说边显示正在识别的文字（会多占用一些 CPU）
                </label>
              </>
            ) : (
              <>
                <Notice tone="warning">
                  需要在讯飞开放平台开通“实时语音转写”并按时长付费。本功能按讯飞公开的接口协议实现，尚未用真实账号验证过。
                </Notice>
                <div className="grid-2">
                  <div className="field">
                    <label htmlFor="xf-app">APPID</label>
                    <input
                      id="xf-app"
                      className="input"
                      value={draft.asr.xfyun_app_id}
                      onChange={(event) => set("asr", { xfyun_app_id: event.target.value.trim() })}
                    />
                  </div>
                  <div className="field">
                    <label htmlFor="xf-key">APIKey</label>
                    <input
                      id="xf-key"
                      className="input"
                      type="password"
                      autoComplete="off"
                      value={draft.asr.xfyun_api_key}
                      onChange={(event) => set("asr", { xfyun_api_key: event.target.value.trim() })}
                    />
                  </div>
                </div>
              </>
            )}
          </div>
        </section>

        <section className="card">
          <div className="card-head">
            <h3>大模型审核</h3>
            <label className="check">
              <input type="checkbox" checked={draft.llm.enabled} onChange={(event) => set("llm", { enabled: event.target.checked })} />
              开启
            </label>
          </div>
          <div className="card-body stack">
            <p className="secondary">
              每次窗口分析后，把这段转写和话术框架交给大模型，找出说错价格、卖点讲得含糊、换了说法的夸大宣传等关键词发现不了的问题。转写内容会发送给所选服务商。
            </p>
            <div className="grid-2">
              <div className="field">
                <label htmlFor="preset">服务商</label>
                <select
                  id="preset"
                  className="select"
                  value={preset}
                  onChange={(event) => {
                    const chosen = LLM_PRESETS.find((item) => item.name === event.target.value);
                    if (chosen) set("llm", { base_url: chosen.base_url, model: chosen.model });
                  }}
                >
                  {LLM_PRESETS.map((item) => (
                    <option key={item.name}>{item.name}</option>
                  ))}
                  <option>自定义</option>
                </select>
              </div>
              <div className="field">
                <label htmlFor="llm-model">模型名称</label>
                <input
                  id="llm-model"
                  className="input"
                  value={draft.llm.model}
                  onChange={(event) => set("llm", { model: event.target.value.trim() })}
                />
              </div>
              <div className="field">
                <label htmlFor="llm-url">接口地址（兼容 OpenAI）</label>
                <input
                  id="llm-url"
                  className="input"
                  value={draft.llm.base_url}
                  onChange={(event) => set("llm", { base_url: event.target.value.trim() })}
                />
              </div>
              <div className="field">
                <label htmlFor="llm-key">API Key</label>
                <input
                  id="llm-key"
                  className="input"
                  type="password"
                  autoComplete="off"
                  placeholder={preset.startsWith("Ollama") ? "本机模型不需要" : ""}
                  value={draft.llm.api_key}
                  onChange={(event) => set("llm", { api_key: event.target.value.trim() })}
                />
              </div>
              <div className="field">
                <label>审核频率</label>
                <div className="row">
                  <span>每</span>
                  <NumberInput label="每几次分析审核一次" value={draft.llm.review_every} min={1} max={10} onChange={(review_every) => set("llm", { review_every })} />
                  <span>次窗口分析审核一次</span>
                </div>
              </div>
              <div className="field">
                <label>超时（秒）</label>
                <NumberInput label="超时秒数" value={draft.llm.timeout_seconds} min={5} max={300} onChange={(timeout_seconds) => set("llm", { timeout_seconds })} />
              </div>
            </div>
            <div className="row">
              <button className="btn" type="button" onClick={test} disabled={testing}>
                {testing ? <LoaderCircle size={15} className="spin" /> : <PlugZap size={15} />}
                测试连接
              </button>
              {testResult && <Status tone={testResult.ok ? "good" : "critical"}>{testResult.text}</Status>}
            </div>
          </div>
        </section>

        <section className="card">
          <div className="card-head">
            <h3>默认分析方式</h3>
          </div>
          <div className="card-body stack">
            <div className="grid-2">
              <div className="field">
                <label htmlFor="d-interval">每隔</label>
                <select
                  id="d-interval"
                  className="select"
                  value={draft.monitor.interval_seconds}
                  onChange={(event) => set("monitor", { interval_seconds: Number(event.target.value) })}
                >
                  {INTERVALS.map((value) => (
                    <option key={value} value={value}>
                      {span(value)}
                    </option>
                  ))}
                </select>
              </div>
              <div className="field">
                <label htmlFor="d-window">分析过去</label>
                <select
                  id="d-window"
                  className="select"
                  value={draft.monitor.window_seconds}
                  onChange={(event) => set("monitor", { window_seconds: Number(event.target.value) })}
                >
                  {WINDOWS.map((value) => (
                    <option key={value} value={value}>
                      {span(value)}
                    </option>
                  ))}
                </select>
              </div>
            </div>
            <span className="help secondary small">开播时还可以单独调整。</span>
          </div>
        </section>

        <section className="card">
          <div className="card-head">
            <h3>录音</h3>
          </div>
          <div className="card-body stack">
            <label className="check">
              <input
                type="checkbox"
                checked={draft.monitor.save_audio}
                onChange={(event) => set("monitor", { save_audio: event.target.checked })}
              />
              实时盯播时保存录音（系统声音、麦克风、直播流）
            </label>
            <span className="help secondary small">
              录音只存在本机数据目录的 recordings 文件夹，约每小时 115 MB，删除场次时一起删除。复盘时可以边听边核对转写和提醒，也可以用它换识别引擎或改了话术框架后重新分析。录音会录下电脑上播放的所有声音，请只在需要时开启。
            </span>
          </div>
        </section>

        <section className="card">
          <div className="card-head">
            <h3>提醒方式</h3>
          </div>
          <div className="card-body stack">
            <label className="check">
              <input type="checkbox" checked={draft.notify.sound} onChange={(event) => set("notify", { sound: event.target.checked })} />
              出现问题时播放提示音
            </label>
            <div className="field" style={{ maxWidth: 320 }}>
              <label htmlFor="min-sev">弹窗和提示音的最低级别</label>
              <select
                id="min-sev"
                className="select"
                value={draft.notify.min_severity}
                onChange={(event) => set("notify", { min_severity: event.target.value as Severity })}
              >
                <option value="high">只提醒严重问题</option>
                <option value="medium">严重和需注意的问题</option>
                <option value="low">所有问题</option>
              </select>
            </div>
          </div>
        </section>

        {info && (
          <section className="card">
            <div className="card-head">
              <h3>关于</h3>
            </div>
            <div className="card-body stack tight secondary">
              <span>AI 盯播助手 {info.version}</span>
              <span>数据保存在：{info.data_dir}</span>
            </div>
          </section>
        )}

        <div className="sticky-actions card" style={{ borderRadius: "var(--radius)" }}>
          <button className="btn primary" type="button" onClick={save} disabled={!dirty || saving}>
            <Save size={15} />
            {saving ? "保存中……" : "保存设置"}
          </button>
          {dirty && (
            <button className="btn ghost" type="button" onClick={() => setDraft(settings)}>
              撤销修改
            </button>
          )}
          {error && <Notice tone="critical">{error}</Notice>}
        </div>
      </div>
    </div>
  );
}
