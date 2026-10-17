// 与后端 Pydantic 模型一一对应（voicedetect/analysis/report.py、frameworks.py、config.py）。

export type Severity = "high" | "medium" | "low";
export type SessionState = "starting" | "running" | "stopping" | "stopped" | "failed";

export interface Segment {
  id: number;
  start: number;
  end: number;
  text: string;
  emotion: string | null;
}

export interface Highlight {
  start: number;
  end: number;
  kind: "point" | "forbidden" | "filler";
  ref: string;
  label: string;
  exact: boolean;
}

export interface TranscriptLine {
  segment: Segment;
  highlights: Highlight[];
}

export interface Evidence {
  segment_id: number;
  text: string;
  sentence: string;
  start: number;
  end: number;
  exact: boolean;
}

export type CoverageStatus = "covered" | "insufficient" | "missing" | "unchecked";

export interface PointCoverage {
  stage_id: string;
  stage_name: string;
  point_id: string;
  point_name: string;
  required: boolean;
  min_mentions: number;
  mentions: number;
  status: CoverageStatus;
  evidence: Evidence[];
  first_at: number | null;
  last_at: number | null;
}

export interface ForbiddenHit {
  rule_id: string;
  rule_name: string;
  severity: Severity;
  keyword: string;
  evidence: Evidence;
}

export interface Pause {
  start: number;
  end: number;
  seconds: number;
}

export interface MinuteRate {
  start: number;
  end: number;
  speech_seconds: number;
  rate: number | null;
}

export interface Metrics {
  window_seconds: number;
  speech_seconds: number;
  speech_ratio: number;
  syllables: number;
  rate: number | null;
  segment_count: number;
  pauses: Pause[];
  longest_pause: number;
  fillers: Record<string, number>;
  fillers_per_minute: number;
  minute_rates: MinuteRate[];
}

export interface Finding {
  key: string;
  kind: string;
  severity: Severity;
  title: string;
  detail: string;
  suggestion: string;
  evidence: Evidence[];
}

export interface LlmIssue {
  category: string;
  severity: Severity;
  title: string;
  quote: string;
  problem: string;
  suggestion: string;
  verified: boolean;
}

export interface LlmReview {
  status: "done" | "failed" | "skipped";
  model: string;
  summary: string;
  points: { point_id: string; status: "covered" | "partial" | "missing"; comment: string }[];
  issues: LlmIssue[];
  error: string;
  seconds: number;
}

export interface WindowReport {
  id: number | null;
  session_id: string;
  start: number;
  end: number;
  full: boolean;
  final: boolean;
  created_at: string;
  metrics: Metrics;
  coverage: PointCoverage[];
  forbidden: ForbiddenHit[];
  findings: Finding[];
  llm: LlmReview | null;
}

export interface Alert {
  id: number | null;
  session_id: string;
  time: number;
  created_at: string;
  source: "realtime" | "window" | "llm" | "system";
  kind: string;
  severity: Severity;
  title: string;
  detail: string;
  suggestion: string;
  evidence: Evidence[];
  report_id: number | null;
}

export interface Health {
  audio_time: number;
  backlog_seconds: number;
  dropped_seconds: number;
  signal: boolean;
}

export interface PaceRules {
  min_rate: number;
  max_rate: number;
  max_pause_seconds: number;
  min_speech_ratio: number;
  max_fillers_per_minute: number;
}

export interface Snapshot {
  session_id: string;
  title: string;
  paced: boolean; // 音频按真实时间到达（实时来源或按实际速度播放）；快速分析文件时为 false
  recording: string | null; // 本场录音文件路径；不录时为 null
  state: SessionState;
  error: string;
  started_at: string;
  framework: { id: string; name: string; pace: PaceRules };
  source: string;
  engine: string;
  window_seconds: number;
  interval_seconds: number;
  next_report_at: number;
  partial: string;
  segments: TranscriptLine[];
  alerts: Alert[];
  reports: WindowReport[];
  minute_rates: { minute: number; rate: number | null }[];
  summary: WindowReport | null;
  health: Health;
}

export interface Point {
  id: string;
  name: string;
  keywords: string[];
  description: string;
  required: boolean;
  min_mentions: number;
}

export interface Stage {
  id: string;
  name: string;
  points: Point[];
}

export interface ForbiddenRule {
  id: string;
  name: string;
  keywords: string[];
  severity: Severity;
  suggestion: string;
}

export interface Framework {
  id: string;
  name: string;
  description: string;
  stages: Stage[];
  forbidden: ForbiddenRule[];
  fillers: string[];
  pace: PaceRules;
  check_order: boolean;
  llm_instructions: string;
}

export interface FrameworkSummary {
  id: string;
  name: string;
  description: string;
  stage_count: number;
  point_count: number;
  forbidden_count: number;
  updated_at: string;
}

export interface Settings {
  asr: {
    engine: "sensevoice" | "xfyun";
    language: "zh" | "yue" | "en" | "auto";
    threads: number;
    partial_results: boolean;
    model_source: "auto" | "hf-mirror" | "huggingface";
    xfyun_app_id: string;
    xfyun_api_key: string;
  };
  llm: {
    enabled: boolean;
    base_url: string;
    api_key: string;
    model: string;
    review_every: number;
    timeout_seconds: number;
  };
  monitor: { window_seconds: number; interval_seconds: number; save_audio: boolean };
  notify: { sound: boolean; min_severity: Severity };
}

export interface ModelStatus {
  id: string;
  name: string;
  size: number;
  installed: boolean;
  path: string;
  downloading: boolean;
  done: number;
  total: number;
  error: string;
}

export interface LoginStatus {
  open: boolean; // 登录窗口是否开着
  saved_at: number | null; // 登录信息最后保存的时间（Unix 秒）；没有保存过为 null
}

export interface Device {
  id: string;
  name: string;
  kind: "loopback" | "microphone";
  default: boolean;
}

export interface AppInfo {
  pid: number;
  name: string; // 程序文件里的“文件说明”，例如“Microsoft Edge”
  exe: string;
  active: boolean; // 正在出声
}

export interface Info {
  version: string;
  platform: string;
  loopback_supported: boolean;
  app_capture_supported: boolean; // 能否只听某个程序的声音（Windows 11）
  data_dir: string;
}

export interface SessionSummary {
  id: string;
  title: string;
  framework_id: string;
  framework_name: string;
  source: string;
  engine: string;
  window_seconds: number;
  interval_seconds: number;
  started_at: string;
  ended_at: string | null;
  duration: number;
  status: "running" | "stopped" | "failed" | "interrupted";
  error: string;
  alert_count: number;
  finding_count: number;
}

export interface Recording {
  path: string;
  bytes: number;
  seconds: number;
}

export interface SessionDetail extends SessionSummary {
  framework: Framework;
  summary: WindowReport | null;
  segments: Segment[];
  reports: WindowReport[];
  alerts: Alert[];
  recording: Recording | null;
}

export type SourceSpec =
  | { kind: "device"; device_id: string | null; device_name: string; loopback: boolean }
  | { kind: "app"; process_id: number; app_name: string }
  | { kind: "file"; path: string; realtime: boolean }
  | { kind: "url"; url: string }
  | { kind: "demo"; realtime: boolean };

export interface StartRequest {
  framework_id: string;
  title: string;
  source: SourceSpec;
  window_seconds: number;
  interval_seconds: number;
}

export type LiveEvent =
  | { type: "hello"; session: Snapshot | null }
  | { type: "state"; session_id: string; state: SessionState; error: string; engine: string }
  | { type: "level"; session_id: string; db: number; time: number }
  | { type: "partial"; session_id: string; text: string; start: number; end: number }
  | { type: "segment"; session_id: string; segment: Segment; highlights: Highlight[] }
  | { type: "report"; session_id: string; report: WindowReport }
  | { type: "summary"; session_id: string; report: WindowReport }
  | { type: "alert"; session_id: string; alert: Alert }
  | { type: "llm"; session_id: string; report_id: number | null; review: LlmReview }
  | ({ type: "health"; session_id: string } & Health)
  | ({ type: "model" } & ModelStatus)
  | ({ type: "logins" } & LoginStatus);
