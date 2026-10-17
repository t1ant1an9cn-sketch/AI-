import { ArrowDown } from "lucide-react";
import { useEffect, useLayoutEffect, useRef, useState } from "react";

import { clock, wallClock } from "../format";
import { splitHighlights } from "../highlight";
import type { Highlight, TranscriptLine } from "../types";

export function HighlightedText({ text, highlights }: { text: string; highlights: Highlight[] }) {
  return (
    <>
      {splitHighlights(text, highlights).map((piece, index) =>
        piece.mark ? (
          <mark
            key={index}
            className={`${piece.mark.kind}${piece.mark.exact ? "" : " homophone"}`}
            title={piece.mark.exact ? piece.mark.label : `${piece.mark.label}（同音匹配，可能是识别错字）`}
          >
            {piece.text}
          </mark>
        ) : (
          <span key={index}>{piece.text}</span>
        ),
      )}
    </>
  );
}

export function TranscriptLegend() {
  return (
    <div className="legend" aria-label="高亮说明">
      <span>
        <mark className="point">要点</mark>讲到了框架要点
      </span>
      <span>
        <mark className="forbidden">违规</mark>不能说的话
      </span>
      <span>
        <mark className="filler">口头禅</mark>
      </span>
      <span>
        <mark className="point homophone">虚线</mark>同音匹配
      </span>
    </div>
  );
}

/** 实时转写：新句子到达时自动滚到底；用户往上翻看时不打扰，给出“回到最新”。 */
export function Transcript({
  lines,
  partial,
  startedAt,
  live,
}: {
  lines: TranscriptLine[];
  partial: string;
  startedAt: string;
  live: boolean;
}) {
  const box = useRef<HTMLDivElement>(null);
  const [following, setFollowing] = useState(true);
  const lastId = lines.length ? lines[lines.length - 1].segment.id : 0;

  useLayoutEffect(() => {
    if (following && box.current) box.current.scrollTop = box.current.scrollHeight;
  }, [lastId, partial, following]);

  useEffect(() => setFollowing(true), [startedAt]);

  const onScroll = () => {
    const element = box.current;
    if (!element) return;
    setFollowing(element.scrollHeight - element.scrollTop - element.clientHeight < 60);
  };

  if (!lines.length && !partial) {
    return (
      <div className="transcript" ref={box}>
        <div className="empty">{live ? "正在听……主播开口后这里会实时出现文字" : "这一场没有识别到讲话"}</div>
      </div>
    );
  }

  return (
    <div className="transcript" ref={box} onScroll={onScroll} aria-live="off">
      {lines.map(({ segment, highlights }) => (
        <div className="line" key={segment.id}>
          <time title={wallClock(startedAt, segment.start)}>{clock(segment.start)}</time>
          <div className="text">
            <HighlightedText text={segment.text} highlights={highlights} />
          </div>
        </div>
      ))}
      {partial && (
        <div className="line partial">
          <time>识别中</time>
          <div className="text">{partial}</div>
        </div>
      )}
      {!following && (
        <button
          className="btn small jump-latest"
          type="button"
          onClick={() => {
            setFollowing(true);
            if (box.current) box.current.scrollTop = box.current.scrollHeight;
          }}
        >
          <ArrowDown size={14} />
          回到最新
        </button>
      )}
    </div>
  );
}
