import type { ReactNode } from "react";
import { ChartFrame } from "./ChartFrame";
import { extent, linearScale, niceTicks } from "./scale";
import { phoneFont, signedFormat, textWidth } from "./shared";
import styles from "./charts.module.css";

export type IntervalTone = "default" | "positive" | "negative" | "muted";

export type IntervalDatum = {
  label: string;
  /** Point estimate; `null` draws a muted dash (missing, not zero). */
  value: number | null;
  /** Interval bounds as given (never recomputed); `null` draws the point alone. */
  lo: number | null;
  hi: number | null;
  tone?: IntervalTone;
  /** Short text after the value, e.g. the sample size. */
  note?: string;
};

const TONE_COLOR: Record<IntervalTone, string> = {
  default: "var(--chart-1)",
  positive: "var(--chart-positive)",
  negative: "var(--chart-negative)",
  muted: "var(--chart-6)",
};

/**
 * Server-rendered point-and-interval chart (a forest plot): one row per item, a dot at the estimate and a whisker
 * across its interval, against a zero line. `muted` rows (too little evidence) draw a dashed whisker and a hollow dot,
 * so "not enough data" never looks like a result. `lowLabel` / `highLabel` name what each side of zero means; values
 * carry a +/− sign, so direction never depends on colour.
 */
export function IntervalChart({ data, valueFormat = signedFormat, width = 480, lowLabel, highLabel, ariaLabel = "Interval chart", title, caption, className }: {
  data: IntervalDatum[];
  valueFormat?: (v: number) => string;
  width?: number;
  /** Text under the axis on the negative side of zero (e.g. "Ours closer"). */
  lowLabel?: string;
  /** Text under the axis on the positive side of zero (e.g. "ESPN closer"). */
  highLabel?: string;
  ariaLabel?: string;
  title?: ReactNode;
  caption?: ReactNode;
  className?: string;
}) {
  const pf = phoneFont(width);
  const describe = (d: IntervalDatum) => d.value === null ? `${d.label}: no data`
    : `${d.label}: ${valueFormat(d.value)}${d.lo !== null && d.hi !== null ? ` (95% interval ${valueFormat(d.lo)} to ${valueFormat(d.hi)})` : ""}${d.note ? `, ${d.note}` : ""}`;
  const summary = `${ariaLabel}: ${data.map(describe).join("; ")}`;
  const ext = extent(data.flatMap((d) => [d.value, d.lo, d.hi]));
  if (!ext || data.length === 0) {
    return (
      <ChartFrame width={width} height={120} label={`${ariaLabel}: no data`} title={title} caption={caption} className={className}>
        <text x={width / 2} y={60} textAnchor="middle" className={styles.empty}>No data to chart</text>
      </ChartFrame>
    );
  }
  // The domain always includes zero, symmetric enough that the zero line sits inside the plot.
  const span = Math.max(Math.abs(ext[0]), Math.abs(ext[1]), 0.5);
  const nice = niceTicks(Math.min(0, ext[0], -span * 0.25), Math.max(0, ext[1], span * 0.25), 4);
  const rowH = 30;
  const maxLabel = Math.max(...data.map((d) => d.label.length));
  const labelW = Math.max(36, textWidth(maxLabel, pf) + 8);
  const valueChars = Math.max(...data.map((d) => (d.value === null ? 1 : valueFormat(d.value).length + (d.note ? d.note.length + 1 : 0))));
  const valueW = textWidth(valueChars, pf) + 12;
  const x0 = labelW + 6;
  const x1 = width - valueW;
  const xs = linearScale([nice.min, nice.max], [x0, x1]);
  const zero = xs(0);
  const top = 4;
  const plotH = data.length * rowH;
  const axisY = top + plotH + 4;
  const sideY = axisY + pf + 6;
  const h = sideY + (lowLabel || highLabel ? pf + 8 : 0);
  return (
    <ChartFrame width={width} height={h} label={summary} title={title} caption={caption} className={className}>
      {nice.ticks.map((t) => (
        <g key={t}>
          <line x1={xs(t)} x2={xs(t)} y1={top} y2={top + plotH} className={t === 0 ? styles.axis : styles.grid} />
          <text x={xs(t)} y={axisY + pf - 2} textAnchor="middle" className={styles.t}>{valueFormat(t)}</text>
        </g>
      ))}
      {data.map((d, i) => {
        const cy = top + i * rowH + rowH / 2;
        const tone = d.tone ?? "default";
        const color = TONE_COLOR[tone];
        const muted = tone === "muted";
        return (
          <g key={`${d.label}-${i}`}>
            <title>{describe(d)}</title>
            <text x={0} y={cy} dy="0.34em" className={`${styles.t} ${styles.strong}`}>{d.label}</text>
            {d.value !== null && d.lo !== null && d.hi !== null && (
              <g stroke={color} strokeWidth={2} strokeLinecap="round" strokeDasharray={muted ? "3 3" : undefined}>
                <line x1={xs(d.lo)} x2={xs(d.hi)} y1={cy} y2={cy} />
                <line x1={xs(d.lo)} x2={xs(d.lo)} y1={cy - 5} y2={cy + 5} />
                <line x1={xs(d.hi)} x2={xs(d.hi)} y1={cy - 5} y2={cy + 5} />
              </g>
            )}
            {d.value !== null && (
              <circle cx={xs(d.value)} cy={cy} r={5} fill={muted ? "var(--surface)" : color} stroke={color} strokeWidth={2} />
            )}
            <text x={width - 2} y={cy} dy="0.34em" textAnchor="end" className={`${styles.t} ${muted ? styles.muted : styles.strong}`}>
              {d.value === null ? "—" : valueFormat(d.value)}{d.note ? <tspan className={styles.muted}>{` ${d.note}`}</tspan> : null}
            </text>
          </g>
        );
      })}
      {lowLabel && <text x={zero - 6} y={sideY + pf} textAnchor="end" className={`${styles.t} ${styles.muted}`}>← {lowLabel}</text>}
      {highLabel && <text x={zero + 6} y={sideY + pf} textAnchor="start" className={`${styles.t} ${styles.muted}`}>{highLabel} →</text>}
    </ChartFrame>
  );
}
