import { InteractiveScatter, type InteractivePoint } from "@/components/charts";
import { bubbleRadius } from "@/components/charts/interactive";
import {
  GROUP_LEGEND, OPPORTUNITY_AXES, WORKLOAD_MAPS, formatMetric, groupColor, keyIds, metricRows, metricValue,
  researchHref, shortName, symmetricDomain, zeroDomain, type ResearchState, type Slug,
} from "@/lib/research";
import type { OpportunityResponse, ResearchPlayer } from "@/lib/research-types";
import styles from "./research.module.css";

type Props = { slug: Slug; r: OpportunityResponse; state: ResearchState };

function emphasis(p: ResearchPlayer, s: ResearchState): InteractivePoint["emphasis"] {
  return p.gsis_id === s.sel ? "selected" : p.gsis_id === s.cmp ? "compare" : null;
}

function meta(p: ResearchPlayer): string {
  return `${p.team ?? "No team"} · ${p.position} · ${p.window.games} game${p.window.games === 1 ? "" : "s"}`;
}

function Legend({ items }: { items: { color: string; label: string; hollow?: boolean; dashed?: boolean }[] }) {
  return (
    <ul className={styles.legend}>
      {items.map((i) => (
        <li key={i.label}>
          <svg width="14" height="14" viewBox="0 0 14 14" aria-hidden="true">
            <circle cx="7" cy="7" r="5" fill={i.hollow ? "var(--surface)" : i.color} stroke={i.color} strokeWidth="1.5" strokeDasharray={i.dashed ? "2 2" : undefined} />
          </svg>
          {i.label}
        </li>
      ))}
      <li><span className={styles.ringKey} aria-hidden="true" />Selected</li>
      <li><span className={`${styles.ringKey} ${styles.ringKeyCompare}`} aria-hidden="true" />Compared</li>
    </ul>
  );
}

/** Centerpiece: expected points per game (role) against production over the same games. */
export function OpportunityChart({ slug, r, state }: Props) {
  const ppr = state.y === "ppr";
  const yAxis = ppr ? OPPORTUNITY_AXES.ppr : OPPORTUNITY_AXES.fpoe;
  const players = r.players;
  const high = r.high_opportunity_xfp;
  const keys = keyIds(players, [
    { value: (p) => p.window.xfp_pg, n: 5, dir: "desc" },
    { value: (p) => p.window.fpoe_pg, n: 3, dir: "asc", where: (p) => (p.window.xfp_pg ?? 0) >= high },
    { value: (p) => p.window.fpoe_pg, n: 3, dir: "desc" },
  ]);
  const points: InteractivePoint[] = players.map((p) => ({
    id: p.gsis_id,
    x: p.window.xfp_pg ?? 0,
    y: (ppr ? p.window.ppr_pg : p.window.fpoe_pg) ?? 0,
    name: p.name,
    short: shortName(p.name),
    meta: meta(p),
    color: groupColor(p.group.production),
    hollow: p.group.key === "insufficient",
    emphasis: emphasis(p, state),
    keyLabel: keys.has(p.gsis_id),
    rows: [...metricRows(p.window, [OPPORTUNITY_AXES.x, OPPORTUNITY_AXES.ppr, OPPORTUNITY_AXES.fpoe]), ["Group", p.group.label]],
    href: researchHref(slug, state, { sel: p.gsis_id }),
  }));
  const xs = players.map((p) => p.window.xfp_pg ?? 0);
  const ys = players.map((p) => (ppr ? p.window.ppr_pg : p.window.fpoe_pg) ?? 0);
  const top = zeroDomain([...xs, ...(ppr ? ys : [])], 4, high + 2);
  return (
    <figure className={styles.figure}>
      <InteractiveScatter
        points={points}
        xLabel="xFP per game"
        yLabel={yAxis.label}
        xFormat="num1"
        yFormat={ppr ? "num1" : "signed"}
        xDomain={top}
        yDomain={ppr ? top : symmetricDomain(ys, 4)}
        refX={[{ value: high, label: `High opportunity ${high} xFP/G` }]}
        refY={ppr ? [] : [{ value: 0, label: "Production = expected" }]}
        diagonal={ppr ? "PPR = xFP" : undefined}
        corners={ppr ? undefined : { tr: "Big role, beating it", br: "Big role, lagging", tl: "Efficient, small role", bl: "Small role, lagging" }}
        ariaLabel={`${r.filters.position} expected fantasy points per game against ${yAxis.label.toLowerCase()}`}
      />
      <Legend items={GROUP_LEGEND.map((g) => ({ color: groupColor(g.production), label: g.label, hollow: g.production === undefined }))} />
      <figcaption className={styles.caption}>
        {ppr
          ? <>Above the dashed line a player scored more PPR points than the role was expected to produce; below it, fewer. Both axes are points per game over the same games.</>
          : <>Right of the dashed line is a high-opportunity role ({high}+ xFP/G, the existing high tier). Above zero the player beat the points the role usually produces. Groups are descriptive, not buy or sell calls; &ldquo;in line&rdquo; means within max({r.value_gap_margin.abs}, {Math.round(r.value_gap_margin.rel * 100)}% of xFP) per game.</>}
      </figcaption>
    </figure>
  );
}

/** Position-specific workload map: the opportunity components behind xFP. Bubble area = xFP/G. */
export function WorkloadChart({ slug, r, state }: Props) {
  const map = WORKLOAD_MAPS[r.filters.position];
  const usable = r.players.filter((p) => metricValue(p.window, map.x.key) !== null && metricValue(p.window, map.y.key) !== null);
  const omitted = r.players.length - usable.length;
  const maxXfp = Math.max(1, ...usable.map((p) => p.window.xfp_pg ?? 0));
  const keys = keyIds(usable, [
    { value: (p) => p.window.xfp_pg, n: 5, dir: "desc" },
    { value: (p) => metricValue(p.window, map.y.key), n: 3, dir: "desc" },
    { value: (p) => metricValue(p.window, map.x.key), n: 2, dir: "desc" },
  ]);
  const points: InteractivePoint[] = usable.map((p) => ({
    id: p.gsis_id,
    x: metricValue(p.window, map.x.key) as number,
    y: metricValue(p.window, map.y.key) as number,
    r: bubbleRadius(p.window.xfp_pg, maxXfp, 4, 15),
    name: p.name,
    short: shortName(p.name),
    meta: meta(p),
    color: groupColor(p.group.production),
    hollow: p.group.key === "insufficient",
    emphasis: emphasis(p, state),
    keyLabel: keys.has(p.gsis_id),
    rows: [...metricRows(p.window, [map.x, map.y, OPPORTUNITY_AXES.x]), ...metricRows(p.window, map.tooltip)]
      .filter((row, i, all) => all.findIndex((o) => o[0] === row[0]) === i),
    href: researchHref(slug, state, { sel: p.gsis_id }),
  }));
  const dom = (key: typeof map.x.key, fmt: string) => zeroDomain(usable.map((p) => metricValue(p.window, key) ?? 0), fmt === "pct" ? 0.05 : 2);
  return (
    <figure className={styles.figure}>
      <InteractiveScatter
        points={points}
        xLabel={map.x.label}
        yLabel={map.y.label}
        xFormat={map.x.format === "pct" ? "pct" : "num1"}
        yFormat={map.y.format === "pct" ? "pct" : "num1"}
        xDomain={dom(map.x.key, map.x.format)}
        yDomain={dom(map.y.key, map.y.format)}
        ariaLabel={`${map.title}: ${map.x.label.toLowerCase()} against ${map.y.label.toLowerCase()}, bubble size expected points per game`}
      />
      <Legend items={GROUP_LEGEND.map((g) => ({ color: groupColor(g.production), label: g.label, hollow: g.production === undefined }))} />
      <figcaption className={styles.caption}>
        Bubble area is xFP per game; colour is production against it. {map.note}
        {omitted > 0 && <> {omitted} player{omitted === 1 ? " is" : "s are"} not drawn because a source did not cover the metric (see the table).</>}
      </figcaption>
    </figure>
  );
}

const READING_COLOR: Record<string, string> = {
  "Workload rising faster than production": "var(--chart-1)",
  "Workload and production rising together": "var(--chart-3)",
  "Production rising without matching workload growth": "var(--chart-2)",
};

/** Role momentum: change in expected points against change in actual points, recent vs earlier games. */
export function MomentumChart({ slug, r, state }: Props) {
  const usable = r.players.filter((p) => p.momentum.confidence.level !== "INSUFFICIENT" && p.momentum.xfp_delta !== null && p.momentum.ppr_delta !== null);
  const skipped = r.players.length - usable.length;
  const keys = keyIds(usable, [
    { value: (p) => p.momentum.xfp_delta, n: 5, dir: "desc" },
    { value: (p) => p.momentum.ppr_delta, n: 2, dir: "desc" },
    { value: (p) => p.momentum.xfp_delta, n: 2, dir: "asc" },
  ]);
  const points: InteractivePoint[] = usable.map((p) => {
    const m = p.momentum;
    return {
      id: p.gsis_id,
      x: m.xfp_delta as number,
      y: m.ppr_delta as number,
      name: p.name,
      short: shortName(p.name),
      meta: `${p.team ?? "No team"} · ${m.confidence.label}`,
      color: READING_COLOR[m.reading ?? ""] ?? "var(--chart-6)",
      hollow: m.confidence.level === "LOW",
      dashed: m.confidence.level === "LOW",
      emphasis: emphasis(p, state),
      keyLabel: keys.has(p.gsis_id),
      rows: [
        ["xFP/G", `${formatMetric(m.xfp_baseline, "num1") ?? "—"} → ${formatMetric(m.xfp_recent, "num1") ?? "—"}`],
        ["Δ xFP/G", formatMetric(m.xfp_delta, "signed") ?? "—"],
        ["PPR/G", `${formatMetric(m.ppr_baseline, "num1") ?? "—"} → ${formatMetric(m.ppr_recent, "num1") ?? "—"}`],
        ["Δ PPR/G", formatMetric(m.ppr_delta, "signed") ?? "—"],
        ["Sample", m.confidence.level.toLowerCase()],
      ],
      href: researchHref(slug, state, { sel: p.gsis_id, view: "momentum" }),
    };
  });
  const dom = symmetricDomain(usable.flatMap((p) => [p.momentum.xfp_delta ?? 0, p.momentum.ppr_delta ?? 0]), 4);
  return (
    <figure className={styles.figure}>
      <InteractiveScatter
        points={points}
        xLabel="Change in xFP per game"
        yLabel="Change in PPR per game"
        xFormat="signed"
        yFormat="signed"
        xDomain={dom}
        yDomain={dom}
        refX={[{ value: 0, label: "" }]}
        refY={[{ value: 0, label: "" }]}
        diagonal="Production change = workload change"
        corners={{ tr: "Both rising", br: "Workload up, points lag", tl: "Points up, workload flat", bl: "Both falling" }}
        ariaLabel={`${r.filters.position} change in expected against change in actual fantasy points per game`}
        emptyText="No player has the four qualifying games a momentum read needs"
      />
      <Legend items={[
        { color: "var(--chart-1)", label: "Workload rising faster than production" },
        { color: "var(--chart-3)", label: "Workload and production rising together" },
        { color: "var(--chart-2)", label: "Production rising without workload" },
        { color: "var(--chart-6)", label: "Other or no clear change" },
        { color: "var(--chart-6)", label: "Small sample (2 recent games)", hollow: true, dashed: true },
      ]} />
      <figcaption className={styles.caption}>
        Recent = the player&apos;s last 3 qualifying games (2 with only 4–5 games), baseline = up to 5 games before them, over the whole season regardless of the window filter.
        Both axes are points per game, so the diagonal is where production changed exactly as much as the role.
        {skipped > 0 && <> {skipped} player{skipped === 1 ? " has" : "s have"} fewer than 4 qualifying games and {skipped === 1 ? "is" : "are"} not drawn.</>}
      </figcaption>
    </figure>
  );
}
