import type {
  Device,
  Framework,
  FrameworkSummary,
  Info,
  LoginStatus,
  ModelStatus,
  SessionDetail,
  SessionSummary,
  Settings,
  Snapshot,
  StartRequest,
} from "./types";

const TOKEN_KEY = "voicedetect-token";
let token = "";

/** 启动时程序把令牌放在网址的 # 后面；读出后保存在本页会话里，再把地址栏换成首页。 */
export function initToken(): string {
  const match = window.location.hash.match(/token=([^&/]+)/);
  if (match) {
    sessionStorage.setItem(TOKEN_KEY, decodeURIComponent(match[1]));
    window.history.replaceState(null, "", `${window.location.pathname}${window.location.search}#/monitor`);
  }
  token = sessionStorage.getItem(TOKEN_KEY) ?? "";
  return token;
}

export function eventsUrl(): string {
  const scheme = window.location.protocol === "https:" ? "wss" : "ws";
  return `${scheme}://${window.location.host}/api/events?token=${encodeURIComponent(token)}`;
}

/** 录音地址：<audio> 元素没法加请求头，令牌放在网址里。 */
export function audioUrl(sessionId: string): string {
  return `/api/sessions/${encodeURIComponent(sessionId)}/audio?token=${encodeURIComponent(token)}`;
}

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
  ) {
    super(message);
  }
}

async function send(method: string, path: string, body?: unknown, raw?: BodyInit): Promise<Response> {
  const headers: Record<string, string> = { "X-Token": token };
  let payload = raw;
  if (body !== undefined) {
    headers["Content-Type"] = "application/json";
    payload = JSON.stringify(body);
  }
  let response: Response;
  try {
    response = await fetch(path, { method, headers, body: payload });
  } catch {
    throw new ApiError("连不上本地服务，请重新打开程序", 0);
  }
  if (!response.ok) {
    let detail = `请求失败（${response.status}）`;
    try {
      const data = await response.json();
      if (typeof data?.detail === "string") detail = data.detail;
    } catch {
      // 不是 JSON 的错误页，使用默认提示
    }
    if (response.status === 401) detail = "访问令牌失效，请关闭后重新打开程序";
    throw new ApiError(detail, response.status);
  }
  return response;
}

async function json<T>(method: string, path: string, body?: unknown, raw?: BodyInit): Promise<T> {
  const response = await send(method, path, body, raw);
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

const id = (value: string) => encodeURIComponent(value);

export const api = {
  info: () => json<Info>("GET", "/api/info"),
  devices: () => json<Device[]>("GET", "/api/devices"),

  frameworks: () => json<FrameworkSummary[]>("GET", "/api/frameworks"),
  framework: (frameworkId: string) => json<Framework>("GET", `/api/frameworks/${id(frameworkId)}`),
  createFramework: (framework: Framework) => json<Framework>("POST", "/api/frameworks", framework),
  updateFramework: (framework: Framework) => json<Framework>("PUT", `/api/frameworks/${id(framework.id)}`, framework),
  deleteFramework: (frameworkId: string) => json<void>("DELETE", `/api/frameworks/${id(frameworkId)}`),

  settings: () => json<Settings>("GET", "/api/settings"),
  saveSettings: (settings: Settings) => json<Settings>("PUT", "/api/settings", settings),
  testLlm: (llm: Settings["llm"]) => json<{ reply: string; seconds: number }>("POST", "/api/llm/test", llm),

  model: () => json<ModelStatus>("GET", "/api/models"),
  downloadModel: () => json<ModelStatus>("POST", "/api/models/download"),
  cancelDownload: () => json<ModelStatus>("POST", "/api/models/cancel"),
  importModel: (folder: string) => json<ModelStatus>("POST", "/api/models/import", { folder }),

  logins: () => json<LoginStatus>("GET", "/api/logins"),
  openLogin: (url: string) => json<LoginStatus>("POST", "/api/logins/open", { url }),
  clearLogins: () => json<LoginStatus>("DELETE", "/api/logins"),

  session: () => json<Snapshot | null>("GET", "/api/session"),
  start: (request: StartRequest) => json<Snapshot>("POST", "/api/session/start", request),
  stop: () => json<Snapshot | null>("POST", "/api/session/stop"),

  sessions: (limit = 50, offset = 0) =>
    json<{ items: SessionSummary[]; total: number }>("GET", `/api/sessions?limit=${limit}&offset=${offset}`),
  sessionDetail: (sessionId: string) => json<SessionDetail>("GET", `/api/sessions/${id(sessionId)}`),
  deleteSession: (sessionId: string) => json<void>("DELETE", `/api/sessions/${id(sessionId)}`),
  reportMarkdown: async (sessionId: string) =>
    (await send("GET", `/api/sessions/${id(sessionId)}/report.md`)).text(),

  upload: (file: File) =>
    json<{ path: string; name: string }>("PUT", `/api/uploads?name=${encodeURIComponent(file.name)}`, undefined, file),
};

/** 桌面窗口（pywebview）提供的本机功能；在浏览器里打开时不存在。 */
interface DesktopBridge {
  pick_audio_file(): Promise<string | null>;
  pick_folder(): Promise<string | null>;
  save_text(filename: string, content: string): Promise<string | null>;
}

declare global {
  interface Window {
    pywebview?: { api: DesktopBridge };
  }
}

export function desktop(): DesktopBridge | null {
  return window.pywebview?.api ?? null;
}

/** 保存文本文件：桌面窗口里弹出“另存为”，浏览器里直接下载。返回 false 表示用户取消。 */
export async function saveText(filename: string, content: string, type = "text/plain"): Promise<boolean> {
  const bridge = desktop();
  if (bridge) return (await bridge.save_text(filename, content)) !== null;
  const url = URL.createObjectURL(new Blob([content], { type: `${type};charset=utf-8` }));
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
  return true;
}
