import { ArrowLeft, FileDown, Trash2 } from "lucide-react";
import { useEffect, useState } from "react";

import { api, saveText } from "../api";
import { RateChart } from "../components/RateChart";
import { AlertList, CoverageList, FindingList, MetricTiles } from "../components/Report";
import { Empty, Notice, Status, type Tone, useConfirm } from "../components/ui";
import { useApp } from "../context";
import { clock, dateTime, duration, percent, span } from "../format";
import type { SessionDetail, SessionSummary } from "../types";

const PAGE = 50;

const STATUS: Record<SessionSummary["status"], { tone: Tone; label: string }> = {
  running: { tone: "good", label: "进行中" },
  stopped: { tone: "neutral", label: "已结束" },
  failed: { tone: "critical", label: "异常结束" },
  interrupted: { tone: "warning", label: "已中断" },
};

export function HistoryPage() {
  const { route } = useApp();
  return route[1] ? <SessionView id={route[1]} /> : <SessionList />;
}

function SessionList() {
  const { navigate } = useApp();
  const [items, setItems] = useState<SessionSummary[] | null>(null);
  const [total, setTotal] = useState(0);
  const [error, setError] = useState("");

  const load = (offset: number) =>
    api
      .sessions(PAGE, offset)
      .then((page) => {
        setItems((current) => (offset ? [...(current ?? []), ...page.items] : page.items));
        setTotal(page.total);
      })
      .catch((reason: Error) => setError(reason.message));

  useEffect(() => {
    load(0);
  }, []);

  return (
    <div className="page">
      <div className="narrow" style={{ maxWidth: 1180 }}>
        <div className="page-head">
          <h2>历史场次</h2>
          <span className="muted">{total ? `共 ${total} 场` : ""}</span>
        </div>
        {error && <Notice tone="critical">{error}</Notice>}
        <section className="card">
          {items === null ? (
            <Empty>加载中……</Empty>
          ) : items.length === 0 ? (
            <Empty>还没有记录。每一场盯播都会自动保存在这里，可以随时复盘。</Empty>
          ) : (
            <table className="data-table">
              <thead>
                <tr>
                  <th>开始时间</th>
                  <th>场次</th>
                  <th>话术框架</th>
                  <th className="num">时长</th>
                  <th className="num">提醒</th>
                  <th>状态</th>
                </tr>
              </thead>
              <tbody>
                {items.map((item) => (
                  <tr
                    key={item.id}
                    className="clickable"
                    tabIndex={0}
                    onClick={() => navigate(`/history/${item.id}`)}
                    onKeyDown={(event) => event.key === "Enter" && navigate(`/history/${item.id}`)}
                  >
                    <td className="tabular nowrap">{dateTime(item.started_at)}</td>
                    <td>{item.title}</td>
                    <td className="secondary">{item.framework_name}</td>
                    <td className="num nowrap">{duration(item.duration)}</td>
                    <td className="num">{item.alert_count}</td>
                    <td>
                      <Status tone={STATUS[item.status].tone}>{STATUS[item.status].label}</Status>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
          {items && items.length < total && (
            <div className="card-foot">
              <button className="btn" type="button" onClick={() => load(items.length)}>
                加载更多
              </button>
            </div>
          )}
        </section>
      </div>
    </div>
  );
}

function SessionView({ id }: { id: string }) {
  const { navigate, toast } = useApp();
  const [detail, setDetail] = useState<SessionDetail | null>(null);
  const [error, setError] = useState("");
  const { confirm, dialog } = useConfirm();

  useEffect(() => {
    api
      .sessionDetail(id)
      .then(setDetail)
      .catch((reason: Error) => setError(reason.message));
  }, [id]);

  const exportReport = async () => {
    if (!detail) return;
    try {
      const markdown = await api.reportMarkdown(id);
      const name = `盯播复盘-${detail.title}-${detail.started_at.slice(0, 10)}.md`.replace(/[\\/:*?"<>|]/g, "_");
      if (await saveText(name, markdown, "text/markdown")) toast({ severity: "info", title: "复盘报告已导出" });
    } catch (reason) {
      setError((reason as Error).message);
    }
  };

  const remove = async () => {
    if (!detail || !(await confirm("删除记录", `确定删除“${detail.title}”的全部记录吗？删除后无法恢复。`, "删除"))) return;
    try {
      await api.deleteSession(id);
      navigate("/history");
    } catch (reason) {
      setError((reason as Error).message);
    }
  };

  if (!detail) {
    return (
      <div className="page">
        {error ? <Notice tone="critical">{error}</Notice> : <Empty>加载中……</Empty>}
      </div>
    );
  }

  const summary = detail.summary;
  const pace = detail.framework.pace;
  const minuteRates = (summary?.metrics.minute_rates ?? []).map((m) => ({ minute: Math.floor(m.start / 60), rate: m.rate }));

  return (
    <div className="page">
      {dialog}
      <div className="stack" style={{ maxWidth: 1280, margin: "0 auto" }}>
        <div className="page-head" style={{ marginBottom: 0 }}>
          <div className="row">
            <button className="icon-btn" type="button" title="返回列表" onClick={() => navigate("/history")}>
              <ArrowLeft size={18} />
            </button>
            <div>
              <h2>{detail.title}</h2>
              <div className="muted small">
                {dateTime(detail.started_at)} · {duration(detail.duration)} · {detail.framework_name} · {detail.source}
                {detail.engine ? ` · ${detail.engine}` : ""} · 每 {span(detail.interval_seconds)}分析过去{" "}
                {span(detail.window_seconds)}
              </div>
            </div>
          </div>
          <div className="row">
            <Status tone={STATUS[detail.status].tone}>{STATUS[detail.status].label}</Status>
            <button className="btn" type="button" onClick={exportReport}>
              <FileDown size={15} />
              导出复盘报告
            </button>
            <button className="btn ghost" type="button" onClick={remove}>
              <Trash2 size={15} />
              删除
            </button>
          </div>
        </div>
        {error && <Notice tone="critical">{error}</Notice>}
        {detail.error && <Notice tone="critical">{detail.error}</Notice>}
        {detail.status === "interrupted" && (
          <Notice tone="warning">这一场在程序意外退出时中断，下面只有中断前保存的内容，没有全场总结。</Notice>
        )}

        {summary && (
          <section className="card">
            <div className="card-head">
              <h3>全场总结</h3>
              <span className="hint">
                说话 {duration(summary.metrics.speech_seconds)}，占 {percent(summary.metrics.speech_ratio)}
              </span>
            </div>
            <div className="card-body stack loose">
              <MetricTiles metrics={summary.metrics} pace={pace} forbidden={summary.forbidden.length} wide />
              <div className="detail-grid">
                <div>
                  <h4 style={{ marginBottom: 6 }}>需要改进</h4>
                  <FindingList findings={summary.findings} empty="全场没有发现问题" />
                </div>
                <div>
                  <h4 style={{ marginBottom: 6 }}>话术要点覆盖</h4>
                  <CoverageList coverage={summary.coverage} judged />
                </div>
              </div>
              <RateChart data={minuteRates} band={{ min: pace.min_rate, max: pace.max_rate }} />
            </div>
          </section>
        )}

        <div className="detail-grid">
          <section className="card">
            <div className="card-head">
              <h3>完整转写</h3>
              <span className="hint">{detail.segments.length} 句</span>
            </div>
            <div className="card-body" style={{ maxHeight: 640, overflow: "auto" }}>
              {detail.segments.length === 0 && <Empty>没有识别到讲话</Empty>}
              {detail.segments.map((segment) => (
                <div className="line" key={segment.id}>
                  <time>{clock(segment.start)}</time>
                  <div className="text">{segment.text}</div>
                </div>
              ))}
            </div>
          </section>
          <div className="stack">
            <section className="card">
              <div className="card-head">
                <h3>提醒记录</h3>
                <span className="hint">{detail.alerts.length} 条</span>
              </div>
              <div className="card-body" style={{ maxHeight: 420, overflow: "auto", paddingTop: 4 }}>
                <AlertList alerts={detail.alerts} />
              </div>
            </section>
            <section className="card">
              <div className="card-head">
                <h3>每次窗口分析</h3>
                <span className="hint">{detail.reports.filter((r) => !r.final).length} 次</span>
              </div>
              <div className="card-body" style={{ maxHeight: 360, overflow: "auto", padding: 0 }}>
                <table className="data-table">
                  <thead>
                    <tr>
                      <th>时间段</th>
                      <th className="num">语速</th>
                      <th className="num">说话占比</th>
                      <th className="num">问题</th>
                    </tr>
                  </thead>
                  <tbody>
                    {detail.reports
                      .filter((report) => !report.final)
                      .map((report) => (
                        <tr key={report.id ?? report.end}>
                          <td className="tabular">
                            {clock(report.start)}–{clock(report.end)}
                          </td>
                          <td className="num">{report.metrics.rate === null ? "—" : Math.round(report.metrics.rate)}</td>
                          <td className="num">{percent(report.metrics.speech_ratio)}</td>
                          <td className="num">{report.findings.length}</td>
                        </tr>
                      ))}
                  </tbody>
                </table>
              </div>
            </section>
          </div>
        </div>
      </div>
    </div>
  );
}
