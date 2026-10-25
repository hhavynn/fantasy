import Link from "@/components/LeagueLink";
import { BarChart, LineChart } from "@/components/charts";
import { PlayerPhoto } from "@/components/PlayerPhoto";
import { Badge, EmptyState, Notice, Pos, Section, Stat, StatStrip } from "@/components/ui";
import { label } from "@/lib/format";
import { providerName } from "@/lib/labels";
import { getLeagueProfile } from "@/lib/league-profile.server";
import { pctTick, valueDomain } from "@/lib/player-usage";
import {
  OPPORTUNITY_AXES, WORKLOAD_MAPS, formatMetric, metricRows, researchHref, tradeHref, waiverHref, weekList,
  type ResearchState, type Slug,
} from "@/lib/research";
import type { Confidence, PlayerResearch, ResearchPlayer, WeekState } from "@/lib/research-types";
import styles from "./research.module.css";

const CONF_TONE: Record<Confidence["level"], string> = { HIGH: "info", MEDIUM: "", LOW: "warn", INSUFFICIENT: "" };
const SIGNAL_NOTE = "Season-level signal from the existing conservative model (opportunity tier, role trend, production vs xFP). It is a prompt to research, not a trade recommendation.";

export function ConfidenceBadge({ c }: { c: Confidence }) {
  const text = c.level === "INSUFFICIENT" ? "Too few games" : `${label(c.level)} sample`;
  return <Badge value={text} tone={CONF_TONE[c.level]} title={`Momentum sample: ${c.label}. Reflects how many games back the comparison, not statistical certainty.`} />;
}

function Value({ v }: { v: string | null }) {
  return v === null ? <span className="missing" title="Not available for these games">—</span> : <>{v}</>;
}

/** The selected player's evidence: numbers for the current window, momentum, context, caveats and actions. */
export async function EvidencePanel({ slug, d, state, row }: { slug: Slug; d: PlayerResearch; state: ResearchState; row: ResearchPlayer | null }) {
  const prov = providerName((await getLeagueProfile()).provider);
  // Player profiles are keyed by ESPN ids for every league; only an ESPN league's readers know them by that name.
  const profileWord = prov === "ESPN" ? "ESPN profile" : "player profile";
  const p = d.player;
  const w = d.windows[state.window];
  const s = w.window;
  const m = d.momentum;
  const map = WORKLOAD_MAPS[p.position];
  const workload = metricRows(s, [map.x, map.y, ...map.tooltip])
    .filter(([k], i, all) => all.findIndex((o) => o[0] === k) === i && !["PPR/G", "FPOE/G", "Snap %"].includes(k));
  const trade = tradeHref(d.league, p.espn_player_id);
  const waiver = waiverHref(d.league, p.position);
  const partial = Object.entries(s.coverage ?? {});
  const clear = researchHref(slug, state, { sel: null, cmp: null });
  return (
    <Section
      id="evidence"
      title={
        <span className={styles.evidenceTitle}>
          {p.espn_player_id !== null && <PlayerPhoto id={p.espn_player_id} name={p.name} size={32} />}
          <span>{p.name}</span>
        </span>
      }
      actions={<Link href={clear} scroll={false} aria-label={`Clear selection of ${p.name}`}>Clear</Link>}
    >
      <p className={styles.evidenceMeta}>
        <Pos value={p.position} /> {p.team ?? "No team"} · {d.season} ·{" "}
        {s.games > 0 ? <>{s.games} qualifying game{s.games === 1 ? "" : "s"} ({weekList(s.weeks)}, {state.window === "season" ? "season" : `last ${s.games}`})</> : "no qualifying games in this window"}
      </p>
      {row === null && (
        <p className={styles.noteTight}>Not in the current player pool (outside the filters); the numbers below are this player&apos;s own window.</p>
      )}
      {s.games === 0 ? (
        <EmptyState title="No qualifying games">Every game needs nflverse xFP and PPR points to count.</EmptyState>
      ) : (
        <>
          <div className={styles.groupLine}><Badge value={w.group.label} tone={w.group.production === "below" ? "info" : w.group.production === "above" ? "warn" : ""} /></div>
          <StatStrip className={styles.evidenceStats}>
            <Stat label="xFP/G" value={<Value v={formatMetric(s.xfp_pg, "num1")} />} note="expected" />
            <Stat label="PPR/G" value={<Value v={formatMetric(s.ppr_pg, "num1")} />} note="actual" />
            <Stat label="FPOE/G" value={<Value v={formatMetric(s.fpoe_pg, "signed")} />} note={(s.fpoe_pg ?? 0) >= 0 ? "above expected" : "below expected"} />
            <Stat label="Snap %" value={<Value v={formatMetric(s.snap_share, "pct")} />} note="offense" />
          </StatStrip>
          <h3 className={styles.h3}>Workload</h3>
          <dl className={styles.kv}>
            {workload.map(([k, v]) => (<div key={k}><dt>{k}</dt><dd><Value v={v === "—" ? null : v} /></dd></div>))}
          </dl>
        </>
      )}

      <h3 className={styles.h3}>Opportunity trend <ConfidenceBadge c={m.confidence} /></h3>
      {m.confidence.level === "INSUFFICIENT" ? (
        <p className={styles.noteTight}>A trend needs 4 qualifying games; this player has {p.games_played}.</p>
      ) : (
        <>
          <dl className={styles.kv}>
            <div><dt>xFP/G</dt><dd>{formatMetric(m.xfp_baseline, "num1")} → {formatMetric(m.xfp_recent, "num1")} ({formatMetric(m.xfp_delta, "signed")})</dd></div>
            <div><dt>PPR/G</dt><dd>{formatMetric(m.ppr_baseline, "num1")} → {formatMetric(m.ppr_recent, "num1")} ({formatMetric(m.ppr_delta, "signed")})</dd></div>
            {[m.volume_metric, "offense_pct", m.share_metric].map((k) => {
              const t = m.metrics[k];
              if (!t || t.label === "INSUFFICIENT") return null;
              const fmt = k === "offense_pct" || k.endsWith("share") ? "pct" : "num1";
              return <div key={k}><dt>{TREND_LABEL[k] ?? k}</dt><dd>{formatMetric(t.baseline_avg, fmt)} → {formatMetric(t.recent_avg, fmt)}</dd></div>;
            })}
          </dl>
          <p className={styles.noteTight}>{weekList(m.baseline_weeks)} vs {weekList(m.recent_weeks).toLowerCase()}. {m.reading}.</p>
        </>
      )}

      <h3 className={styles.h3}>Context</h3>
      <dl className={styles.kv}>
        <div><dt>Market signal</dt><dd>{p.market?.market_signal ? <Badge value={p.market.market_signal} title={SIGNAL_NOTE} /> : <Value v={null} />}</dd></div>
        <div><dt>Opportunity tier</dt><dd>{p.market?.opportunity_tier ? label(p.market.opportunity_tier) : <Value v={null} />}</dd></div>
        <div><dt>Role trend</dt><dd>{p.market?.role_trend ? label(p.market.role_trend) : <Value v={null} />}</dd></div>
        <div><dt>Injury</dt><dd>{injuryText(d, prov)}</dd></div>
      </dl>
      <p className={styles.noteTight}>{SIGNAL_NOTE}</p>

      {w.research_groups.length > 0 && (
        <ul className={styles.groupNotes}>
          {w.research_groups.map((g) => (
            <li key={g}><b>{d.groups_info[g]?.title}.</b> {d.groups_info[g]?.caveat}</li>
          ))}
        </ul>
      )}
      {(p.excluded_games || partial.length > 0 || p.mapping_status === "ambiguous") && (
        <Notice tone="caution" title="Coverage">
          {p.excluded_games && <>Played in {weekList(p.excluded_games.weeks).toLowerCase()} but nflverse xFP is not published for it, so that game is left out of every average. </>}
          {partial.length > 0 && <>Some metrics cover fewer games than the window: {partial.map(([k, n]) => `${k.replaceAll("_", " ")} ${n}/${s.games}`).join(", ")}. </>}
          {p.mapping_status === "ambiguous" && (prov === "ESPN"
            ? <>Two ESPN players map to this nflverse id, so no ESPN profile is linked.</>
            : <>Two players map to this nflverse id, so no {profileWord} is linked.</>)}
        </Notice>
      )}

      <div className={styles.actions}>
        {p.espn_player_id !== null
          ? <Link href={`/players?id=${p.espn_player_id}`}>Full player profile</Link>
          : <span className="faint" title="No verified ESPN id match">No {profileWord}</span>}
        {trade && <Link href={trade}>{d.league?.is_mine ? "Shop in the trade analyzer" : "Explore a trade for this player"}</Link>}
        {waiver && <Link href={waiver}>Available: see {p.position} waivers</Link>}
        {d.league === null && p.espn_player_id !== null && <span className="faint">Not in the synced league</span>}
      </div>
      {trade && <p className={styles.noteTight}>The analyzer simulates lineups; it does not say whether a manager would accept.</p>}
    </Section>
  );
}

const TREND_LABEL: Record<string, string> = {
  targets: "Targets/G", opportunities: "Carries + targets/G", offense_pct: "Snap %",
  target_share: "Target share", backfield_rush_share: "Backfield share",
};

/** `prov` names whose designation `espn_status` is: the league's provider (Sleeper statuses use ESPN's vocabulary). */
function injuryText(d: PlayerResearch, prov: string) {
  const inj = d.player.injury;
  if (!inj) return <span className="faint">No designation on file</span>;
  const parts = [];
  if (inj.espn_status) parts.push(`${prov} ${label(inj.espn_status)}`);
  if (inj.nfl_report?.status) parts.push(`NFL report wk ${inj.nfl_report.week}: ${inj.nfl_report.status}${inj.nfl_report.injury ? ` (${inj.nfl_report.injury})` : ""}`);
  else if (inj.nfl_report?.practice) parts.push(`Practice wk ${inj.nfl_report.week}: ${inj.nfl_report.practice}`);
  return parts.length ? parts.join(" · ") : <span className="faint">No designation on file</span>;
}

const STATE_TONE: Record<WeekState["state"], string> = {
  played: "", missing_xfp: "warn", no_offensive_snaps: "", bye: "", did_not_play: "bad", unknown: "",
};

/** Weekly timeline: synchronized small multiples (shared week axis), then the week table. */
export function Timeline({ d }: { d: PlayerResearch }) {
  const t = d.timeline;
  if (t.length === 0) return null;
  const weeks = t.map((w) => String(w.week));
  const val = (k: keyof WeekState) => t.map((w) => (w.state === "played" || w.state === "missing_xfp" || w.state === "no_offensive_snaps" ? (w[k] as number | null | undefined) ?? null : null));
  const pos = d.player.position;
  const usage = pos === "RB"
    ? { title: "Backfield share and snap share", series: [{ id: "bf", label: "Backfield designed-rush share", points: val("backfield_rush_share") }, { id: "snap", label: "Snap share", points: val("offense_pct") }] }
    : pos === "TE"
      ? { title: "Snap share and target share", series: [{ id: "snap", label: "Snap share", points: val("offense_pct") }, { id: "tgt", label: "Target share", points: val("target_share") }] }
      : { title: "Target and air-yard share", series: [{ id: "tgt", label: "Target share", points: val("target_share") }, { id: "air", label: "Air-yard share", points: val("air_yards_share") }] };
  // One neutral tone: above or below expectation is direction (sign and side of zero), not good or bad.
  const fpoe = t.map((w) => ({ label: String(w.week), value: w.state === "played" ? w.fpoe ?? null : null, tone: "default" as const }));
  return (
    <Section title={`${d.player.name}: week by week`}
      footnote="A gap or dash is a week with no measured value (bye, missed game, or a source not yet published). It is never drawn as zero; the status column says which.">
      <div className={styles.multiples}>
        <div>
          <h3 className={styles.h3}>PPR points vs expected</h3>
          <LineChart series={[{ id: "ppr", label: "PPR points", points: val("fantasy_points_ppr") }, { id: "xfp", label: "xFP", points: val("xfp") }]}
            xLabels={weeks} height={200} width={420} markers ariaLabel={`${d.player.name} PPR points and expected points by week`} />
        </div>
        <div>
          <h3 className={styles.h3}>FPOE (PPR − xFP)</h3>
          <BarChart data={fpoe} height={200} width={420} ariaLabel={`${d.player.name} fantasy points over expectation by week`} />
        </div>
        <div>
          <h3 className={styles.h3}>{usage.title}</h3>
          <LineChart series={usage.series} xLabels={weeks} height={200} width={420} markers yFormat={pctTick} yDomain={valueDomain(usage.series.flatMap((x) => x.points), "pct")}
            ariaLabel={`${d.player.name} ${usage.title.toLowerCase()} by week`} />
        </div>
      </div>
      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              <th scope="col">Week</th><th scope="col">Status</th><th scope="col" className="num">xFP</th><th scope="col" className="num">PPR</th>
              <th scope="col" className="num">FPOE</th><th scope="col" className="num">Snap %</th><th scope="col" className="num">Tgt</th>
              {pos === "RB" ? <><th scope="col" className="num">Rush</th><th scope="col" className="num">BF %</th><th scope="col" className="num">RZ car</th><th scope="col" className="num">GL car</th></>
                : <><th scope="col" className="num">Tgt %</th><th scope="col" className="num">{pos === "WR" ? "Air %" : "RZ tgt"}</th></>}
            </tr>
          </thead>
          <tbody>
            {t.map((w) => (
              <tr key={w.week}>
                <td>{w.week}{w.opponent ? <span className="faint"> v {w.opponent}</span> : null}</td>
                <td><Badge value={w.label} tone={STATE_TONE[w.state]} /></td>
                <td className="num"><Value v={formatMetric(w.xfp, "num1")} /></td>
                <td className="num"><Value v={formatMetric(w.fantasy_points_ppr, "num1")} /></td>
                <td className="num"><Value v={formatMetric(w.fpoe, "signed")} /></td>
                <td className="num"><Value v={formatMetric(w.offense_pct, "pct")} /></td>
                <td className="num"><Value v={w.targets === undefined || w.targets === null ? null : String(w.targets)} /></td>
                {pos === "RB" ? (
                  <>
                    <td className="num"><Value v={w.designed_rushes == null ? null : String(w.designed_rushes)} /></td>
                    <td className="num"><Value v={formatMetric(w.backfield_rush_share, "pct")} /></td>
                    <td className="num"><Value v={w.rz_carries == null ? null : String(w.rz_carries)} /></td>
                    <td className="num"><Value v={w.gl_carries == null ? null : String(w.gl_carries)} /></td>
                  </>
                ) : (
                  <>
                    <td className="num"><Value v={formatMetric(w.target_share, "pct")} /></td>
                    <td className="num"><Value v={pos === "WR" ? formatMetric(w.air_yards_share, "pct") : w.rz_targets == null ? null : String(w.rz_targets)} /></td>
                  </>
                )}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </Section>
  );
}

/** Two players side by side on the same window definition (each over their own qualifying games). */
export function Compare({ slug, a, b, state }: { slug: Slug; a: PlayerResearch; b: PlayerResearch; state: ResearchState }) {
  const map = WORKLOAD_MAPS[a.player.position];
  const axes = [OPPORTUNITY_AXES.x, OPPORTUNITY_AXES.ppr, OPPORTUNITY_AXES.fpoe, map.x, map.y, ...map.tooltip]
    .filter((ax, i, all) => all.findIndex((o) => o.key === ax.key) === i);
  const wa = a.windows[state.window].window;
  const wb = b.windows[state.window].window;
  const ra = metricRows(wa, axes);
  const rb = metricRows(wb, axes);
  const crossPosition = a.player.position !== b.player.position;
  return (
    <Section title="Comparison" actions={<Link href={researchHref(slug, state, { cmp: null })} scroll={false}>Close comparison</Link>}>
      {crossPosition && (
        <Notice tone="caution">These players play different positions, so the workload measures do not mean the same thing; compare xFP/G and FPOE/G only.</Notice>
      )}
      <div className="table-wrap">
        <table>
          <thead>
            <tr><th scope="col">Metric</th><th scope="col" className="num">{a.player.name}</th><th scope="col" className="num">{b.player.name}</th></tr>
          </thead>
          <tbody>
            <tr><td>Games (window)</td><td className="num">{wa.games}</td><td className="num">{wb.games}</td></tr>
            {ra.map(([k, v], i) => (
              <tr key={k}><td>{k}</td><td className="num"><Value v={v === "—" ? null : v} /></td><td className="num"><Value v={rb[i][1] === "—" ? null : rb[i][1]} /></td></tr>
            ))}
            <tr><td>Δ xFP/G (momentum)</td><td className="num"><Value v={formatMetric(a.momentum.xfp_delta, "signed")} /></td><td className="num"><Value v={formatMetric(b.momentum.xfp_delta, "signed")} /></td></tr>
            <tr><td>Momentum sample</td><td className="num"><ConfidenceBadge c={a.momentum.confidence} /></td><td className="num"><ConfidenceBadge c={b.momentum.confidence} /></td></tr>
            <tr><td>Market signal</td><td className="num"><Badge value={a.player.market?.market_signal} /></td><td className="num"><Badge value={b.player.market?.market_signal} /></td></tr>
          </tbody>
        </table>
      </div>
      <p className={styles.noteTight}>Each column uses that player&apos;s own qualifying games for the window ({weekList(wa.weeks).toLowerCase()} vs {weekList(wb.weeks).toLowerCase()}).</p>
    </Section>
  );
}
