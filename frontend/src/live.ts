import type { LiveEvent, ModelStatus, Snapshot } from "./types";

export interface LiveState {
  connected: boolean;
  session: Snapshot | null;
  level: number; // 输入电平 dBFS
  model: ModelStatus | null; // 模型下载进度（来自 model 事件）
}

export const initialLive: LiveState = { connected: false, session: null, level: -120, model: null };

const MAX_SEGMENTS = 400;
const MAX_ALERTS = 200;
const MAX_REPORTS = 20;

function keepLast<T>(items: T[], limit: number): T[] {
  return items.length > limit ? items.slice(items.length - limit) : items;
}

/** 把一条实时事件合并进当前状态；与当前场次无关的事件忽略。 */
export function applyEvent(state: LiveState, event: LiveEvent): LiveState {
  if (event.type === "hello") return { ...state, session: event.session };
  if (event.type === "model") {
    const { type: _type, ...model } = event;
    return { ...state, model };
  }
  const session = state.session;
  if (!session || session.session_id !== event.session_id) return state;
  const update = (patch: Partial<Snapshot>): LiveState => ({ ...state, session: { ...session, ...patch } });

  switch (event.type) {
    case "state":
      return update({ state: event.state, error: event.error, engine: event.engine || session.engine });
    case "level":
      return { ...state, level: event.db, session: { ...session, health: { ...session.health, audio_time: event.time } } };
    case "partial":
      return update({ partial: event.text });
    case "segment":
      return update({
        partial: "",
        segments: keepLast([...session.segments, { segment: event.segment, highlights: event.highlights }], MAX_SEGMENTS),
      });
    case "report": {
      const rates = new Map(session.minute_rates.map((item) => [item.minute, item.rate]));
      for (const minute of event.report.metrics.minute_rates) rates.set(Math.floor(minute.start / 60), minute.rate);
      return update({
        reports: keepLast([...session.reports, event.report], MAX_REPORTS),
        minute_rates: [...rates.entries()].sort((a, b) => a[0] - b[0]).map(([minute, rate]) => ({ minute, rate })),
        next_report_at: event.report.end + session.interval_seconds,
      });
    }
    case "summary":
      return update({ summary: event.report });
    case "alert":
      return update({ alerts: keepLast([...session.alerts, event.alert], MAX_ALERTS) });
    case "llm":
      return update({
        reports: session.reports.map((report) =>
          report.id !== null && report.id === event.report_id ? { ...report, llm: event.review } : report,
        ),
      });
    case "health": {
      const { type: _type, session_id: _id, ...health } = event;
      return update({ health });
    }
  }
}

/** 维持一条 WebSocket 连接，断开后自动重连；每次连上服务端都会先发 hello（当前场次的完整状态）。 */
export function connectLive(
  url: string,
  onEvent: (event: LiveEvent) => void,
  onStatus: (connected: boolean) => void,
): () => void {
  let socket: WebSocket | null = null;
  let retry = 0;
  let timer: number | undefined;
  let closed = false;

  const open = () => {
    socket = new WebSocket(url);
    socket.onopen = () => {
      retry = 0;
      onStatus(true);
    };
    socket.onmessage = (message) => {
      try {
        onEvent(JSON.parse(message.data as string) as LiveEvent);
      } catch {
        // 忽略无法解析的消息
      }
    };
    socket.onclose = () => {
      onStatus(false);
      if (closed) return;
      retry += 1;
      timer = window.setTimeout(open, Math.min(5000, 500 * retry));
    };
  };
  open();
  return () => {
    closed = true;
    window.clearTimeout(timer);
    socket?.close();
  };
}
