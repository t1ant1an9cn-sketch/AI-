import {
  AudioWaveform,
  History,
  ListChecks,
  Monitor,
  Moon,
  Settings as SettingsIcon,
  Sun,
  SunMoon,
  X,
} from "lucide-react";
import { useCallback, useEffect, useMemo, useReducer, useRef, useState } from "react";

import { api, eventsUrl } from "./api";
import { SeverityIcon } from "./components/ui";
import { AppContext, type AppContextValue, type ToastInput } from "./context";
import { atLeast } from "./format";
import { applyEvent, connectLive, initialLive } from "./live";
import { FrameworksPage } from "./pages/Frameworks";
import { HistoryPage } from "./pages/History";
import { MonitorPage } from "./pages/Monitor";
import { SettingsPage } from "./pages/Settings";
import { beep } from "./sound";
import type { Info, LiveEvent, Settings, Snapshot } from "./types";

type Theme = "auto" | "light" | "dark";
const THEMES: Theme[] = ["auto", "light", "dark"];
const THEME_LABEL: Record<Theme, string> = { auto: "跟随系统", light: "浅色", dark: "深色" };

const NAV = [
  { path: "monitor", label: "盯播", icon: Monitor },
  { path: "frameworks", label: "话术框架", icon: ListChecks },
  { path: "history", label: "历史复盘", icon: History },
  { path: "settings", label: "设置", icon: SettingsIcon },
];

function parseRoute(): string[] {
  const parts = window.location.hash
    .replace(/^#\/?/, "")
    .split("/")
    .filter(Boolean)
    .map((part) => decodeURIComponent(part));
  return parts.length && NAV.some((item) => item.path === parts[0]) ? parts : ["monitor"];
}

interface Toast extends ToastInput {
  id: number;
}

export function App() {
  const [live, dispatch] = useReducer(applyEvent, initialLive);
  const [connected, setConnected] = useState(false);
  const [settings, setSettings] = useState<Settings | null>(null);
  const [info, setInfo] = useState<Info | null>(null);
  const [route, setRoute] = useState(parseRoute);
  const [toasts, setToasts] = useState<Toast[]>([]);
  const [theme, setTheme] = useState<Theme>(() => (localStorage.getItem("theme") as Theme) || "auto");
  const guard = useRef<(() => boolean) | null>(null);
  const liveRef = useRef(live);
  const settingsRef = useRef(settings);
  liveRef.current = live;
  settingsRef.current = settings;

  const toast = useCallback((input: ToastInput) => {
    const id = Date.now() + Math.random();
    setToasts((current) => [...current.slice(-3), { ...input, id }]);
    const lifetime = input.severity === "high" ? 12000 : 6000;
    window.setTimeout(() => setToasts((current) => current.filter((item) => item.id !== id)), lifetime);
  }, []);

  useEffect(() => {
    api
      .settings()
      .then(setSettings)
      .catch(() => undefined);
    api
      .info()
      .then(setInfo)
      .catch(() => undefined);
  }, []);

  useEffect(() => {
    if (theme === "auto") document.documentElement.removeAttribute("data-theme");
    else document.documentElement.setAttribute("data-theme", theme);
    localStorage.setItem("theme", theme);
  }, [theme]);

  useEffect(() => {
    const onHash = () => setRoute(parseRoute());
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);

  useEffect(() => {
    let refetch: number | undefined;
    const onEvent = (event: LiveEvent) => {
      dispatch(event);
      if (event.type === "hello" || event.type === "model" || event.type === "logins") return;
      const session = liveRef.current.session;
      // 本页还不知道的场次（例如开播请求返回前就到的事件）：取一次完整状态。
      if (!session || session.session_id !== event.session_id) {
        window.clearTimeout(refetch);
        refetch = window.setTimeout(() => {
          api
            .session()
            .then((snapshot) => dispatch({ type: "hello", session: snapshot }))
            .catch(() => undefined);
        }, 150);
        return;
      }
      // 快速分析文件时几秒内会出现很多提醒，只记在提醒列表里，不弹窗、不响铃。
      if (event.type === "alert" && session.paced) {
        const notify = settingsRef.current?.notify ?? { sound: true, min_severity: "medium" as const };
        if (atLeast(event.alert.severity, notify.min_severity)) {
          toast({ severity: event.alert.severity, title: event.alert.title, detail: event.alert.detail });
          if (notify.sound) beep(event.alert.severity);
        }
      }
    };
    const disconnect = connectLive(eventsUrl(), onEvent, setConnected);
    return () => {
      window.clearTimeout(refetch);
      disconnect();
    };
  }, [toast]);

  const navigate = useCallback((path: string) => {
    const target = `#${path.startsWith("/") ? path : `/${path}`}`;
    if (target === window.location.hash) return;
    if (guard.current && !guard.current()) return;
    guard.current = null;
    window.location.hash = target;
  }, []);

  const context = useMemo<AppContextValue>(
    () => ({
      live,
      setSession: (snapshot: Snapshot | null) => dispatch({ type: "hello", session: snapshot }),
      settings,
      setSettings,
      info,
      toast,
      route,
      navigate,
      setLeaveGuard: (fn) => {
        guard.current = fn;
      },
    }),
    [live, settings, info, toast, route, navigate],
  );

  const running = live.session?.state === "running" || live.session?.state === "starting";
  const page = route[0];
  const ThemeIcon = theme === "auto" ? SunMoon : theme === "light" ? Sun : Moon;

  return (
    <AppContext.Provider value={context}>
      <div className="app">
        <aside className="sidebar">
          <div className="brand">
            <span className="brand-mark">
              <AudioWaveform size={18} />
            </span>
            <span className="brand-text">
              <span className="brand-name">AI 盯播助手</span>
              <span className="brand-sub">直播讲解实时提醒</span>
            </span>
          </div>
          <nav className="nav" aria-label="主菜单">
            {NAV.map((item) => (
              <a
                key={item.path}
                href={`#/${item.path}`}
                title={item.label}
                aria-current={page === item.path ? "page" : undefined}
                onClick={(event) => {
                  event.preventDefault();
                  navigate(`/${item.path}`);
                }}
              >
                <item.icon size={18} aria-hidden />
                <span className="nav-label">{item.label}</span>
                {item.path === "monitor" && running && (
                  <span className="on-air-chip" title="正在盯播">
                    <span className="dot live pulse" />
                    <span className="nav-label">直播中</span>
                  </span>
                )}
              </a>
            ))}
          </nav>
          <div className="sidebar-foot">
            {!connected && (
              <span className="connection" role="status" title="未连接本地服务，正在重试……">
                <span className="dot critical" />
                <span className="nav-label">未连接，正在重试……</span>
              </span>
            )}
            <button
              className="theme-btn"
              type="button"
              title={`外观：${THEME_LABEL[theme]}（点击切换）`}
              onClick={() => setTheme(THEMES[(THEMES.indexOf(theme) + 1) % THEMES.length])}
            >
              <ThemeIcon size={16} />
              <span className="nav-label">{THEME_LABEL[theme]}</span>
            </button>
            {info?.version && <span className="version nav-label">版本 {info.version}</span>}
          </div>
        </aside>
        <main className="main">
          {page === "frameworks" && <FrameworksPage />}
          {page === "history" && <HistoryPage />}
          {page === "settings" && <SettingsPage />}
          {page === "monitor" && <MonitorPage />}
        </main>
      </div>
      <div className="toasts" aria-live="assertive">
        {toasts.map((item) => (
          <div className={`toast ${item.severity}`} key={item.id} role="alert">
            {item.severity === "info" ? <span /> : <SeverityIcon severity={item.severity} />}
            <div>
              <div className="title">{item.title}</div>
              {item.detail && <div className="secondary small">{item.detail}</div>}
            </div>
            <button
              className="icon-btn"
              type="button"
              aria-label="关闭"
              onClick={() => setToasts((current) => current.filter((toastItem) => toastItem.id !== item.id))}
            >
              <X size={14} />
            </button>
          </div>
        ))}
      </div>
    </AppContext.Provider>
  );
}
