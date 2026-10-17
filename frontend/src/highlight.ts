import type { Highlight } from "./types";

export interface Piece {
  text: string;
  mark?: Highlight;
}

const PRIORITY: Record<Highlight["kind"], number> = { forbidden: 0, point: 1, filler: 2 };

/** 把一句话按高亮切成若干段。高亮互相重叠时，违规表达优先，其次是要点，最后是口头禅。 */
export function splitHighlights(text: string, highlights: Highlight[]): Piece[] {
  const chosen: Highlight[] = [];
  const ordered = [...highlights].sort((a, b) => PRIORITY[a.kind] - PRIORITY[b.kind] || a.start - b.start);
  for (const item of ordered) {
    const start = Math.max(0, item.start);
    const end = Math.min(text.length, item.end);
    if (end <= start) continue;
    if (chosen.some((taken) => start < taken.end && taken.start < end)) continue;
    chosen.push({ ...item, start, end });
  }
  chosen.sort((a, b) => a.start - b.start);
  const pieces: Piece[] = [];
  let position = 0;
  for (const mark of chosen) {
    if (mark.start > position) pieces.push({ text: text.slice(position, mark.start) });
    pieces.push({ text: text.slice(mark.start, mark.end), mark });
    position = mark.end;
  }
  if (position < text.length) pieces.push({ text: text.slice(position) });
  return pieces;
}
