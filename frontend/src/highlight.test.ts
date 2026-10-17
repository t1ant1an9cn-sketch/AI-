import { describe, expect, it } from "vitest";

import { splitHighlights } from "./highlight";
import type { Highlight } from "./types";

function mark(start: number, end: number, kind: Highlight["kind"]): Highlight {
  return { start, end, kind, ref: kind, label: kind, exact: true };
}

describe("splitHighlights", () => {
  it("没有高亮时整句保持原样", () => {
    expect(splitHighlights("你好", [])).toEqual([{ text: "你好" }]);
  });

  it("违规表达优先于重叠的要点", () => {
    // 不锈钢全网最低：要点“锈钢全”(1–4) 与违规“全网最”(3–6) 重叠
    const pieces = splitHighlights("不锈钢全网最低", [mark(1, 4, "point"), mark(3, 6, "forbidden")]);
    expect(pieces.map((piece) => [piece.text, piece.mark?.kind ?? null])).toEqual([
      ["不锈钢", null],
      ["全网最", "forbidden"],
      ["低", null],
    ]);
  });

  it("切分后拼回去仍是原文，越界的高亮会被裁剪或忽略", () => {
    const text = "嗯，这个杯子很好";
    const pieces = splitHighlights(text, [mark(0, 1, "filler"), mark(10, 20, "point")]);
    expect(pieces.map((piece) => piece.text).join("")).toBe(text);
    expect(pieces[0].mark?.kind).toBe("filler");
    expect(pieces.some((piece) => piece.mark?.kind === "point")).toBe(false);
  });
});
