import { Table2, TrendingUp } from "lucide-react";
import { type KeyboardEvent, type PointerEvent, useEffect, useRef, useState } from "react";

import { clock } from "../format";

export interface RatePoint {
  minute: number; // 会话第几分钟（从 0 开始）
  rate: number | null; // 字/分钟；说话不足时为空
}

const MARGIN = { top: 14, right: 44, bottom: 26, left: 38 };

function niceScale(maxValue: number): { max: number; ticks: number[] } {
  for (const step of [50, 100, 200, 500]) {
    const max = Math.max(step, Math.ceil(maxValue / step) * step);
    if (max / step <= 5) return { max, ticks: Array.from({ length: max / step + 1 }, (_, i) => i * step) };
  }
  return { max: maxValue, ticks: [0, maxValue] };
}

function judge(rate: number | null, band: { min: number; max: number }): string {
  if (rate === null) return "说话不足 10 秒，未计算";
  if (rate > band.max) return "偏快";
  if (rate < band.min) return "偏慢";
  return "在建议区间内";
}

/** 每分钟语速折线：单一系列，灰色带表示建议区间；悬停或用方向键查看每分钟数值，可切换为表格。 */
export function RateChart({
  data,
  band,
  height = 190,
}: {
  data: RatePoint[];
  band: { min: number; max: number };
  height?: number;
}) {
  const box = useRef<HTMLDivElement>(null);
  const [width, setWidth] = useState(520);
  const [active, setActive] = useState<number | null>(null);
  const [table, setTable] = useState(false);

  useEffect(() => {
    const element = box.current;
    if (!element || typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(([entry]) => setWidth(Math.max(260, entry.contentRect.width)));
    observer.observe(element);
    return () => observer.disconnect();
  }, []);

  const points = [...data].sort((a, b) => a.minute - b.minute);
  const values = points.map((p) => p.rate).filter((rate): rate is number => rate !== null);
  const latest = [...points].reverse().find((p) => p.rate !== null) ?? null;

  const header = (
    <div className="section-title">
      <h4>每分钟语速（字/分钟）</h4>
      {points.length > 0 && (
        <button className="btn ghost small" type="button" onClick={() => setTable(!table)} aria-pressed={table}>
          {table ? <TrendingUp size={14} /> : <Table2 size={14} />}
          {table ? "图表" : "表格"}
        </button>
      )}
    </div>
  );

  if (!points.length) {
    return (
      <div ref={box}>
        {header}
        <div className="empty">开播满 1 分钟后显示语速变化</div>
      </div>
    );
  }

  if (table) {
    return (
      <div ref={box}>
        {header}
        <table className="data-table">
          <thead>
            <tr>
              <th>时间段</th>
              <th className="num">语速</th>
              <th>判断</th>
            </tr>
          </thead>
          <tbody>
            {points.map((p) => (
              <tr key={p.minute}>
                <td className="tabular">
                  {clock(p.minute * 60)}–{clock(p.minute * 60 + 60)}
                </td>
                <td className="num">{p.rate === null ? "—" : Math.round(p.rate)}</td>
                <td>{judge(p.rate, band)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    );
  }

  const plotWidth = width - MARGIN.left - MARGIN.right;
  const plotHeight = height - MARGIN.top - MARGIN.bottom;
  const scale = niceScale(Math.max(band.max * 1.15, ...values));
  const first = points[0].minute;
  const last = points[points.length - 1].minute;
  const x = (minute: number) =>
    last === first ? MARGIN.left + plotWidth / 2 : MARGIN.left + ((minute - first) / (last - first)) * plotWidth;
  const y = (rate: number) => MARGIN.top + plotHeight - (rate / scale.max) * plotHeight;

  // 遇到说话不足的分钟断开折线，不把两端硬连起来。
  let path = "";
  let pen = false;
  for (const p of points) {
    if (p.rate === null) {
      pen = false;
      continue;
    }
    path += `${pen ? "L" : "M"}${x(p.minute).toFixed(1)},${y(p.rate).toFixed(1)}`;
    pen = true;
  }
  const lonely = points.filter((p, i) => p.rate !== null && points[i - 1]?.rate == null && points[i + 1]?.rate == null);

  const labelEvery = Math.max(1, Math.ceil(points.length / 6));
  const activePoint = active === null ? null : points[active];

  const pick = (event: PointerEvent<SVGRectElement>) => {
    const bounds = event.currentTarget.getBoundingClientRect();
    const position = event.clientX - bounds.left + MARGIN.left;
    let nearest = 0;
    points.forEach((p, i) => {
      if (Math.abs(x(p.minute) - position) < Math.abs(x(points[nearest].minute) - position)) nearest = i;
    });
    setActive(nearest);
  };

  const onKey = (event: KeyboardEvent<SVGSVGElement>) => {
    if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return;
    event.preventDefault();
    const current = active ?? points.length - 1;
    setActive(Math.max(0, Math.min(points.length - 1, current + (event.key === "ArrowLeft" ? -1 : 1))));
  };

  const summary = latest
    ? `最近一分钟约 ${Math.round(latest.rate!)} 字/分钟，建议 ${band.min}–${band.max}`
    : "还没有足够的讲话来计算语速";

  return (
    <div ref={box}>
      {header}
      <div className="chart">
        <svg
          height={height}
          role="img"
          aria-label={`每分钟语速。${summary}。按左右方向键查看每分钟数值。`}
          tabIndex={0}
          onKeyDown={onKey}
          onFocus={() => setActive(points.length - 1)}
          onBlur={() => setActive(null)}
        >
          <rect
            className="band"
            x={MARGIN.left}
            width={plotWidth}
            y={y(band.max)}
            height={Math.max(0, y(band.min) - y(band.max))}
          />
          <text className="band-label" x={MARGIN.left + 6} y={y(band.max) - 5}>
            建议 {band.min}–{band.max}
          </text>
          {scale.ticks.map((tick) => (
            <g key={tick}>
              <line
                className={tick === 0 ? "axis-line" : "grid-line"}
                x1={MARGIN.left}
                x2={MARGIN.left + plotWidth}
                y1={y(tick)}
                y2={y(tick)}
              />
              <text className="tick" x={MARGIN.left - 8} y={y(tick) + 4} textAnchor="end">
                {tick}
              </text>
            </g>
          ))}
          {points.map((p, i) =>
            i % labelEvery === 0 || i === points.length - 1 ? (
              <text key={p.minute} className="tick" x={x(p.minute)} y={height - 6} textAnchor="middle">
                {clock(p.minute * 60)}
              </text>
            ) : null,
          )}
          {activePoint && (
            <line
              className="crosshair"
              x1={x(activePoint.minute)}
              x2={x(activePoint.minute)}
              y1={MARGIN.top}
              y2={MARGIN.top + plotHeight}
            />
          )}
          <path className="series" d={path} />
          {lonely.map((p) => (
            <circle key={p.minute} className="marker" cx={x(p.minute)} cy={y(p.rate!)} r={4} />
          ))}
          {latest && (
            <>
              <circle className="marker" cx={x(latest.minute)} cy={y(latest.rate!)} r={4} />
              <text className="value-label" x={x(latest.minute) + 9} y={y(latest.rate!) + 4}>
                {Math.round(latest.rate!)}
              </text>
            </>
          )}
          {activePoint && activePoint.rate !== null && activePoint !== latest && (
            <circle className="marker" cx={x(activePoint.minute)} cy={y(activePoint.rate)} r={4} />
          )}
          <rect
            x={MARGIN.left}
            y={MARGIN.top}
            width={plotWidth}
            height={plotHeight}
            fill="transparent"
            onPointerMove={pick}
            onPointerLeave={() => setActive(null)}
          />
        </svg>
        {activePoint && (
          <div
            className="chart-tooltip"
            style={{ left: Math.min(Math.max(0, x(activePoint.minute) + 12), width - 150), top: MARGIN.top }}
          >
            <div>
              <span className="key" />
              <strong className="tabular">{activePoint.rate === null ? "—" : Math.round(activePoint.rate)}</strong>
              <span className="muted"> 字/分钟</span>
            </div>
            <div className="secondary tabular">
              {clock(activePoint.minute * 60)}–{clock(activePoint.minute * 60 + 60)}
            </div>
            <div className="secondary">{judge(activePoint.rate, band)}</div>
          </div>
        )}
      </div>
    </div>
  );
}
