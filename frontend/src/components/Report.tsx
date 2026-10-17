import { Bot, ChevronDown, ChevronRight } from "lucide-react";
import { useState } from "react";

import { bySeverity, clock, percent } from "../format";
import type { Alert, Finding, LlmReview, Metrics, PaceRules, PointCoverage } from "../types";
import { COVERAGE, Meter, SeverityIcon, Status, type Tone } from "./ui";

function rateStatus(rate: number | null, pace: PaceRules): [Tone, string] {
  if (rate === null) return ["neutral", "说话太少，暂不评价"];
  if (rate > pace.max_rate) return ["serious", `偏快（建议 ≤ ${pace.max_rate}）`];
  if (rate < pace.min_rate) return ["warning", `偏慢（建议 ≥ ${pace.min_rate}）`];
  return ["good", "正常"];
}

/** 窗口指标：语速、说话占比、冷场、口头禅；extra 为 true 时再加违规次数（历史复盘用）。 */
export function MetricTiles({
  metrics,
  pace,
  forbidden,
  wide,
}: {
  metrics: Metrics;
  pace: PaceRules;
  forbidden?: number;
  wide?: boolean;
}) {
  const [rateTone, rateText] = rateStatus(metrics.rate, pace);
  const lowSpeech = metrics.window_seconds >= 60 && metrics.speech_ratio < pace.min_speech_ratio;
  const fillerTotal = Object.values(metrics.fillers).reduce((sum, count) => sum + count, 0);
  const manyFillers = fillerTotal >= 3 && metrics.fillers_per_minute > pace.max_fillers_per_minute;
  const topFillers = Object.entries(metrics.fillers)
    .slice(0, 2)
    .map(([word, count]) => `${word}×${count}`)
    .join("、");

  return (
    <div className={wide ? "kpis" : "stats"}>
      <div className="stat">
        <span className="label">语速</span>
        <span className="value">
          {metrics.rate === null ? "—" : Math.round(metrics.rate)}
          <small>字/分钟</small>
        </span>
        <Status tone={rateTone}>{rateText}</Status>
      </div>
      <div className="stat">
        <span className="label">说话时间占比</span>
        <span className="value">{percent(metrics.speech_ratio)}</span>
        <Meter value={metrics.speech_ratio} label="说话时间占比" />
        <Status tone={lowSpeech ? "serious" : "good"}>
          {lowSpeech ? `偏少（建议 ≥ ${percent(pace.min_speech_ratio)}）` : "正常"}
        </Status>
      </div>
      <div className="stat">
        <span className="label">冷场（停顿超过 {pace.max_pause_seconds} 秒）</span>
        <span className="value">
          {metrics.pauses.length}
          <small>次</small>
        </span>
        <Status tone={metrics.pauses.length ? "serious" : "good"}>
          {metrics.pauses.length ? `最长 ${Math.round(metrics.longest_pause)} 秒` : "没有冷场"}
        </Status>
      </div>
      <div className="stat">
        <span className="label">口头禅</span>
        <span className="value">
          {metrics.fillers_per_minute.toFixed(1)}
          <small>次/分钟</small>
        </span>
        <Status tone={manyFillers ? "warning" : "good"}>
          {manyFillers ? `偏多：${topFillers}` : fillerTotal ? `正常：${topFillers}` : "没有"}
        </Status>
      </div>
      {forbidden !== undefined && (
        <div className="stat">
          <span className="label">违规表达</span>
          <span className="value">
            {forbidden}
            <small>次</small>
          </span>
          <Status tone={forbidden ? "critical" : "good"}>{forbidden ? "需要整改" : "没有"}</Status>
        </div>
      )}
    </div>
  );
}

type Seek = (seconds: number) => void;

/** 时间标签：有录音时可以点击，跳到录音里的这个位置。 */
export function TimeLink({ seconds, onSeek }: { seconds: number; onSeek?: Seek }) {
  if (!onSeek) return <span className="muted tabular">{clock(seconds)}</span>;
  return (
    <button
      className="time-link tabular"
      type="button"
      title="跳到录音的这个位置"
      onClick={(event) => {
        event.stopPropagation();
        onSeek(seconds);
      }}
    >
      {clock(seconds)}
    </button>
  );
}

export function FindingList({
  findings,
  empty = "这一段没有发现问题",
  onSeek,
}: {
  findings: Finding[];
  empty?: string;
  onSeek?: Seek;
}) {
  if (!findings.length) return <Status tone="good">{empty}</Status>;
  return (
    <div>
      {bySeverity(findings).map((finding) => (
        <div className="finding" key={finding.key}>
          <SeverityIcon severity={finding.severity} />
          <div>
            <div className="title" style={{ fontWeight: 600 }}>
              {finding.title}
            </div>
            <div className="secondary">{finding.detail}</div>
            {finding.suggestion && <div className="small secondary">建议：{finding.suggestion}</div>}
            {finding.evidence.slice(0, 2).map((evidence, index) => (
              <div className="quote-line" key={index}>
                <TimeLink seconds={evidence.start} onSeek={onSeek} /> {evidence.sentence}
              </div>
            ))}
          </div>
        </div>
      ))}
    </div>
  );
}

/** 要点覆盖：按环节分组，点开看命中的原话和时间。 */
export function CoverageList({
  coverage,
  judged,
  onSeek,
}: {
  coverage: PointCoverage[];
  judged: boolean;
  onSeek?: Seek;
}) {
  const [open, setOpen] = useState<string | null>(null);
  const stages = new Map<string, { name: string; items: PointCoverage[] }>();
  for (const item of coverage) {
    const stage = stages.get(item.stage_id) ?? { name: item.stage_name, items: [] };
    stage.items.push(item);
    stages.set(item.stage_id, stage);
  }
  if (!coverage.length) return <div className="muted">这个话术框架没有设置要点</div>;
  return (
    <div className="coverage">
      {[...stages.entries()].map(([stageId, stage]) => (
        <div key={stageId}>
          <h4>{stage.name}</h4>
          {stage.items.map((item) => {
            // 窗口未满时，“没讲”只是“还没讲到”，不按问题显示。
            const status = !judged && item.status === "missing" ? null : COVERAGE[item.status];
            const expanded = open === item.point_id && item.evidence.length > 0;
            return (
              <div key={item.point_id}>
                <div
                  className="point-row"
                  role={item.evidence.length ? "button" : undefined}
                  tabIndex={item.evidence.length ? 0 : undefined}
                  aria-expanded={item.evidence.length ? expanded : undefined}
                  onClick={() => setOpen(expanded ? null : item.point_id)}
                  onKeyDown={(event) => event.key === "Enter" && setOpen(expanded ? null : item.point_id)}
                  style={{ cursor: item.evidence.length ? "pointer" : "default" }}
                >
                  {item.evidence.length ? (
                    expanded ? (
                      <ChevronDown size={14} />
                    ) : (
                      <ChevronRight size={14} />
                    )
                  ) : (
                    <span />
                  )}
                  <div>
                    {item.point_name}
                    {!item.required && <span className="muted small">（选讲）</span>}
                  </div>
                  <div className="row">
                    <span className="muted small tabular">
                      {item.mentions}/{item.min_mentions} 次
                    </span>
                    {status ? <Status tone={status.tone}>{status.label}</Status> : <Status tone="neutral">还没讲到</Status>}
                  </div>
                </div>
                {expanded &&
                  item.evidence.map((evidence, index) => (
                    <div className="quote-line" key={index} style={{ marginLeft: 26 }}>
                      <TimeLink seconds={evidence.start} onSeek={onSeek} /> {evidence.sentence}
                      {!evidence.exact && <span className="muted small">（同音匹配：{evidence.text}）</span>}
                    </div>
                  ))}
              </div>
            );
          })}
        </div>
      ))}
    </div>
  );
}

export function LlmPanel({ review, enabled }: { review: LlmReview | null; enabled: boolean }) {
  if (!enabled && !review) return null;
  return (
    <div className="stack tight">
      <div className="row">
        <Bot size={16} aria-hidden />
        <h4>大模型审核</h4>
        {review?.status === "done" && (
          <span className="muted small">
            {review.model} · {review.seconds} 秒
          </span>
        )}
      </div>
      {!review && <span className="muted">审核中……结果会自动出现在这里</span>}
      {review?.status === "failed" && <Status tone="critical">审核失败：{review.error}</Status>}
      {review?.status === "skipped" && <span className="muted">{review.error || "本轮跳过"}</span>}
      {review?.status === "done" && (
        <>
          {review.summary && <p>{review.summary}</p>}
          {review.issues.length === 0 && <Status tone="good">没有发现额外问题</Status>}
          {bySeverity(review.issues).map((issue, index) => (
            <div className="finding" key={index}>
              <SeverityIcon severity={issue.severity} />
              <div>
                <div style={{ fontWeight: 600 }}>
                  {issue.category}：{issue.problem}
                </div>
                {issue.quote && (
                  <div className="quote-line">
                    「{issue.quote}」{!issue.verified && <span className="muted small">（转写里没找到这句原话）</span>}
                  </div>
                )}
                {issue.suggestion && <div className="small secondary">建议：{issue.suggestion}</div>}
              </div>
            </div>
          ))}
        </>
      )}
    </div>
  );
}

const SOURCE_LABEL: Record<Alert["source"], string> = {
  realtime: "实时",
  window: "窗口分析",
  llm: "大模型",
  system: "系统",
};

/** 提醒列表，最新的在最上面。 */
export function AlertList({
  alerts,
  limit,
  highlightNew,
  onSeek,
  empty = "还没有提醒。发现问题时会在这里出现，并发出提示音。",
}: {
  alerts: Alert[];
  limit?: number;
  highlightNew?: boolean;
  onSeek?: Seek;
  empty?: string;
}) {
  const items = [...alerts].reverse().slice(0, limit ?? alerts.length);
  if (!items.length) return <div className="empty">{empty}</div>;
  return (
    <div className="alerts" aria-live="polite">
      {items.map((alert, index) => (
        <div
          className={`alert-item${highlightNew && index === 0 ? " new" : ""}`}
          key={alert.id ?? `${alert.time}-${alert.title}`}
        >
          <SeverityIcon severity={alert.severity} />
          <div className="grow">
            <div className="head">
              <span className="title">{alert.title}</span>
              <span className="muted small tabular nowrap">
                {SOURCE_LABEL[alert.source]} · <TimeLink seconds={alert.time} onSeek={onSeek} />
              </span>
            </div>
            <div className="detail">{alert.detail}</div>
            {alert.evidence[0] && (
              <div className="quote-line">
                <TimeLink seconds={alert.evidence[0].start} onSeek={onSeek} /> {alert.evidence[0].sentence}
              </div>
            )}
            {alert.suggestion && <div className="suggestion">建议：{alert.suggestion}</div>}
          </div>
        </div>
      ))}
    </div>
  );
}
