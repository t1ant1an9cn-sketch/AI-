import { createContext, useContext } from "react";

import type { LiveState } from "./live";
import type { Info, Severity, Settings, Snapshot } from "./types";

export interface ToastInput {
  severity: Severity | "info";
  title: string;
  detail?: string;
}

export interface AppContextValue {
  live: LiveState;
  setSession(snapshot: Snapshot | null): void;
  settings: Settings | null;
  setSettings(settings: Settings): void;
  info: Info | null;
  toast(toast: ToastInput): void;
  route: string[];
  navigate(path: string): void;
  /** 有未保存的修改时注册确认函数，离开页面前询问。 */
  setLeaveGuard(guard: (() => boolean) | null): void;
}

export const AppContext = createContext<AppContextValue | null>(null);

export function useApp(): AppContextValue {
  const value = useContext(AppContext);
  if (!value) throw new Error("useApp 必须在 AppContext 内使用");
  return value;
}
