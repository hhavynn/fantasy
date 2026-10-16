import type { ReactNode } from "react";
import { ChartFrame } from "./ChartFrame";
import { extent, linearScale, niceTicks } from "./scale";
import { defaultFormat, phoneFont, signedFormat, textWidth, truncate } from "./shared";
import styles from "./charts.module.css";

export type BarTone = "default" | "positive" | "negative" | "caution" | "muted";

export type BarDatum = {
  label: string;
  /** `null` draws no bar and a muted dash: missing data, not zero. */
  value: number | null;
  tone?: BarTone;
};

const TONE_COLOR: Record<BarTone, string> = {
  default: "var(--chart-1)",
  positive: "var(--chart-positive)",
  negative: "var(--chart-negative)",
  caution: "var(--caution)",
  muted: "var(--chart-6)",
};

/**
 * Server-rendered bar chart, vertical or horizontal. Values below zero extend below/left of the
 * zero axis; when any value is negative the chart is "diverging": untoned bars are
 * `--chart-positive` / `--chart-negative` and value labels carry a +/− sign, so direction is
 * never colour alone. Set `tone` on a datum to override.
 */
export function BarChart({ data, orientation = "vertical", valueFormat, height, width = 480, showValues, divider, ariaLabel = "Bar chart", title, caption, className }: {
  data: BarDatum[];
  orientation?: "vertical" | "horizontal";
  /** Formats value labels and tooltips. Defaults to a signed format for diverging data. */
  valueFormat?: (v: number) => string;
  /** Vertical charts only (horizontal height follows the row count). */
  height?: number;
  width?: number;
  /** Print the value at the end of each bar. Default: true for up to 8 vertical bars, always for horizontal. */
  showValues?: boolean;
  /** Horizontal charts only: a dashed line with a label after the first `after` rows (e.g. a cutline). */
  divider?: { after: number; label: string };
  ariaLabel?: string;
  /** Visible heading and source/sample line, drawn by ChartFrame. */
  title?: ReactNode;
  caption?: ReactNode;
  className?: string;
}) {
  const ext = extent(data.map((d) => d.value));
  const diverging = !!ext && ext[0] < 0;
  const fmt = valueFormat ?? (diverging ? signedFormat : defaultFormat);
  const colorOf = (d: BarDatum) => {
    if (d.tone) return TONE_COLOR[d.tone];
    if (diverging && d.value !== null) return d.value < 0 ? TONE_COLOR.negative : TONE_COLOR.positive;
    return TONE_COLOR.default;
  };
  const tip = (d: BarDatum) => `${d.label}: ${d.value === null ? "no data" : fmt(d.value)}`;
  const summary = `${ariaLabel}: ${data.map((d) => `${d.label} ${d.value === null ? "no data" : fmt(d.value)}`).join("; ")}`;
  const pf = phoneFont(width);

  if (!ext || data.length === 0) {
    const h = height ?? 120;
    return (
      <ChartFrame width={width} height={h} label={`${ariaLabel}: no data`} className={className}>
        <text x={width / 2} y={h / 2} textAnchor="middle" className={styles.empty}>No data to chart</text>
      </ChartFrame>
    );
  }

  // Domain always includes zero so bar length is honest.
  const nice = niceTicks(Math.min(0, ext[0]), Math.max(0, ext[1]), 4);

  if (orientation === "horizontal") {
    const rowH = 28;
    const barH = 14;
    const maxLabel = Math.max(...data.map((d) => d.label.length));
    const labelW = Math.min(width * 0.4, Math.max(70, textWidth(maxLabel, pf)));
    const maxChars = Math.floor(labelW / (pf * 0.58));
    const valueW = Math.max(...data.map((d) => (d.value === null ? 1 : fmt(d.value).length))) * pf * 0.58 + 8;
    const x0 = labelW + 8 + (diverging ? valueW : 0);
    const x1 = width - valueW;
    const xs = linearScale([nice.min, nice.max], [x0, x1]);
    const zero = xs(0);
    // A divider reserves its own gap between rows so its label never sits on a bar or a value.
    const hasDivider = !!divider && divider.after >= 0 && divider.after < data.length;
    const gap = hasDivider ? pf + 8 : 0;
    const h = data.length * rowH + 8 + gap;
    return (
      <ChartFrame width={width} height={h} label={summary} title={title} caption={caption} className={className}>
        {data.map((d, i) => {
          const cy = 4 + i * rowH + rowH / 2 + (hasDivider && i >= (divider?.after ?? 0) ? gap : 0);
          const v = d.value;
          const end = v === null ? zero : xs(v);
          return (
            <g key={`${d.label}-${i}`}>
              <title>{tip(d)}</title>
              <text x={0} y={cy} dy="0.34em" className={`${styles.t} ${styles.muted}`}>{truncate(d.label, maxChars)}</text>
              {v !== null && <rect x={Math.min(zero, end)} y={cy - barH / 2} width={Math.max(Math.abs(end - zero), v === 0 ? 0 : 1)} height={barH} rx={2} fill={colorOf(d)} />}
              <text x={v !== null && v < 0 ? end - 6 : end + 6} y={cy} dy="0.34em" textAnchor={v !== null && v < 0 ? "end" : "start"} className={`${styles.t} ${styles.strong}`}>
                {v === null ? "—" : fmt(v)}
              </text>
            </g>
          );
        })}
        <line x1={zero} x2={zero} y1={2} y2={h - 2} className={styles.axis} />
        {hasDivider && divider && (
          <g>
            <line x1={0} x2={width} y1={4 + divider.after * rowH + gap - 2} y2={4 + divider.after * rowH + gap - 2} className={styles.ref} />
            <text x={width - 2} y={4 + divider.after * rowH + gap - 5} textAnchor="end" className={`${styles.t} ${styles.strong} ${styles.halo}`}>{divider.label}</text>
          </g>
        )}
      </ChartFrame>
    );
  }

  // Vertical.
  const h = height ?? 220;
  const labelChars = Math.max(...nice.ticks.map((t) => fmt(t).length));
  const left = Math.ceil(textWidth(labelChars, pf)) + 14;
  const right = 8;
  const top = pf + 6;
  const bottom = pf + 14;
  const floor = h - bottom - (diverging && (showValues ?? data.length <= 8) ? pf + 2 : 0); // room for labels under negative bars
  const slot = (width - left - right) / data.length;
  const barW = Math.min(44, slot * 0.62);
  const ys = linearScale([nice.min, nice.max], [floor, top]);
  const zeroY = ys(0);
  const maxChars = Math.max(2, Math.floor((slot - 4) / (pf * 0.58)));
  const values = showValues ?? data.length <= 8;
  return (
    <ChartFrame width={width} height={h} label={summary} title={title} caption={caption} className={className}>
      {nice.ticks.map((t) => (
        <g key={t}>
          <line x1={left} x2={width - right} y1={ys(t)} y2={ys(t)} className={t === 0 ? styles.axis : styles.grid} />
          <text x={left - 6} y={ys(t)} dy="0.34em" textAnchor="end" className={styles.t}>{fmt(t)}</text>
        </g>
      ))}
      {data.map((d, i) => {
        const cx = left + slot * i + slot / 2;
        const v = d.value;
        const y = v === null ? zeroY : ys(v);
        return (
          <g key={`${d.label}-${i}`}>
            <title>{tip(d)}</title>
            {v !== null && <rect x={cx - barW / 2} y={Math.min(y, zeroY)} width={barW} height={Math.max(Math.abs(y - zeroY), v === 0 ? 0 : 1)} rx={2} fill={colorOf(d)} />}
            {(values || v === null) && (
              <text x={cx} y={v !== null && v < 0 ? y + pf + 2 : (v === null ? zeroY - 5 : y - 5)} textAnchor="middle" className={`${styles.t} ${styles.strong}`}>
                {v === null ? "—" : fmt(v)}
              </text>
            )}
            <text x={cx} y={h - bottom + pf + 4} textAnchor="middle" className={`${styles.t} ${styles.muted}`}>{truncate(d.label, maxChars)}</text>
          </g>
        );
      })}
    </ChartFrame>
  );
}
