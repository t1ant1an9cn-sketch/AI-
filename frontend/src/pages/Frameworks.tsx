import { ArrowDown, ArrowUp, Copy, FileDown, FileUp, Plus, Save, Trash2 } from "lucide-react";
import { useEffect, useRef, useState } from "react";

import { api, saveText } from "../api";
import { Empty, Notice, NumberInput, TagInput, useConfirm } from "../components/ui";
import { useApp } from "../context";
import { dateTime } from "../format";
import type { ForbiddenRule, Framework, FrameworkSummary, Point, Severity, Stage } from "../types";

const DEFAULT_FILLERS = ["嗯", "呃", "额", "那个那个", "然后然后", "就是说"];

function newId(): string {
  return crypto.randomUUID().replace(/-/g, "").slice(0, 8);
}

function blankPoint(): Point {
  return { id: newId(), name: "", keywords: [], description: "", required: true, min_mentions: 1 };
}

export function blankFramework(): Framework {
  return {
    id: "",
    name: "新话术框架",
    description: "",
    stages: [{ id: newId(), name: "产品介绍", points: [{ ...blankPoint(), name: "核心卖点" }] }],
    forbidden: [
      {
        id: newId(),
        name: "绝对化用语",
        keywords: ["最好", "最便宜", "全网最低", "第一", "顶级", "国家级"],
        severity: "high",
        suggestion: "广告法禁止绝对化用语，改用可以证明的描述",
      },
    ],
    fillers: [...DEFAULT_FILLERS],
    pace: { min_rate: 150, max_rate: 330, max_pause_seconds: 8, min_speech_ratio: 0.5, max_fillers_per_minute: 8 },
    check_order: false,
    llm_instructions: "",
  };
}

function move<T>(items: T[], index: number, offset: number): T[] {
  const target = index + offset;
  if (target < 0 || target >= items.length) return items;
  const copy = [...items];
  [copy[index], copy[target]] = [copy[target], copy[index]];
  return copy;
}

export function FrameworksPage() {
  const { route, navigate, toast } = useApp();
  const selected = route[1] ?? null;
  const [list, setList] = useState<FrameworkSummary[] | null>(null);
  const [error, setError] = useState("");
  const importer = useRef<HTMLInputElement>(null);

  const reload = () =>
    api
      .frameworks()
      .then(setList)
      .catch((reason: Error) => setError(reason.message));

  useEffect(() => {
    reload();
  }, []);

  useEffect(() => {
    if (!selected && list?.length) navigate(`/frameworks/${list[0].id}`);
  }, [selected, list]);

  const importFile = async (file: File | undefined) => {
    if (!file) return;
    setError("");
    try {
      const data = JSON.parse(await file.text()) as Framework;
      const created = await api.createFramework({ ...blankFramework(), ...data, id: "" });
      await reload();
      toast({ severity: "info", title: "已导入", detail: created.name });
      navigate(`/frameworks/${created.id}`);
    } catch (reason) {
      setError(reason instanceof SyntaxError ? "这不是有效的 JSON 文件" : (reason as Error).message);
    } finally {
      if (importer.current) importer.current.value = "";
    }
  };

  return (
    <div className="page">
      <div className="split">
        <aside className="card">
          <div className="card-head">
            <h3>话术框架</h3>
            <div className="row">
              <button className="icon-btn" type="button" title="导入 JSON" onClick={() => importer.current?.click()}>
                <FileUp size={16} />
              </button>
              <button className="icon-btn" type="button" title="新建" onClick={() => navigate("/frameworks/new")}>
                <Plus size={16} />
              </button>
            </div>
          </div>
          <input
            ref={importer}
            type="file"
            accept=".json,application/json"
            hidden
            onChange={(event) => importFile(event.target.files?.[0])}
          />
          <div className="list">
            {list === null && <div className="empty">加载中……</div>}
            {list?.map((item) => (
              <button
                key={item.id}
                type="button"
                className="list-item"
                aria-current={item.id === selected}
                onClick={() => navigate(`/frameworks/${item.id}`)}
              >
                <span className="name">{item.name}</span>
                <span className="meta">
                  {item.stage_count} 个环节 · {item.point_count} 个要点 · 更新于 {dateTime(item.updated_at)}
                </span>
              </button>
            ))}
            {selected === "new" && (
              <button type="button" className="list-item" aria-current>
                <span className="name">新话术框架</span>
                <span className="meta">未保存</span>
              </button>
            )}
          </div>
          {error && (
            <div className="card-body">
              <Notice tone="critical">{error}</Notice>
            </div>
          )}
        </aside>
        {selected ? (
          <Editor key={selected} id={selected} onChanged={reload} />
        ) : (
          <section className="card">
            <Empty>
              {list?.length === 0 ? "还没有话术框架。点右上角的“+”新建一个，或导入 JSON 文件。" : "选择左侧的框架开始编辑"}
            </Empty>
          </section>
        )}
      </div>
    </div>
  );
}

function Editor({ id, onChanged }: { id: string; onChanged(): Promise<void> }) {
  const { navigate, setLeaveGuard, toast } = useApp();
  const [saved, setSaved] = useState<Framework | null>(null);
  const [draft, setDraft] = useState<Framework | null>(id === "new" ? blankFramework() : null);
  const [error, setError] = useState("");
  const [saving, setSaving] = useState(false);
  const { confirm, dialog } = useConfirm();

  useEffect(() => {
    if (id === "new") return;
    api
      .framework(id)
      .then((framework) => {
        setSaved(framework);
        setDraft(framework);
      })
      .catch((reason: Error) => setError(reason.message));
  }, [id]);

  const dirty = draft !== null && JSON.stringify(draft) !== JSON.stringify(saved);
  useEffect(() => {
    setLeaveGuard(dirty ? () => window.confirm("修改还没有保存，确定离开吗？") : null);
    return () => setLeaveGuard(null);
  }, [dirty]);

  if (!draft) {
    return (
      <section className="card">
        {error ? (
          <div className="card-body">
            <Notice tone="critical">{error}</Notice>
          </div>
        ) : (
          <Empty>加载中……</Empty>
        )}
      </section>
    );
  }

  const set = (patch: Partial<Framework>) => setDraft({ ...draft, ...patch });
  const setStage = (index: number, stage: Stage) =>
    set({ stages: draft.stages.map((item, i) => (i === index ? stage : item)) });
  const setRule = (index: number, rule: ForbiddenRule) =>
    set({ forbidden: draft.forbidden.map((item, i) => (i === index ? rule : item)) });

  const save = async () => {
    setSaving(true);
    setError("");
    try {
      if (id === "new") {
        const created = await api.createFramework(draft);
        setSaved(created);
        setDraft(created);
        setLeaveGuard(null);
        await onChanged();
        navigate(`/frameworks/${created.id}`);
      } else {
        const updated = await api.updateFramework(draft);
        setSaved(updated);
        setDraft(updated);
        await onChanged();
      }
      toast({ severity: "info", title: "已保存", detail: draft.name });
    } catch (reason) {
      setError((reason as Error).message);
    } finally {
      setSaving(false);
    }
  };

  const duplicate = async () => {
    try {
      const copy = await api.createFramework({ ...draft, name: `${draft.name}（副本）` });
      await onChanged();
      navigate(`/frameworks/${copy.id}`);
    } catch (reason) {
      setError((reason as Error).message);
    }
  };

  const remove = async () => {
    if (!(await confirm("删除话术框架", `确定删除“${draft.name}”吗？历史场次里保存的副本不受影响。`, "删除"))) return;
    try {
      await api.deleteFramework(id);
      setLeaveGuard(null);
      await onChanged();
      navigate("/frameworks");
    } catch (reason) {
      setError((reason as Error).message);
    }
  };

  const exportJson = async () => {
    const { id: _id, ...content } = draft;
    await saveText(`${draft.name}.json`, JSON.stringify(content, null, 2), "application/json");
  };

  return (
    <section className="card">
      {dialog}
      <div className="card-head">
        <h2>{draft.name || "未命名框架"}</h2>
        <div className="row">
          {id !== "new" && (
            <>
              <button className="btn small ghost" type="button" onClick={exportJson}>
                <FileDown size={14} />
                导出
              </button>
              <button className="btn small ghost" type="button" onClick={duplicate}>
                <Copy size={14} />
                复制一份
              </button>
              <button className="btn small ghost" type="button" onClick={remove}>
                <Trash2 size={14} />
                删除
              </button>
            </>
          )}
        </div>
      </div>

      <div className="card-body stack loose">
        <div className="editor-section">
          <div className="field">
            <label htmlFor="fw-name">名称</label>
            <input
              id="fw-name"
              className="input"
              value={draft.name}
              maxLength={80}
              onChange={(event) => set({ name: event.target.value })}
            />
          </div>
          <div className="field">
            <label htmlFor="fw-desc">说明</label>
            <textarea
              id="fw-desc"
              className="textarea"
              rows={2}
              value={draft.description}
              placeholder="适用的商品、场景或注意事项"
              onChange={(event) => set({ description: event.target.value })}
            />
          </div>
        </div>

        <div className="editor-section">
          <div className="section-title">
            <div>
              <h3>话术环节与要点</h3>
              <p className="muted small">
                每个要点说到任一关键词就算讲到一次。识别出的同音字也能匹配，例如“食品级”也能匹配到“食品及”。
              </p>
            </div>
            <button
              className="btn small"
              type="button"
              onClick={() => set({ stages: [...draft.stages, { id: newId(), name: "", points: [blankPoint()] }] })}
            >
              <Plus size={14} />
              添加环节
            </button>
          </div>
          {draft.stages.map((stage, index) => (
            <StageEditor
              key={stage.id}
              stage={stage}
              position={index + 1}
              onChange={(next) => setStage(index, next)}
              onMove={(offset) => set({ stages: move(draft.stages, index, offset) })}
              onRemove={() => set({ stages: draft.stages.filter((_, i) => i !== index) })}
            />
          ))}
          {!draft.stages.length && <div className="muted">还没有环节。</div>}
        </div>

        <div className="editor-section">
          <div className="section-title">
            <div>
              <h3>违规表达</h3>
              <p className="muted small">一旦说出口立即提醒，不等分析窗口。</p>
            </div>
            <button
              className="btn small"
              type="button"
              onClick={() =>
                set({
                  forbidden: [
                    ...draft.forbidden,
                    { id: newId(), name: "", keywords: [], severity: "high", suggestion: "" },
                  ],
                })
              }
            >
              <Plus size={14} />
              添加一类
            </button>
          </div>
          {draft.forbidden.map((rule, index) => (
            <div className="point-box" key={rule.id}>
              <div className="stack">
                <div className="grid-2">
                  <div className="field">
                    <label>类别名称</label>
                    <input
                      className="input"
                      value={rule.name}
                      placeholder="例如：绝对化用语"
                      onChange={(event) => setRule(index, { ...rule, name: event.target.value })}
                    />
                  </div>
                  <div className="field">
                    <label>严重程度</label>
                    <select
                      className="select"
                      value={rule.severity}
                      onChange={(event) => setRule(index, { ...rule, severity: event.target.value as Severity })}
                    >
                      <option value="high">高：立即整改</option>
                      <option value="medium">中：需要注意</option>
                      <option value="low">低：提示</option>
                    </select>
                  </div>
                </div>
                <div className="field">
                  <label>关键词</label>
                  <TagInput
                    label="违规关键词"
                    value={rule.keywords}
                    placeholder="输入后按回车，或用逗号分隔"
                    onChange={(keywords) => setRule(index, { ...rule, keywords })}
                  />
                </div>
                <div className="field">
                  <label>整改建议</label>
                  <input
                    className="input"
                    value={rule.suggestion}
                    placeholder="提醒时一并显示"
                    onChange={(event) => setRule(index, { ...rule, suggestion: event.target.value })}
                  />
                </div>
              </div>
              <div className="tools">
                <button
                  className="icon-btn"
                  type="button"
                  title="删除"
                  onClick={() => set({ forbidden: draft.forbidden.filter((_, i) => i !== index) })}
                >
                  <Trash2 size={15} />
                </button>
              </div>
            </div>
          ))}
        </div>

        <div className="editor-section">
          <h3>口头禅</h3>
          <TagInput
            label="口头禅"
            value={draft.fillers}
            placeholder="例如：嗯、那个那个"
            onChange={(fillers) => set({ fillers })}
          />
          <p className="muted small">只按原文逐字统计。“然后”“就是”这类常用词单独列出会误报很多，建议写成重复的形式。</p>
        </div>

        <div className="editor-section">
          <h3>节奏标准</h3>
          <div className="grid-3">
            <PaceField label="语速下限（字/分钟）" value={draft.pace.min_rate} min={30} max={600} onChange={(min_rate) => set({ pace: { ...draft.pace, min_rate } })} />
            <PaceField label="语速上限（字/分钟）" value={draft.pace.max_rate} min={30} max={800} onChange={(max_rate) => set({ pace: { ...draft.pace, max_rate } })} />
            <PaceField label="停顿多少秒算冷场" value={draft.pace.max_pause_seconds} min={1} max={120} onChange={(max_pause_seconds) => set({ pace: { ...draft.pace, max_pause_seconds } })} />
            <PaceField
              label="说话时间占比下限（%）"
              value={Math.round(draft.pace.min_speech_ratio * 100)}
              min={0}
              max={100}
              onChange={(value) => set({ pace: { ...draft.pace, min_speech_ratio: value / 100 } })}
            />
            <PaceField label="每分钟口头禅上限" value={draft.pace.max_fillers_per_minute} min={0} max={100} onChange={(max_fillers_per_minute) => set({ pace: { ...draft.pace, max_fillers_per_minute } })} />
          </div>
          <p className="muted small">带货直播一般在 200–300 字/分钟。语速只按说话时间计算，停顿不计入。</p>
        </div>

        <div className="editor-section">
          <h3>其他</h3>
          <label className="check">
            <input
              type="checkbox"
              checked={draft.check_order}
              onChange={(event) => set({ check_order: event.target.checked })}
            />
            检查环节顺序（循环讲品时容易误报，默认关闭）
          </label>
          <div className="field">
            <label htmlFor="fw-llm">给大模型的额外要求</label>
            <textarea
              id="fw-llm"
              className="textarea"
              rows={3}
              value={draft.llm_instructions}
              placeholder="例如：价格必须是 99 元，赠品是杯套；说错时请指出。"
              onChange={(event) => set({ llm_instructions: event.target.value })}
            />
            <span className="help">开启大模型审核后生效。</span>
          </div>
        </div>
      </div>

      <div className="sticky-actions">
        <button className="btn primary" type="button" onClick={save} disabled={saving || !dirty}>
          <Save size={15} />
          {saving ? "保存中……" : id === "new" ? "创建" : "保存"}
        </button>
        {dirty && id !== "new" && (
          <button className="btn ghost" type="button" onClick={() => setDraft(saved)}>
            撤销修改
          </button>
        )}
        {error ? <Notice tone="critical">{error}</Notice> : dirty && <span className="muted small">有未保存的修改</span>}
      </div>
    </section>
  );
}

function PaceField({
  label,
  value,
  min,
  max,
  onChange,
}: {
  label: string;
  value: number;
  min: number;
  max: number;
  onChange(value: number): void;
}) {
  return (
    <div className="field">
      <label>{label}</label>
      <NumberInput label={label} value={value} min={min} max={max} onChange={onChange} />
    </div>
  );
}

function StageEditor({
  stage,
  position,
  onChange,
  onMove,
  onRemove,
}: {
  stage: Stage;
  position: number;
  onChange(stage: Stage): void;
  onMove(offset: number): void;
  onRemove(): void;
}) {
  const setPoint = (index: number, point: Point) =>
    onChange({ ...stage, points: stage.points.map((item, i) => (i === index ? point : item)) });

  return (
    <div className="stage-box">
      <div className="head">
        <span className="badge tabular">{position}</span>
        <input
          className="input grow"
          aria-label="环节名称"
          placeholder="环节名称，例如：产品介绍"
          value={stage.name}
          onChange={(event) => onChange({ ...stage, name: event.target.value })}
        />
        <button className="icon-btn" type="button" title="上移" onClick={() => onMove(-1)}>
          <ArrowUp size={15} />
        </button>
        <button className="icon-btn" type="button" title="下移" onClick={() => onMove(1)}>
          <ArrowDown size={15} />
        </button>
        <button className="icon-btn" type="button" title="删除环节" onClick={onRemove}>
          <Trash2 size={15} />
        </button>
      </div>
      <div className="body">
        {stage.points.map((point, index) => (
          <div className="point-box" key={point.id}>
            <div className="stack">
              <div className="row wrap" style={{ alignItems: "flex-end" }}>
                <div className="field grow">
                  <label>要点</label>
                  <input
                    className="input"
                    value={point.name}
                    placeholder="例如：材质"
                    onChange={(event) => setPoint(index, { ...point, name: event.target.value })}
                  />
                </div>
                <div className="field">
                  <label>至少提到</label>
                  <div className="row">
                    <NumberInput
                      label="至少提到次数"
                      value={point.min_mentions}
                      min={1}
                      max={20}
                      onChange={(min_mentions) => setPoint(index, { ...point, min_mentions })}
                    />
                    <span className="muted">次</span>
                  </div>
                </div>
                <label className="check" style={{ height: 34 }}>
                  <input
                    type="checkbox"
                    checked={point.required}
                    onChange={(event) => setPoint(index, { ...point, required: event.target.checked })}
                  />
                  必讲
                </label>
              </div>
              <div className="field">
                <label>关键词</label>
                <TagInput
                  label="要点关键词"
                  value={point.keywords}
                  placeholder="说到任意一个就算讲到，例如：316不锈钢、食品级"
                  onChange={(keywords) => setPoint(index, { ...point, keywords })}
                />
              </div>
              <div className="field">
                <label>应该怎么讲</label>
                <input
                  className="input"
                  value={point.description}
                  placeholder="给大模型和提醒用的说明，例如：说明内胆是 316 食品级不锈钢"
                  onChange={(event) => setPoint(index, { ...point, description: event.target.value })}
                />
              </div>
            </div>
            <div className="tools">
              <button
                className="icon-btn"
                type="button"
                title="上移"
                onClick={() => onChange({ ...stage, points: move(stage.points, index, -1) })}
              >
                <ArrowUp size={15} />
              </button>
              <button
                className="icon-btn"
                type="button"
                title="下移"
                onClick={() => onChange({ ...stage, points: move(stage.points, index, 1) })}
              >
                <ArrowDown size={15} />
              </button>
              <button
                className="icon-btn"
                type="button"
                title="删除要点"
                onClick={() => onChange({ ...stage, points: stage.points.filter((_, i) => i !== index) })}
              >
                <Trash2 size={15} />
              </button>
            </div>
          </div>
        ))}
        <div>
          <button
            className="btn small ghost"
            type="button"
            onClick={() => onChange({ ...stage, points: [...stage.points, blankPoint()] })}
          >
            <Plus size={14} />
            添加要点
          </button>
        </div>
      </div>
    </div>
  );
}
