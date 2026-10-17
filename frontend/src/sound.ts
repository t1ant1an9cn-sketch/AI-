import type { Severity } from "./types";

let context: AudioContext | null = null;

/** 浏览器要求先有一次用户操作才能发声；在点击“开始盯播”时调用。 */
export function unlockSound(): void {
  try {
    context ??= new AudioContext();
    void context.resume();
  } catch {
    context = null;
  }
}

/** 提示音：高级别两声，其他一声。 */
export function beep(severity: Severity): void {
  if (!context || context.state !== "running") return;
  const notes = severity === "high" ? [880, 880] : [660];
  const now = context.currentTime;
  notes.forEach((frequency, index) => {
    const oscillator = context!.createOscillator();
    const gain = context!.createGain();
    const start = now + index * 0.22;
    oscillator.frequency.value = frequency;
    gain.gain.setValueAtTime(0.0001, start);
    gain.gain.exponentialRampToValueAtTime(0.18, start + 0.015);
    gain.gain.exponentialRampToValueAtTime(0.0001, start + 0.18);
    oscillator.connect(gain).connect(context!.destination);
    oscillator.start(start);
    oscillator.stop(start + 0.2);
  });
}
