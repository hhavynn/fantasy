import type { ReactNode } from "react";

const TONE: Record<string, string> = {
  BUY: "good", CLAIM: "good", UP: "good", HIGH: "info", ALIGNED: "",
  SELL: "bad", DOWN: "bad", IGNORE: "", LOW: "",
  HOLD: "", FLAT: "", MEDIUM: "warn", WAIT: "warn", MONITOR: "warn",
  INSUFFICIENT: "", USAGE_EXCEEDS_PRODUCTION: "good", PRODUCTION_EXCEEDS_USAGE: "bad",
  OUT: "bad", INJURY_RESERVE: "bad", SUSPENSION: "bad", DOUBTFUL: "bad", QUESTIONABLE: "warn",
  Out: "bad", Doubtful: "bad", Questionable: "warn",
};

const SHORT: Record<string, string> = {
  USAGE_EXCEEDS_PRODUCTION: "usage > production",
  PRODUCTION_EXCEEDS_USAGE: "production > usage",
  INJURY_RESERVE: "IR",
  INSUFFICIENT: "insufficient data",
};

export function Badge({ value, title, tone }: { value?: string | null; title?: string; tone?: string }) {
  if (!value) return <span className="missing" title={title ?? "Not available"}>—</span>;
  const t = tone ?? TONE[value] ?? "";
  return (
    <span className={`badge ${t}`} title={title}>
      {SHORT[value] ?? value}
    </span>
  );
}

export function Pos({ value }: { value?: string | null }) {
  return <span className="badge pos">{value ?? "?"}</span>;
}

export function TrendArrow({ label }: { label?: string | null }) {
  if (label === "UP") return <span className="up" title="Usage trending up">▲</span>;
  if (label === "DOWN") return <span className="down" title="Usage trending down">▼</span>;
  if (label === "FLAT") return <span className="faint" title="Usage flat">▬</span>;
  return <span className="missing" title="Not enough games for a trend">·</span>;
}

export function ErrorBox({ error }: { error: string }) {
  return (
    <div className="callout error">
      <b>Couldn&apos;t load data.</b> {error}
    </div>
  );
}

export function Callout({ children }: { children: ReactNode }) {
  return <div className="callout">{children}</div>;
}

export function Score({ value, max = 10 }: { value?: number | null; max?: number }) {
  if (value === null || value === undefined) return <span className="missing" title="Not computed">—</span>;
  const w = Math.max(0, Math.min(100, (value / max) * 100));
  return (
    <span className="row-flex" style={{ gap: 6 }}>
      <span style={{ minWidth: 28, textAlign: "right" }} className="num">{value.toFixed(1)}</span>
      <span className="meter" style={{ width: 46 }}><div style={{ width: `${w}%` }} /></span>
    </span>
  );
}

/** Tiny inline line chart. `null` points are gaps (missing), never zero. */
export function Sparkline({ points, width = 90, height = 22, max }: {
  points: (number | null)[]; width?: number; height?: number; max?: number;
}) {
  const vals = points.filter((p): p is number => p !== null);
  if (vals.length < 2) return <span className="missing" title="Not enough games">—</span>;
  const hi = max ?? Math.max(...vals, 0.0001);
  const step = width / Math.max(1, points.length - 1);
  const segs: string[] = [];
  let cur = "";
  points.forEach((p, i) => {
    if (p === null) {
      if (cur) segs.push(cur);
      cur = "";
      return;
    }
    const x = (i * step).toFixed(1);
    const y = (height - 2 - (p / hi) * (height - 4)).toFixed(1);
    cur += `${cur ? "L" : "M"}${x},${y}`;
  });
  if (cur) segs.push(cur);
  return (
    <svg className="spark" width={width} height={height} viewBox={`0 0 ${width} ${height}`}>
      {segs.map((d, i) => (
        <path key={i} d={d} fill="none" stroke="var(--accent)" strokeWidth={1.5} />
      ))}
    </svg>
  );
}
