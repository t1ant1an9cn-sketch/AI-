// 从复盘页跳到开播页时预先填好的内容（“用这段录音重新分析”）。存在本页会话里，用一次就清掉。

export interface Prefill {
  frameworkId: string;
  title: string;
  file: { path: string; name: string };
  // 沿用原场的分析方式，两次结果才好比较
  windowSeconds: number;
  intervalSeconds: number;
}

const KEY = "monitor-prefill";

export function setPrefill(prefill: Prefill): void {
  sessionStorage.setItem(KEY, JSON.stringify(prefill));
}

/** 只读不删：React 开发模式会把初始化函数调用两次，删除放到挂载后的 clearPrefill。 */
export function readPrefill(): Prefill | null {
  const raw = sessionStorage.getItem(KEY);
  if (!raw) return null;
  try {
    const value = JSON.parse(raw) as Prefill;
    return value?.file?.path ? value : null;
  } catch {
    return null;
  }
}

export function clearPrefill(): void {
  sessionStorage.removeItem(KEY);
}
