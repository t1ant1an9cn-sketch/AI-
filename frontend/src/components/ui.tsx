import { CircleAlert, CircleCheck, CircleMinus, CircleX, Info, TriangleAlert, X } from "lucide-react";
import { type KeyboardEvent, type ReactNode, useEffect, useRef, useState } from "react";

import type { CoverageStatus, Severity } from "../types";

export type Tone = "good" | "warning" | "serious" | "critical" | "neutral";

const TONE_ICON = {
  good: CircleCheck,
  warning: CircleAlert,
  serious: TriangleAlert,
  critical: CircleX,
  neutral: CircleMinus,
};

/** 状态永远是“图标 + 文字”，不只靠颜色。 */
export function Status({ tone, children }: { tone: Tone; children: ReactNode }) {
  const Icon = TONE_ICON[tone];
  return (
    <span className={`status ${tone}`}>
      <Icon size={14} aria-hidden />
      {children}
    </span>
  );
}

export const SEVERITY_TONE: Record<Severity, Tone> = { high: "critical", medium: "serious", low: "warning" };

export function SeverityIcon({ severity, size = 18 }: { severity: Severity; size?: number }) {
  const Icon = severity === "high" ? TriangleAlert : severity === "medium" ? CircleAlert : Info;
  const label = { high: "严重", medium: "需注意", low: "提示" }[severity];
  return <Icon className={`sev-icon ${severity}`} size={size} aria-label={label} role="img" />;
}

export const COVERAGE: Record<CoverageStatus, { tone: Tone; label: string }> = {
  covered: { tone: "good", label: "已讲" },
  insufficient: { tone: "warning", label: "不够" },
  missing: { tone: "critical", label: "没讲" },
  unchecked: { tone: "neutral", label: "无关键词" },
};

export function Meter({ value, label, tall }: { value: number; label: string; tall?: boolean }) {
  const width = `${Math.round(Math.max(0, Math.min(1, value)) * 100)}%`;
  return (
    <div
      className={`meter${tall ? " tall" : ""}`}
      role="meter"
      aria-label={label}
      aria-valuemin={0}
      aria-valuemax={100}
      aria-valuenow={Math.round(value * 100)}
    >
      <span style={{ width }} />
    </div>
  );
}

export function Empty({ icon, children }: { icon?: ReactNode; children: ReactNode }) {
  return (
    <div className="empty">
      {icon}
      <div>{children}</div>
    </div>
  );
}

export function Notice({ tone = "info", children }: { tone?: "info" | "warning" | "critical"; children: ReactNode }) {
  const Icon = tone === "info" ? Info : tone === "warning" ? CircleAlert : TriangleAlert;
  return (
    <div className={`notice ${tone}`} role={tone === "critical" ? "alert" : undefined}>
      <Icon size={16} aria-hidden />
      <div className="grow">{children}</div>
    </div>
  );
}

export function Segmented<T extends string>({
  value,
  options,
  onChange,
  label,
}: {
  value: T;
  options: { value: T; label: string; icon?: ReactNode }[];
  onChange(value: T): void;
  label: string;
}) {
  return (
    <div className="segmented" role="group" aria-label={label}>
      {options.map((option) => (
        <button
          key={option.value}
          type="button"
          aria-pressed={option.value === value}
          onClick={() => onChange(option.value)}
        >
          {option.icon}
          {option.label}
        </button>
      ))}
    </div>
  );
}

const SEPARATORS = /[,，、;；\n]+/;

/** 关键词输入：回车、逗号或顿号添加；退格删除最后一个；粘贴多个词时自动拆开。 */
export function TagInput({
  value,
  onChange,
  placeholder,
  label,
}: {
  value: string[];
  onChange(value: string[]): void;
  placeholder?: string;
  label: string;
}) {
  const [draft, setDraft] = useState("");
  const input = useRef<HTMLInputElement>(null);

  const add = (text: string) => {
    const words = text
      .split(SEPARATORS)
      .map((word) => word.trim())
      .filter(Boolean);
    const next = [...value];
    for (const word of words) if (!next.includes(word)) next.push(word);
    if (next.length !== value.length) onChange(next);
    setDraft("");
  };

  const onKeyDown = (event: KeyboardEvent<HTMLInputElement>) => {
    if (event.key === "Enter" || event.key === "," || event.key === "，" || event.key === "、") {
      event.preventDefault();
      add(draft);
    } else if (event.key === "Backspace" && !draft && value.length) {
      onChange(value.slice(0, -1));
    }
  };

  return (
    <div className="tag-input" onClick={() => input.current?.focus()}>
      {value.map((word) => (
        <span className="tag" key={word}>
          {word}
          <button type="button" aria-label={`删除 ${word}`} onClick={() => onChange(value.filter((w) => w !== word))}>
            <X size={12} />
          </button>
        </span>
      ))}
      <input
        ref={input}
        value={draft}
        aria-label={label}
        placeholder={value.length ? "" : placeholder}
        onChange={(event) => {
          const text = event.target.value;
          if (SEPARATORS.test(text)) add(text);
          else setDraft(text);
        }}
        onKeyDown={onKeyDown}
        onBlur={() => draft && add(draft)}
      />
    </div>
  );
}

export function NumberInput({
  value,
  onChange,
  min,
  max,
  step = 1,
  label,
}: {
  value: number;
  onChange(value: number): void;
  min?: number;
  max?: number;
  step?: number;
  label: string;
}) {
  const [text, setText] = useState(String(value));
  useEffect(() => setText(String(value)), [value]);
  return (
    <input
      className="input number tabular"
      type="number"
      inputMode="decimal"
      aria-label={label}
      value={text}
      min={min}
      max={max}
      step={step}
      onChange={(event) => {
        setText(event.target.value);
        const number = Number(event.target.value);
        if (event.target.value !== "" && Number.isFinite(number)) onChange(number);
      }}
      onBlur={() => setText(String(value))}
    />
  );
}

/** 简单的确认对话框，用 Promise 返回用户的选择。 */
export function useConfirm() {
  const [request, setRequest] = useState<{
    title: string;
    detail: string;
    action: string;
    resolve(ok: boolean): void;
  } | null>(null);

  const confirm = (title: string, detail: string, action = "确定") =>
    new Promise<boolean>((resolve) => setRequest({ title, detail, action, resolve }));

  const close = (ok: boolean) => {
    request?.resolve(ok);
    setRequest(null);
  };

  const dialog = request ? (
    <div className="modal-backdrop" onClick={() => close(false)}>
      <div className="modal" role="dialog" aria-modal="true" onClick={(event) => event.stopPropagation()}>
        <div className="card-body">
          <h3>{request.title}</h3>
          <p className="secondary">{request.detail}</p>
        </div>
        <div className="card-foot">
          <button className="btn" type="button" onClick={() => close(false)} autoFocus>
            取消
          </button>
          <button className="btn danger" type="button" onClick={() => close(true)}>
            {request.action}
          </button>
        </div>
      </div>
    </div>
  ) : null;

  return { confirm, dialog };
}
