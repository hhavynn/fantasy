import { ChartFrame } from "./ChartFrame";
import { placeLabels, type PlacedLabel } from "./labels";
import { extent, linearScale, niceTicks, ticksWithin } from "./scale";
import { BASE_FONT, defaultFormat, phoneFont, textWidth } from "./shared";
import styles from "./charts.module.css";

export type ScatterPoint = {
  id: string | number;
  x: number;
  y: number;
  /** Text printed beside the point. Points without one are unlabeled (they still have a tooltip). */
  label?: string;
  /** Emphasised point (e.g. your team): larger, accent coloured, labelled first. */
  highlight?: boolean;
  /** Tooltip text; defaults to the label with both values. */
  title?: string;
};

/**
 * Server-rendered scatter plot with collision-avoiding point labels. With `diagonal`, both axes
 * share one domain and a dashed y = x line is drawn (above the line means y exceeds x).
 * Labels that cannot be placed without overlap are dropped, never stacked; every point keeps a tooltip.
 */
export function ScatterChart({ points, xLabel, yLabel, xFormat = defaultFormat, yFormat = defaultFormat, diagonal = false, xDomain, yDomain, height = 340, width = 480, ariaLabel = "Scatter plot", className }: {
  points: ScatterPoint[];
  xLabel: string;
  yLabel: string;
  xFormat?: (v: number) => string;
  yFormat?: (v: number) => string;
  diagonal?: boolean;
  xDomain?: readonly [number, number];
  yDomain?: readonly [number, number];
  height?: number;
  width?: number;
  ariaLabel?: string;
  className?: string;
}) {
  const xe = extent(points.map((p) => p.x));
  const ye = extent(points.map((p) => p.y));
  if (!xe || !ye) {
    return (
      <ChartFrame width={width} height={height} label={`${ariaLabel}: no data`} className={className}>
        <text x={width / 2} y={height / 2} textAnchor="middle" className={styles.empty}>No data to chart</text>
      </ChartFrame>
    );
  }

  const shared = diagonal ? ([Math.min(xe[0], ye[0]), Math.max(xe[1], ye[1])] as const) : null;
  const axis = (dom: readonly [number, number] | undefined, e: [number, number]) => {
    if (dom) return { lo: dom[0], hi: dom[1], ticks: ticksWithin(dom[0], dom[1], 4) };
    const t = niceTicks(shared ? shared[0] : e[0], shared ? shared[1] : e[1], 4);
    return { lo: t.min, hi: t.max, ticks: t.ticks };
  };
  const ax = axis(xDomain, xe);
  const ay = axis(yDomain, ye);

  const pf = phoneFont(width);
  const left = Math.ceil(textWidth(Math.max(...ay.ticks.map((t) => yFormat(t).length)), pf)) + 14;
  const right = 18;
  const top = pf + 12;
  const bottom = pf * 2 + 16;
  const xs = linearScale([ax.lo, ax.hi], [left, width - right]);
  const ys = linearScale([ay.lo, ay.hi], [height - bottom, top]);

  const labeled = points.filter((p) => p.label);
  const placeFor = (font: number): PlacedLabel[] =>
    placeLabels(
      labeled.map((p) => ({ id: p.id, text: p.label as string, px: xs(p.x), py: ys(p.y), priority: p.highlight })),
      { width, height: height - bottom + 4 },
      { charWidth: font * 0.58, textHeight: font, markerRadius: 6, obstacles: points.map((p) => ({ id: p.id, px: xs(p.x), py: ys(p.y) })) },
    );
  const desktop = placeFor(BASE_FONT);
  const phone = placeFor(pf);
  const same = desktop.length === phone.length && desktop.every((l, i) => l.id === phone[i].id && l.x === phone[i].x && l.y === phone[i].y && l.anchor === phone[i].anchor);
  const labelNodes = (set: PlacedLabel[], cls?: string) =>
    set.map((l) => {
      const hl = points.find((p) => p.id === l.id)?.highlight;
      return (
        <text key={`${cls}-${l.id}`} x={l.x} y={l.y} textAnchor={l.anchor} className={`${styles.t} ${styles.halo} ${hl ? styles.strong : styles.muted} ${cls ?? ""}`}>
          {l.truncated ? <title>{points.find((p) => p.id === l.id)?.label}</title> : null}
          {l.text}
        </text>
      );
    });

  const summary = `${ariaLabel}: ${points.length} points; ${xLabel} from ${xFormat(xe[0])} to ${xFormat(xe[1])}, ${yLabel} from ${yFormat(ye[0])} to ${yFormat(ye[1])}`;

  return (
    <ChartFrame width={width} height={height} label={summary} className={className}>
      {ay.ticks.map((t) => (
        <g key={`y${t}`}>
          <line x1={left} x2={width - right} y1={ys(t)} y2={ys(t)} className={styles.grid} />
          <text x={left - 6} y={ys(t)} dy="0.34em" textAnchor="end" className={styles.t}>{yFormat(t)}</text>
        </g>
      ))}
      {ax.ticks.map((t) => (
        <g key={`x${t}`}>
          <line x1={xs(t)} x2={xs(t)} y1={top} y2={height - bottom} className={styles.grid} />
          <text x={xs(t)} y={height - bottom + pf + 4} textAnchor="middle" className={styles.t}>{xFormat(t)}</text>
        </g>
      ))}
      <line x1={left} x2={width - right} y1={height - bottom} y2={height - bottom} className={styles.axis} />
      <line x1={left} x2={left} y1={top} y2={height - bottom} className={styles.axis} />
      <text x={width - right} y={height - 6} textAnchor="end" className={`${styles.t} ${styles.muted}`}>{xLabel} →</text>
      <text x={left} y={pf + 2} className={`${styles.t} ${styles.muted}`}>↑ {yLabel}</text>
      {diagonal && shared && (
        <line x1={xs(Math.max(ax.lo, ay.lo))} y1={ys(Math.max(ax.lo, ay.lo))} x2={xs(Math.min(ax.hi, ay.hi))} y2={ys(Math.min(ax.hi, ay.hi))} className={styles.ref} />
      )}
      {points.map((p) => (
        <circle key={p.id} cx={xs(p.x)} cy={ys(p.y)} r={p.highlight ? 6 : 4.5} className={styles.dot}
                fill={p.highlight ? "var(--accent)" : "var(--ink-muted)"}>
          <title>{p.title ?? `${p.label ? `${p.label}: ` : ""}${xLabel} ${xFormat(p.x)}, ${yLabel} ${yFormat(p.y)}`}</title>
        </circle>
      ))}
      {same ? labelNodes(desktop) : (
        <>
          {labelNodes(desktop, styles.desktopOnly)}
          {labelNodes(phone, styles.phoneOnly)}
        </>
      )}
    </ChartFrame>
  );
}
