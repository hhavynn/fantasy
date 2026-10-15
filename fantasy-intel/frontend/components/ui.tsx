import type { ReactNode } from "react";

const TONE: Record<string, string> = {
  BUY: "good", CLAIM: "good", UP: "good", HIGH: "info", ALIGNED: "",
  SELL: "bad", DOWN: "bad", IGNORE: "", LOW: "",
  HOLD: "", FLAT: "", MEDIUM: "warn", WAIT: "warn", MONITOR: "warn",
  INSUFFICIENT: "", USAGE_EXCEEDS_PRODUCTION: "good", PRODUCTION_EXCEEDS_USAGE: "bad",
  BELOW_EXPECTATION: "info", ABOVE_EXPECTATION: "warn",
  OUT: "bad", INJURY_RESERVE: "bad", SUSPENSION: "bad", DOUBTFUL: "bad", QUESTIONABLE: "warn",
  Out: "bad", Doubtful: "bad", Questionable: "warn",
};

const SHORT: Record<string, string> = {
  USAGE_EXCEEDS_PRODUCTION: "Usage > production",
  PRODUCTION_EXCEEDS_USAGE: "Production > usage",
  INSUFFICIENT: "Insufficient data",
  ABOVE_EXPECTATION: "Production above xFP",
  BELOW_EXPECTATION: "Production below xFP",
};

/** Availability designations render as fantasy-standard letter codes; the full word goes in the tooltip. */
const STATUS: Record<string, { code: string; full: string }> = {
  QUESTIONABLE: { code: "Q", full: "Questionable" }, Questionable: { code: "Q", full: "Questionable" },
  DOUBTFUL: { code: "D", full: "Doubtful" }, Doubtful: { code: "D", full: "Doubtful" },
  OUT: { code: "O", full: "Out" }, Out: { code: "O", full: "Out" },
  INJURY_RESERVE: { code: "IR", full: "Injured reserve" }, IR: { code: "IR", full: "Injured reserve" },
  SUSPENSION: { code: "SSPD", full: "Suspended" },
};

/** Acronyms that stay upper case when other ALL_CAPS enum values are sentence-cased. */
const KEEP_CASE = new Set(["BYE", "FA", "FAAB", "PPR", "NFL", "ESPN"]);

function display(value: string): string {
  if (SHORT[value]) return SHORT[value];
  if (KEEP_CASE.has(value) || !/^[A-Z]{2,}(_[A-Z]+)*$/.test(value)) return value;
  const words = value.toLowerCase().replace(/_/g, " ");
  return words[0].toUpperCase() + words.slice(1);
}

export function Badge({ value, title, tone }: { value?: string | null; title?: string; tone?: string }) {
  if (!value) return <span className="missing" title={title ?? "Not available"}>—</span>;
  const t = tone ?? TONE[value] ?? "";
  const status = STATUS[value];
  if (status) {
    return (
      <span className={`badge status ${tone ?? TONE[value] ?? "bad"}`} title={title ? `${status.full} · ${title}` : status.full}>
        {status.code}
      </span>
    );
  }
  // Explicit tones mark caller-supplied text (team names, custom labels), which keeps its own casing.
  return (
    <span className={`badge ${t}`} title={title}>
      {tone === undefined ? display(value) : SHORT[value] ?? value}
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

/** 0–max value as the number plus ten ticks (countable at a glance, comparable across rows). */
export function Score({ value, max = 10 }: { value?: number | null; max?: number }) {
  if (value === null || value === undefined) return <span className="missing" title="Not computed">—</span>;
  const on = Math.round(Math.max(0, Math.min(1, value / max)) * 10);
  return (
    <span className="score">
      <span className="v">{value.toFixed(1)}</span>
      <span className="ticks" role="img" aria-label={`${value.toFixed(1)} of ${max}`}>
        {Array.from({ length: 10 }, (_, i) => <i key={i} className={i < on ? "on" : undefined} />)}
      </span>
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
