import Link from "next/link";
import { PlayerPhoto } from "@/components/PlayerPhoto";
import { Badge, Notice, PageHeader, Pos, Score, Section, Stat, StatStrip, TrendArrow } from "@/components/ui";
import { label, num, pct, signed } from "@/lib/format";
import { metricLabel, tidyReason } from "@/lib/reason-text";
import { BASIS_LABEL } from "@/lib/types";
import { ForwardView } from "./ForwardView";
import { GameLog } from "./GameLog";
import { ContextSection, RouteSection, ScheduleLink } from "./PlayerContext";
import { SearchForm } from "./PlayerSearch";
import { UsageCharts } from "./UsageCharts";
import { hasInjuryDesignation, ownerBadge } from "./helpers";
import styles from "./players.module.css";
import type { Detail } from "./types";

const SKILL = ["QB", "RB", "WR", "TE"];

/** A player profile: who and what state, the key numbers, the verdict signals, then the evidence by week. */
export function PlayerProfile({ d, q, cols }: { d: Detail; q: string; cols: "all" | "core" }) {
  const p = d.player;
  const f = d.forward_projection;
  const hasUsage = d.weekly.length > 0 || d.score !== null;
  return (
    <div className="stack">
      <ProfileHeader d={d} q={q} />
      <KeyNumbers d={d} hasUsage={hasUsage} />

      {p.match_method === "unmatched" && (
        <Notice tone="caution" title="No usage data for this player">
          There is no nflverse ID match for this ESPN player, so the usage numbers and charts below are empty. {p.mapping_notes}
        </Notice>
      )}

      {d.waiver && (
        <Section title="Waiver view" actions={<Link href="/waivers">All waivers</Link>}>
          <div className={styles.badgeRow}>
            <Badge value={d.waiver.recommendation} />
            <span>Waiver score {d.waiver.waiver_score.toFixed(2)}</span>
          </div>
          <ul className={styles.reasons}>{d.waiver.reasons.map((r, i) => <li key={i}>{tidyReason(r)}</li>)}</ul>
        </Section>
      )}

      {d.score && <ValueAndTrends d={d} />}

      <UsageCharts d={d} />

      {f && <ForwardView f={f} />}
      {SKILL.includes(p.position ?? "") && <ScheduleLink id={p.espn_player_id} />}

      {d.context && <ContextSection d={d} />}
      {d.route_data && <RouteSection r={d.route_data} />}

      <GameLog d={d} all={cols === "all"} id={String(p.espn_player_id)} />
    </div>
  );
}

function ProfileHeader({ d, q }: { d: Detail; q: string }) {
  const p = d.player;
  const f = d.forward_projection;
  const owner = ownerBadge(p.fantasy_team, p.availability_status);
  const byes = f?.summary.bye_weeks ?? [];
  const latestInj = d.injuries[0];
  const report = latestInj?.report_status;
  return (
    <PageHeader
      title={<span className={styles.titleLine}><PlayerPhoto id={p.espn_player_id} name={p.full_name} size={56} />{p.full_name}</span>}
      meta={
        <span className={styles.metaLine}>
          <Pos value={p.position} />
          <span>{p.pro_team ?? "No NFL team"}</span>
          {byes.length > 0 && <span>· Bye week {byes.join(", ")}</span>}
          <Badge value={owner.text} tone={owner.tone} />
          {hasInjuryDesignation(p.injury_status) && <Badge value={p.injury_status} title="ESPN injury designation" />}
        </span>
      }
      actions={<SearchForm q={q} compact />}
    >
      <ul className={styles.facts}>
        <li>{num(p.percent_owned, 1)}% owned ESPN-wide ({signed(p.percent_change)} this week)</li>
        <li>Season {num(p.season_points)} league pts ({num(p.season_avg_points)}/g)</li>
        {d.depth_chart && <li><span className={styles.factLabel}>Depth chart</span> {d.depth_chart.pos_abb}{d.depth_chart.pos_rank} ({d.depth_chart.team})</li>}
        {hasInjuryDesignation(p.injury_status) && (
          <li><span className={styles.factLabel}>ESPN</span> <Badge value={p.injury_status} /></li>
        )}
        {report ? (
          <li>
            <span className={styles.factLabel}>NFL report wk {latestInj.week}</span> <Badge value={report} />
            {latestInj.report_primary_injury ? <span>{latestInj.report_primary_injury}</span> : null}
          </li>
        ) : latestInj?.practice_status ? (
          <li><span className={styles.factLabel}>Practice wk {latestInj.week}</span> {latestInj.practice_status}</li>
        ) : null}
        <li>
          <span className={styles.factLabel} title="How this ESPN player was matched to nflverse data; coverage is on the Data quality page">ID map</span> <span className="mono">{p.match_method ?? "none"}</span>
          {p.match_confidence && !["high", "n/a"].includes(p.match_confidence) && (
            <Badge value={`${label(p.match_confidence)} confidence`} tone="warn" title="Matched by name rather than by an ID; verify before relying on the usage numbers" />
          )}
        </li>
      </ul>
    </PageHeader>
  );
}

function KeyNumbers({ d, hasUsage }: { d: Detail; hasUsage: boolean }) {
  const p = d.player;
  const f = d.forward_projection;
  if (!hasUsage) {
    return (
      <StatStrip>
        <Stat label="League pts / game" value={num(p.season_avg_points, 1, "No ESPN games yet")} note={<>{num(p.season_points)} season pts</>} />
        <Stat label="This week (ESPN)" value={num(f?.summary.current_week_projection, 1, "No ESPN projection for this week")} note={f ? `week ${f.summary.as_of_week}` : undefined} />
        <Stat label="Rest of season (ESPN)" value={num(f?.summary.ros_projected_points, 1, "Not every remaining week has an ESPN projection")} note={f ? `${f.summary.ros_weeks_available}/${f.summary.ros_weeks_expected} wk` : undefined} />
        <Stat label="Owned" value={<>{num(p.percent_owned, 1)}%</>} note={<>{signed(p.percent_change)} this week</>} />
      </StatStrip>
    );
  }
  const gap = p.fp_minus_xfp;
  return (
    <StatStrip>
      <Stat label="PPR / game" value={num(p.fp_per_game, 1, "No games / no NFL id")} note={p.games !== null ? `${p.games} ${p.games === 1 ? "game" : "games"}` : undefined} />
      <Stat label="Expected PPR / game" value={num(p.xfp_per_game, 1, "No xFP for this player")} note="nflverse xFP" />
      <Stat
        label="Actual − expected"
        value={gap === null || gap === undefined ? num(gap) : `${gap > 0 ? "+" : gap < 0 ? "−" : ""}${Math.abs(gap).toFixed(1)}`}
        tone={gap === null || gap === undefined || gap === 0 ? undefined : gap > 0 ? "positive" : "negative"}
        note={p.value_gap_label ? label(p.value_gap_label) : undefined}
      />
      <Stat label="Value score" value={num(p.value_score, 1, "Not computed")} note="0–10 rest-of-season proxy" />
      <Stat label="Market signal" value={p.market_signal ? label(p.market_signal) : num(null, 1, "Not computed")} note={p.opportunity_tier ? `${label(p.opportunity_tier)} role` : undefined} />
      <Stat
        label="Usage trend"
        value={p.trend_label ? <><TrendArrow label={p.trend_label} /> {label(p.trend_label)}</> : num(null, 1, "Not enough games for a trend")}
        note={p.usage_trend_score !== null ? <>{signed(p.usage_trend_score, 2)} score</> : undefined}
      />
    </StatStrip>
  );
}

/** The signals behind the verdict, then the recent-vs-baseline table that produced the trend label. */
function ValueAndTrends({ d }: { d: Detail }) {
  const p = d.player;
  const score = d.score!;
  return (
    <div className={styles.pair}>
      <Section title="Underlying value vs box score">
        <div className={styles.badgeRow}>
          <Badge value={p.market_signal} title="Market signal (needs a meaningful absolute role for BUY)" />
          <span className="faint">role</span><Badge value={p.opportunity_tier} title="Absolute opportunity tier" />
          <span className="faint">trend</span><Badge value={p.role_trend} title="Composite usage trend" />
          <span className="faint">production</span>
          <Badge
            value={p.production_sustainability ?? p.value_gap_label}
            title="Production vs the opportunity (xFP) baseline. Regression evidence only, not a sell signal: xFP is an average-player model."
          />
        </div>
        <div className="kv">
          <span className="k">Opportunity score</span>
          <span><Score value={p.opportunity_score} /> <span className="faint">basis: {BASIS_LABEL[p.opportunity_basis ?? "unavailable"] ?? p.opportunity_basis}</span></span>
          <span className="k">Production score</span><Score value={p.production_score} />
          <span className="k">Usage trend score</span><span>{signed(p.usage_trend_score, 2)} <span className="faint">(−1…+1)</span></span>
          <span className="k">PPR / game</span><span>{num(p.fp_per_game)}</span>
          <span className="k">Expected PPR / game</span><span>{num(p.xfp_per_game)} <span className="faint">(nflverse xFP)</span></span>
          <span className="k">Actual − expected</span><span>{signed(p.fp_minus_xfp)}</span>
          <span className="k">Value score</span><Score value={p.value_score} />
        </div>
        <ul className={styles.reasons}>{score.reasons.map((r, i) => <li key={i}>{tidyReason(r)}</li>)}</ul>
        <p className={styles.note}>
          Opportunity basis: {BASIS_LABEL[score.components.opportunity_basis] ?? score.components.opportunity_basis}, weeks {score.components.recent_weeks.join(", ")}. {score.components.value_formula}.
        </p>
      </Section>

      <Section title="Usage trends (recent vs baseline games)" flush>
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Metric</th><th className="num">Baseline</th><th className="num">Recent</th><th className="num">Δ</th>
                <th className="num">Threshold</th><th>Label</th>
              </tr>
            </thead>
            <tbody>
              {score.components.trends.map((t) => {
                const isPct = ["offense_pct", "target_share", "team_rush_share", "backfield_rush_share"].includes(t.metric);
                const fmt = (v: number | null) => (isPct ? pct(v) : num(v));
                return (
                  <tr key={t.metric}>
                    <td>{metricLabel(t.metric)}</td>
                    <td className="num">{fmt(t.baseline_avg)} <span className="faint">{t.baseline_weeks.length ? `wk ${t.baseline_weeks.join(",")}` : ""}</span></td>
                    <td className="num">{fmt(t.recent_avg)} <span className="faint">{t.recent_weeks.length ? `wk ${t.recent_weeks.join(",")}` : ""}</span></td>
                    <td className="num">{t.delta === null ? <span className="missing" title="Not enough games">—</span> : isPct ? signed(t.delta * 100, 0) : signed(t.delta)}</td>
                    <td className="num faint">{isPct ? `${(t.threshold * 100).toFixed(0)} pts` : t.threshold}</td>
                    <td><Badge value={t.label} /></td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </Section>
    </div>
  );
}
