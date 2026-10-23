"use client";

import Link from "@/components/LeagueLink";
import { useLeagueHref } from "@/components/useAppPathname";
import { useRouter } from "next/navigation";
import { useEffect, useRef, useState, type KeyboardEvent, type PointerEvent } from "react";
import { formatMetric, type Format } from "@/lib/research";
import { chartHeight, nearestPoint, tooltipPosition } from "./interactive";
import { placeLabels } from "./labels";
import { linearScale, ticksWithin } from "./scale";
import styles from "./InteractiveScatter.module.css";

export type InteractivePoint = {
  id: string;
  x: number;
  y: number;
  /** Marker radius in pixels (bubble charts); default 6. */
  r?: number;
  /** Full name for the tooltip and the accessible label. */
  name: string;
  /** Short text printed beside the marker. */
  short: string;
  /** Second line: team, position, games. */
  meta: string;
  color: string;
  /** Hollow marker: a point whose group or confidence is not established. */
  hollow?: boolean;
  /** Dashed outline: low sample confidence. */
  dashed?: boolean;
  emphasis?: "selected" | "compare" | "match" | null;
  /** Printed by default (the chart's key players); every point is labeled under "All labels". */
  keyLabel?: boolean;
  /** Tooltip rows: [label, value]. */
  rows: [string, string][];
  /** Where selecting the point goes (URL-backed selection). */
  href: string;
};

export type RefLine = { value: number; label: string };

type LabelMode = "key" | "all" | "none";

const MARGIN = { top: 16, right: 16, bottom: 44 };
const FONT = 11;
const TIP_WIDTH = 232;

/**
 * Client scatter/bubble chart with hover, focus and tap tooltips and URL-backed selection. It measures
 * its container so markers and text keep their real pixel size on phones. Each marker is a focusable
 * button (Enter selects); the nearest marker answers the pointer, so small markers stay easy to hit.
 * `diagonal` draws y = x and is only for charts whose axes share a unit.
 */
export function InteractiveScatter({ points, xLabel, yLabel, xFormat, yFormat, xDomain, yDomain, refX = [], refY = [], diagonal, corners, ariaLabel, emptyText = "No players match these filters" }: {
  points: InteractivePoint[];
  xLabel: string;
  yLabel: string;
  xFormat: Format;
  yFormat: Format;
  xDomain: [number, number];
  yDomain: [number, number];
  refX?: RefLine[];
  refY?: RefLine[];
  diagonal?: string;
  corners?: { tl?: string; tr?: string; bl?: string; br?: string };
  ariaLabel: string;
  emptyText?: string;
}) {
  const router = useRouter();
  // A point's href is built on the server without the league; on a league page the navigation stays in it.
  const leagueHref = useLeagueHref();
  const wrap = useRef<HTMLDivElement>(null);
  const [width, setWidth] = useState(760);
  const [hover, setHover] = useState<string | null>(null);
  const [pinned, setPinned] = useState<string | null>(null);
  const [labels, setLabels] = useState<LabelMode>("key");

  useEffect(() => {
    const el = wrap.current;
    if (!el) return;
    const ro = new ResizeObserver(([e]) => setWidth(Math.max(280, Math.round(e.contentRect.width))));
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  const height = chartHeight(width);
  const fmtX = (v: number) => formatMetric(v, xFormat) ?? "";
  const fmtY = (v: number) => formatMetric(v, yFormat) ?? "";
  const xTicks = ticksWithin(xDomain[0], xDomain[1], width < 520 ? 4 : 6);
  const yTicks = ticksWithin(yDomain[0], yDomain[1], 5);
  const left = Math.ceil(Math.max(...yTicks.map((t) => fmtY(t).length)) * FONT * 0.6) + 14;
  const xs = linearScale(xDomain, [left, width - MARGIN.right]);
  const ys = linearScale(yDomain, [height - MARGIN.bottom, MARGIN.top]);
  const plot = { x0: left, x1: width - MARGIN.right, y0: MARGIN.top, y1: height - MARGIN.bottom };
  const clampX = (v: number) => Math.max(plot.x0, Math.min(plot.x1, xs(v)));
  const clampY = (v: number) => Math.max(plot.y0, Math.min(plot.y1, ys(v)));

  const placed = points.map((p) => ({ ...p, px: clampX(p.x), py: clampY(p.y), r: p.r ?? 6 }));
  const tabOrder = [...placed].sort((a, b) => a.px - b.px || a.py - b.py);
  const drawOrder = [...placed].sort((a, b) => rank(a.emphasis) - rank(b.emphasis) || b.r - a.r);
  const chosen = placed.filter((p) => p.emphasis || labels === "all" || (labels === "key" && p.keyLabel));
  const placedLabels = placeLabels(
    chosen.map((p) => ({ id: p.id, text: p.short, px: p.px, py: p.py, priority: !!p.emphasis })),
    { width, height: plot.y1 + 4 },
    { charWidth: FONT * 0.58, textHeight: FONT, markerRadius: 7, obstacles: placed.map((p) => ({ id: p.id, px: p.px, py: p.py })) },
  );

  const activeId = pinned ?? hover;
  const active = placed.find((p) => p.id === activeId) ?? null;

  if (points.length === 0) {
    return <div ref={wrap} className={styles.empty}>{emptyText}</div>;
  }

  const local = (e: PointerEvent<SVGSVGElement>) => {
    const box = e.currentTarget.getBoundingClientRect();
    return { x: ((e.clientX - box.left) / box.width) * width, y: ((e.clientY - box.top) / box.height) * height };
  };
  const near = (e: PointerEvent<SVGSVGElement>, maxDist: number) => {
    const { x, y } = local(e);
    const i = nearestPoint(placed, x, y, maxDist);
    return i === null ? null : placed[i];
  };
  const onMove = (e: PointerEvent<SVGSVGElement>) => {
    if (e.pointerType !== "mouse") return;
    setHover(near(e, 18)?.id ?? null);
  };
  const onUp = (e: PointerEvent<SVGSVGElement>) => {
    const p = near(e, e.pointerType === "mouse" ? 18 : 26);
    if (!p) {
      setPinned(null);
      return;
    }
    if (e.pointerType === "mouse") router.push(leagueHref(p.href), { scroll: false });
    else setPinned((cur) => (cur === p.id ? null : p.id));   // touch: first tap shows the card, which links to the evidence
  };
  const onKey = (p: (typeof placed)[number]) => (e: KeyboardEvent<SVGCircleElement>) => {
    if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      router.push(leagueHref(p.href), { scroll: false });
    } else if (e.key === "Escape") {
      setPinned(null);
      setHover(null);
    }
  };

  // Size estimate from the content (fixed row height and width in CSS), so placement needs no measuring pass.
  const tipW = Math.min(TIP_WIDTH, width - 8);
  const tipH = active ? 48 + active.rows.length * 19 + (pinned === active.id ? 30 : 0) : 0;
  const pos = active ? tooltipPosition(active.px, active.py, tipW, tipH, width, height) : null;
  const summary = `${ariaLabel}. ${points.length} players. ${xLabel} on the horizontal axis, ${yLabel} on the vertical axis. Tab to a marker to read it; Enter selects. The table below lists every player.`;
  // Corner annotations need room; on phones the legend and caption carry the same reading.
  const showCorners = width >= 560;
  const diagLo = Math.max(xDomain[0], yDomain[0]);
  const diagHi = Math.min(xDomain[1], yDomain[1]);

  return (
    <div className={styles.root}>
      <div className={styles.toolbar} role="group" aria-label="Chart labels">
        <span className={styles.toolbarLabel}>Labels</span>
        {(["key", "all", "none"] as const).map((m) => (
          <button key={m} type="button" className={styles.toggle} aria-pressed={labels === m} onClick={() => setLabels(m)}>
            {m === "key" ? "Key players" : m === "all" ? "All" : "Selected only"}
          </button>
        ))}
      </div>
      <div ref={wrap} className={styles.wrap} onMouseLeave={() => setHover(null)}>
        <svg width={width} height={height} viewBox={`0 0 ${width} ${height}`} className={styles.svg} role="group" aria-label={summary}
             onPointerMove={onMove} onPointerUp={onUp}>
          {yTicks.map((t) => (
            <g key={`y${t}`} aria-hidden="true">
              <line x1={plot.x0} x2={plot.x1} y1={ys(t)} y2={ys(t)} className={styles.grid} />
              <text x={plot.x0 - 6} y={ys(t)} dy="0.34em" textAnchor="end" className={styles.tick}>{fmtY(t)}</text>
            </g>
          ))}
          {xTicks.map((t) => (
            <g key={`x${t}`} aria-hidden="true">
              <line x1={xs(t)} x2={xs(t)} y1={plot.y0} y2={plot.y1} className={styles.grid} />
              <text x={xs(t)} y={plot.y1 + 16} textAnchor="middle" className={styles.tick}>{fmtX(t)}</text>
            </g>
          ))}
          <g aria-hidden="true">
            <line x1={plot.x0} x2={plot.x1} y1={plot.y1} y2={plot.y1} className={styles.axis} />
            <line x1={plot.x0} x2={plot.x0} y1={plot.y0} y2={plot.y1} className={styles.axis} />
            <text x={plot.x1} y={height - 6} textAnchor="end" className={styles.axisLabel}>{xLabel} →</text>
            <text x={plot.x0 + 6} y={plot.y0 + 10} className={styles.axisLabel}>↑ {yLabel}</text>
            {diagonal && diagHi > diagLo && (
              <>
                <line x1={xs(diagLo)} y1={ys(diagLo)} x2={xs(diagHi)} y2={ys(diagHi)} className={styles.ref} />
                <text x={xs(diagHi) - 4} y={ys(diagHi) + 14} textAnchor="end" className={styles.refLabel}>{diagonal}</text>
              </>
            )}
            {refX.filter((r) => r.value > xDomain[0] && r.value < xDomain[1]).map((r) => (
              <g key={`rx${r.value}`}>
                <line x1={xs(r.value)} x2={xs(r.value)} y1={plot.y0} y2={plot.y1} className={styles.ref} />
                <text x={xs(r.value) + 4} y={plot.y1 - 6} className={styles.refLabel}>{r.label}</text>
              </g>
            ))}
            {refY.filter((r) => r.value >= yDomain[0] && r.value <= yDomain[1]).map((r) => (
              <g key={`ry${r.value}`}>
                <line x1={plot.x0} x2={plot.x1} y1={ys(r.value)} y2={ys(r.value)} className={styles.refStrong} />
                <text x={plot.x1 - 4} y={ys(r.value) - 5} textAnchor="end" className={styles.refLabel}>{r.label}</text>
              </g>
            ))}
            {showCorners && corners?.tl && <text x={plot.x0 + 8} y={plot.y0 + 28} className={styles.corner}>{corners.tl}</text>}
            {showCorners && corners?.tr && <text x={plot.x1 - 8} y={plot.y0 + 28} textAnchor="end" className={styles.corner}>{corners.tr}</text>}
            {showCorners && corners?.bl && <text x={plot.x0 + 8} y={plot.y1 - 22} className={styles.corner}>{corners.bl}</text>}
            {showCorners && corners?.br && <text x={plot.x1 - 8} y={plot.y1 - 22} textAnchor="end" className={styles.corner}>{corners.br}</text>}
          </g>
          <g>
            {drawOrder.map((p) => (
              <g key={p.id} aria-hidden="true" className={styles.markerGroup} data-active={p.id === activeId || undefined}>
                {p.emphasis && <circle cx={p.px} cy={p.py} r={p.r + 4} className={p.emphasis === "compare" ? styles.ringCompare : p.emphasis === "match" ? styles.ringMatch : styles.ringSelected} />}
                <circle cx={p.px} cy={p.py} r={p.r} className={styles.marker} fill={p.hollow ? "var(--surface)" : p.color}
                        stroke={p.hollow ? p.color : undefined} strokeDasharray={p.dashed ? "2 2" : undefined}
                        fillOpacity={p.hollow ? 1 : p.emphasis ? 0.95 : 0.78} />
              </g>
            ))}
          </g>
          <g aria-hidden="true">
            {placedLabels.map((l) => {
              const p = placed.find((q) => q.id === l.id);
              return (
                <text key={`l${l.id}`} x={l.x} y={l.y} textAnchor={l.anchor} className={`${styles.label} ${p?.emphasis ? styles.labelStrong : ""}`}>
                  {l.text}
                </text>
              );
            })}
          </g>
          <g>
            {tabOrder.map((p) => (
              <circle key={`f${p.id}`} cx={p.px} cy={p.py} r={Math.max(p.r, 8)} className={styles.focus} tabIndex={0} role="button"
                      aria-label={`${p.name}, ${p.meta}. ${p.rows.map(([k, v]) => `${k} ${v}`).join(", ")}${p.emphasis === "selected" ? ". Selected" : ""}`}
                      onFocus={() => setHover(p.id)} onBlur={() => setHover((h) => (h === p.id ? null : h))} onKeyDown={onKey(p)} />
            ))}
          </g>
        </svg>
        {active && pos && (
          <div className={styles.tooltip} style={{ left: pos.left, top: pos.top, width: tipW }} aria-hidden={pinned ? undefined : true}>
            <p className={styles.tipName}>{active.name}</p>
            <p className={styles.tipMeta}>{active.meta}</p>
            <dl className={styles.tipRows}>
              {active.rows.map(([k, v]) => (
                <div key={k}><dt>{k}</dt><dd>{v}</dd></div>
              ))}
            </dl>
            {pinned === active.id && <Link className={styles.tipLink} href={`${active.href}#evidence`} scroll={false}>View evidence</Link>}
          </div>
        )}
      </div>
    </div>
  );
}

function rank(e: InteractivePoint["emphasis"]): number {
  return e === "selected" ? 3 : e === "compare" ? 2 : e === "match" ? 1 : 0;
}
