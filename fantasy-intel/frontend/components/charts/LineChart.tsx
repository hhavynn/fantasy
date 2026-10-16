import { useId, type ReactNode } from "react";
import { ChartFrame, Legend } from "./ChartFrame";
import { extent, linearScale, niceTicks, ticksWithin } from "./scale";
import { BASE_FONT, defaultFormat, phoneFont, seriesColor, textWidth, thinStep } from "./shared";
import styles from "./charts.module.css";

export type LineSeries = {
  id: string;
  label: string;
  /** Any CSS colour; defaults to the next `--chart-n` token. */
  color?: string;
  /** One value per x label. `null` is a gap in the line (missing), never zero. */
  points: (number | null)[];
};

/**
 * Server-rendered multi-series line chart. `null` points break the line; a point with no
 * neighbours is drawn as a dot. A legend appears when there is more than one series, every point
 * has a hover tooltip, and the SVG carries a screen-reader summary.
 *
 * `yDomain` pins the axis (values outside it are clipped); otherwise the axis spans the data and
 * `refLine` with round-number ticks. `width` is the natural chart width in SVG units (text stays
 * readable at roughly that size); the figure scales down to its container and up to 1.3x.
 */
export function LineChart({ series, xLabels, yFormat = defaultFormat, height = 220, width = 480, yDomain, markers = false, refLine, ariaLabel = "Line chart", title, caption, className }: {
  series: LineSeries[];
  xLabels: string[];
  yFormat?: (v: number) => string;
  height?: number;
  width?: number;
  yDomain?: readonly [number, number];
  markers?: boolean;
  refLine?: { y: number; label: string };
  ariaLabel?: string;
  /** Visible heading and source/sample line, drawn by ChartFrame. */
  title?: ReactNode;
  caption?: ReactNode;
  className?: string;
}) {
  const clipId = useId();
  const n = xLabels.length;
  const ext = extent([...series.flatMap((s) => s.points.slice(0, n)), refLine?.y]);

  if (!ext || n === 0) {
    return (
      <ChartFrame width={width} height={height} label={`${ariaLabel}: no data`} className={className}>
        <text x={width / 2} y={height / 2} textAnchor="middle" className={styles.empty}>No data to chart</text>
      </ChartFrame>
    );
  }

  const [lo, hi] = yDomain ?? (() => { const t = niceTicks(ext[0], ext[1], 4); return [t.min, t.max] as const; })();
  const ticks = yDomain ? ticksWithin(lo, hi, 4) : niceTicks(ext[0], ext[1], 4).ticks;
  const pf = phoneFont(width);

  const labelChars = Math.max(...ticks.map((t) => yFormat(t).length));
  const left = Math.ceil(textWidth(labelChars, pf)) + 14;
  const right = 14;
  const top = 12;
  const bottom = pf + 12;
  const x0 = left + 6;
  const x1 = width - right - 6;
  const xs = (i: number) => (n === 1 ? (x0 + x1) / 2 : x0 + (i * (x1 - x0)) / (n - 1));
  const ys = linearScale([lo, hi], [height - bottom, top]);

  const maxLabel = Math.max(...xLabels.map((l) => l.length));
  const stepDesktop = thinStep(n, x1 - x0, Math.max(26, textWidth(maxLabel, BASE_FONT) + 12));
  const stepPhone = thinStep(n, x1 - x0, Math.max(30, textWidth(maxLabel, pf) + 12));
  const xLabelSet = (step: number, cls?: string) =>
    xLabels.map((l, i) => (i % step === 0 ? (
      <text key={`${cls}-${i}`} x={xs(i)} y={height - bottom + pf + 4} textAnchor="middle" className={`${styles.t} ${cls ?? ""}`}>{l}</text>
    ) : null));

  const summary = `${ariaLabel}: ${series.map((s) => s.label).join(", ")}; ${xLabels[0]} to ${xLabels[n - 1]}; values from ${yFormat(ext[0])} to ${yFormat(ext[1])}`;

  return (
    <ChartFrame
      width={width}
      height={height}
      label={summary}
      title={title}
      caption={caption}
      className={className}
      legend={series.length > 1 ? <Legend items={series.map((s, i) => ({ id: s.id, label: s.label, color: seriesColor(i, s.color) }))} /> : undefined}
    >
      <defs>
        <clipPath id={clipId}><rect x={left} y={top - 4} width={width - left - right + 4} height={height - bottom - top + 8} /></clipPath>
      </defs>
      {ticks.map((t) => (
        <g key={t}>
          <line x1={left} x2={width - right} y1={ys(t)} y2={ys(t)} className={styles.grid} />
          <text x={left - 6} y={ys(t)} dy="0.34em" textAnchor="end" className={styles.t}>{yFormat(t)}</text>
        </g>
      ))}
      {stepDesktop === stepPhone ? xLabelSet(stepDesktop) : (
        <>
          {xLabelSet(stepDesktop, styles.desktopOnly)}
          {xLabelSet(stepPhone, styles.phoneOnly)}
        </>
      )}
      {refLine && refLine.y >= lo && refLine.y <= hi && (
        <g>
          <line x1={left} x2={width - right} y1={ys(refLine.y)} y2={ys(refLine.y)} className={styles.ref} />
          <text x={width - right} y={ys(refLine.y) - 4} textAnchor="end" className={`${styles.t} ${styles.muted} ${styles.halo}`}>{refLine.label}</text>
        </g>
      )}
      <g clipPath={`url(#${clipId})`}>
        {series.map((s, si) => {
          const color = seriesColor(si, s.color);
          const segments: { i: number; v: number }[][] = [];
          let current: { i: number; v: number }[] = [];
          for (let i = 0; i < n; i++) {
            const v = s.points[i];
            if (v === null || v === undefined || !Number.isFinite(v)) {
              if (current.length) segments.push(current);
              current = [];
            } else {
              current.push({ i, v });
            }
          }
          if (current.length) segments.push(current);
          return (
            <g key={s.id} className={styles.series}>
              {segments.filter((seg) => seg.length > 1).map((seg, k) => (
                <path key={k} className={styles.line} stroke={color}
                      d={seg.map((p, j) => `${j ? "L" : "M"}${xs(p.i).toFixed(1)},${ys(p.v).toFixed(1)}`).join("")} />
              ))}
              {/* A point with no neighbours is not a line: show it as a dot even without markers. */}
              {!markers && segments.filter((seg) => seg.length === 1).map((seg) => (
                <circle key={`solo-${seg[0].i}`} cx={xs(seg[0].i)} cy={ys(seg[0].v)} r={3} fill={color} className={styles.dot} />
              ))}
              {segments.flat().map((p) => (
                <circle key={p.i} cx={xs(p.i)} cy={ys(p.v)} className={markers ? styles.dot : styles.hit}
                        r={markers ? 3.5 : 7}
                        fill={markers ? color : "transparent"}>
                  <title>{`${s.label}, ${xLabels[p.i]}: ${yFormat(p.v)}`}</title>
                </circle>
              ))}
            </g>
          );
        })}
      </g>
    </ChartFrame>
  );
}
