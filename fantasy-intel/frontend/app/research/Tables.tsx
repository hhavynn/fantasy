import Link from "next/link";
import { PlayerPhoto } from "@/components/PlayerPhoto";
import { Badge, Section, SortableTh, TrendArrow, VisuallyHidden } from "@/components/ui";
import { sortRows, type SortState } from "@/lib/sort";
import {
  COLUMNS, HEAT_LEGEND, formatMetric, heatBucket, researchHref, sortValue, stateParams, weekList,
  type ResearchState, type Slug,
} from "@/lib/research";
import type { GroupInfo, OpportunityResponse, ResearchPlayer } from "@/lib/research-types";
import { ConfidenceBadge } from "./Evidence";
import styles from "./research.module.css";

const ordinal = (n: number) => {
  const s = ["th", "st", "nd", "rd"];
  const v = n % 100;
  return `${n}${s[(v - 20) % 10] ?? s[v] ?? s[0]}`;
};

function Missing() {
  return <span className="missing" title="Not available: a source did not cover these games">—</span>;
}

/** Sortable opportunity leaderboard with a restrained position-relative heatmap (text always shown). */
export function Leaderboard({ slug, r, state, sort }: { slug: Slug; r: OpportunityResponse; state: ResearchState; sort: SortState }) {
  const pos = r.filters.position;
  const cols = COLUMNS[pos];
  const rows = sortRows(r.players, (p) => sortValue(p, sort.key), sort.dir);
  const sp = stateParams(state);
  return (
    <Section
      title="Opportunity leaderboard"
      flush
      actions={<HeatLegend />}
      footnote={<>Tints rank each value among the {r.players.length} {pos}s shown (position-relative, higher = more of it; for FPOE, higher = further above expectation). A dash is missing data and is never ranked. Sort any column; missing values stay last.</>}
    >
      <div className="table-wrap">
        <table className={styles.board}>
          <thead>
            <tr>
              <SortableTh sortKey="name" label="Player" current={sort} searchParams={sp} />
              {cols.map((c) => c.key === "trend" || c.key === "signal"
                ? <th key={c.key} scope="col" title={c.title}>{c.label}</th>
                : <SortableTh key={c.key} sortKey={c.key} label={c.label} title={c.title} current={sort} searchParams={sp} align="right" />)}
            </tr>
          </thead>
          <tbody>
            {rows.map((p) => (
              <tr key={p.gsis_id} className={p.gsis_id === state.sel ? "mine" : undefined}>
                <td>
                  <Link className="player-link" href={researchHref(slug, state, { sel: p.gsis_id })} scroll={false}
                        aria-current={p.gsis_id === state.sel ? "true" : undefined}>
                    {p.espn_player_id !== null && <PlayerPhoto id={p.espn_player_id} name={p.name} inline />}{p.name}
                  </Link>
                  <span className={styles.teamTag}>{p.team ?? "—"}</span>
                </td>
                {cols.map((c) => <Cell key={c.key} p={p} col={c.key} format={c.format} heat={!!c.heat} />)}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </Section>
  );
}

function Cell({ p, col, format, heat }: { p: ResearchPlayer; col: string; format: Parameters<typeof formatMetric>[1]; heat: boolean }) {
  if (col === "games") return <td className="num" title={weekList(p.window.weeks)}>{p.window.games}</td>;
  if (col === "momentum") {
    const v = formatMetric(p.momentum.xfp_delta, "signed");
    return <td className="num" title={p.momentum.confidence.label}>{v ?? <Missing />}{v && p.momentum.confidence.level === "LOW" ? <span className={styles.lowSample} title="Small sample: 2 recent games"> *</span> : null}</td>;
  }
  if (col === "trend") return <td>{p.market?.role_trend ? <TrendArrow label={p.market.role_trend} /> : <Missing />}</td>;
  if (col === "signal") return <td>{p.market?.market_signal ? <Badge value={p.market.market_signal} title="Existing season-level signal; a research prompt, not a recommendation" /> : <Missing />}</td>;
  const raw = (p.window as Record<string, unknown>)[col];
  const v = formatMetric(typeof raw === "number" ? raw : null, format);
  const pct = p.percentiles?.[col];
  const b = heat && v !== null ? heatBucket(pct) : null;
  return (
    <td className={`num ${b ? styles[`heat${b}`] : ""}`} title={b ? `${ordinal(pct as number)} percentile among shown ${p.position}s` : undefined}>
      {v ?? <Missing />}
      {b ? <VisuallyHidden>, {ordinal(pct as number)} percentile</VisuallyHidden> : null}
    </td>
  );
}

function HeatLegend() {
  return (
    <ul className={styles.heatLegend} aria-label="Heat legend: position percentile">
      {HEAT_LEGEND.map((h) => (
        <li key={h.bucket}><span className={`${styles.heatSwatch} ${styles[`heat${h.bucket}`]}`} aria-hidden="true" />{h.label}</li>
      ))}
    </ul>
  );
}

/** Red-zone and goal-line work with raw totals, so a sparse sample is visible next to the rate. */
export function ScoringArea({ slug, r, state }: { slug: Slug; r: OpportunityResponse; state: ResearchState }) {
  const rb = r.filters.position === "RB";
  const total = (p: ResearchPlayer) => (rb ? (p.window.rz_carries_total ?? null) : (p.window.rz_targets_total ?? null));
  const rows = sortRows(r.players.filter((p) => total(p) !== null), total, "desc").slice(0, 10);
  if (rows.length === 0) return null;
  return (
    <Section
      title="High-value touches"
      flush
      footnote={<>Red zone = snaps inside the opponent&apos;s 20; goal line = inside the 5 (designed runs only, no scrambles). These counts are small: a handful of plays moves the rate a lot, so the totals are shown beside it. No touchdown-conversion rate is applied.</>}
    >
      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              <th scope="col">Player</th>
              <th scope="col" className="num">G</th>
              {rb ? <><th scope="col" className="num">RZ carries</th><th scope="col" className="num">GL carries</th><th scope="col" className="num">RZ car/G</th></>
                : <><th scope="col" className="num">RZ targets</th><th scope="col" className="num">RZ tgt/G</th></>}
              <th scope="col" className="num">Tgt/G</th>
              <th scope="col" className="num">xFP/G</th>
              <th scope="col">Sample</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((p) => {
              const n = total(p) ?? 0;
              return (
                <tr key={p.gsis_id}>
                  <td><Link className="player-link" href={researchHref(slug, state, { sel: p.gsis_id })} scroll={false}>{p.name}</Link><span className={styles.teamTag}>{p.team}</span></td>
                  <td className="num">{p.window.games}</td>
                  {rb ? (
                    <>
                      <td className="num">{n}</td>
                      <td className="num">{p.window.gl_carries_total ?? <Missing />}</td>
                      <td className="num">{formatMetric(p.window.rz_carries_pg, "num1") ?? <Missing />}</td>
                    </>
                  ) : (
                    <>
                      <td className="num">{n}</td>
                      <td className="num">{formatMetric(p.window.rz_targets_pg, "num1") ?? <Missing />}</td>
                    </>
                  )}
                  <td className="num">{formatMetric(p.window.targets_pg, "num1") ?? <Missing />}</td>
                  <td className="num">{formatMetric(p.window.xfp_pg, "num1") ?? <Missing />}</td>
                  <td>{n < 5 ? <Badge value="Sparse" tone="warn" title={`Only ${n} red-zone opportunities`} /> : <span className="faint">{n} plays</span>}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </Section>
  );
}

const GROUP_ORDER = ["underperforming_opportunity", "emerging_role", "production_ahead"] as const;

function groupScore(g: string, p: ResearchPlayer): number {
  if (g === "emerging_role") return p.momentum.xfp_delta ?? 0;
  if (g === "underperforming_opportunity") return -(p.window.fpoe_pg ?? 0);
  return p.window.fpoe_pg ?? 0;
}

/** The three exploratory research groups, strongest evidence first. Separate from any recommendation. */
export function ResearchGroups({ slug, r, state, limit = 5 }: { slug: Slug; r: OpportunityResponse; state: ResearchState; limit?: number }) {
  return (
    <div className={styles.groups}>
      {GROUP_ORDER.map((g) => {
        const info: GroupInfo | undefined = r.groups_info[g];
        const members = r.players.filter((p) => p.research_groups.includes(g)).sort((a, b) => groupScore(g, b) - groupScore(g, a));
        return (
          <section key={g} className={styles.group} aria-labelledby={`grp-${slug}-${g}`}>
            <h3 id={`grp-${slug}-${g}`} className={styles.groupTitle}>{info?.title ?? g}</h3>
            {members.length === 0 ? (
              <p className={styles.noteTight}>No {r.filters.position} in the current pool meets the rule.</p>
            ) : (
              <ol className={styles.groupList}>
                {members.slice(0, limit).map((p) => (
                  <li key={p.gsis_id}>
                    <Link className="player-link" href={researchHref(slug, state, { sel: p.gsis_id, view: g === "emerging_role" ? "momentum" : state.view })} scroll={false}>{p.name}</Link>
                    <span className={styles.teamTag}>{p.team}</span>
                    <span className={styles.groupStat}>{groupEvidence(g, p)}</span>
                    {g === "emerging_role" && <ConfidenceBadge c={p.momentum.confidence} />}
                  </li>
                ))}
              </ol>
            )}
            {members.length > limit && <p className={styles.noteTight}>{members.length - limit} more in the table below.</p>}
            {info && (
              <details className={styles.groupRule}>
                <summary>Rule and caveat</summary>
                <p>{info.rule}</p>
                <p>{info.caveat}</p>
              </details>
            )}
          </section>
        );
      })}
    </div>
  );
}

function groupEvidence(g: string, p: ResearchPlayer): string {
  const w = p.window;
  if (g === "emerging_role") {
    const m = p.momentum;
    return `xFP/G ${formatMetric(m.xfp_baseline, "num1") ?? "—"} → ${formatMetric(m.xfp_recent, "num1") ?? "—"}`;
  }
  return `${formatMetric(w.xfp_pg, "num1")} xFP/G, ${formatMetric(w.fpoe_pg, "signed")} FPOE/G`;
}
