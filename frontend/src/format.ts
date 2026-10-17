import type { Severity } from "./types";

/** 会话内时间：75 → 01:15，3725 → 1:02:05。 */
export function clock(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined || !Number.isFinite(seconds)) return "—";
  const total = Math.max(0, Math.round(seconds));
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const secs = total % 60;
  const mm = String(minutes).padStart(2, "0");
  const ss = String(secs).padStart(2, "0");
  return hours ? `${hours}:${mm}:${ss}` : `${mm}:${ss}`;
}

/** 时长：3725 → “1 小时 2 分”，95 → “1 分 35 秒”。 */
export function duration(seconds: number): string {
  const total = Math.max(0, Math.round(seconds));
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const secs = total % 60;
  if (hours) return minutes ? `${hours} 小时 ${minutes} 分` : `${hours} 小时`;
  if (minutes) return secs ? `${minutes} 分 ${secs} 秒` : `${minutes} 分钟`;
  return `${secs} 秒`;
}

/** 分析窗口的口语说法：300 → “5 分钟”，90 → “90 秒”。 */
export function span(seconds: number): string {
  const total = Math.round(seconds);
  return total >= 60 && total % 60 === 0 ? `${total / 60} 分钟` : `${total} 秒`;
}

/** 会话开始时刻 + 音频时间 → 当天钟点，例如 “20:15:03”。 */
export function wallClock(startedAt: string, offset: number): string {
  const base = Date.parse(startedAt);
  if (Number.isNaN(base)) return clock(offset);
  return new Date(base + offset * 1000).toLocaleTimeString("zh-CN", { hour12: false });
}

/** ISO 时间 → “10月17日 20:15”。 */
export function dateTime(iso: string | null): string {
  if (!iso) return "—";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  const month = date.getMonth() + 1;
  const time = date.toLocaleTimeString("zh-CN", { hour12: false, hour: "2-digit", minute: "2-digit" });
  return `${month}月${date.getDate()}日 ${time}`;
}

export function megabytes(bytes: number): string {
  return `${(bytes / 1_000_000).toFixed(bytes >= 10_000_000 ? 0 : 1)} MB`;
}

export function percent(ratio: number): string {
  return `${Math.round(ratio * 100)}%`;
}

export const SEVERITY_LABEL: Record<Severity, string> = { high: "高", medium: "中", low: "低" };
const SEVERITY_ORDER: Record<Severity, number> = { high: 0, medium: 1, low: 2 };

export function atLeast(severity: Severity, minimum: Severity): boolean {
  return SEVERITY_ORDER[severity] <= SEVERITY_ORDER[minimum];
}

export function bySeverity<T extends { severity: Severity }>(items: T[]): T[] {
  return [...items].sort((a, b) => SEVERITY_ORDER[a.severity] - SEVERITY_ORDER[b.severity]);
}
