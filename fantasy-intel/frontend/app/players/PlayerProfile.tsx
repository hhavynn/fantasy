import Link from "next/link";
import type { ReactNode } from "react";
import { CompareLink } from "@/components/CompareLink";
import { PlayerPhoto } from "@/components/PlayerPhoto";
import { Badge, Notice, PageHeader, Pos, ProvenanceBadge, Rating, Section, Stat, StatStrip, TabList, TabPanel, TrendArrow } from "@/components/ui";
import { label, num, pct, signed } from "@/lib/format";
import { isOursNumber, oursBlock, oursByWeek } from "@/lib/forward-view";
import { OWNERSHIP_UNAVAILABLE, ownershipAvailable, providerName } from "@/lib/labels";
import { ESPN_PROFILE, type LeagueProfile } from "@/lib/league-profile";
import { profileTabItems, type ProfileState } from "@/lib/player-tabs";
import { RATING_CAVEAT, ratingFor } from "@/lib/ratings";
import { metricLabel, tidyReason } from "@/lib/reason-text";
import { signalsOf } from "@/lib/signal-provenance";
import { isShortlistCandidate } from "@/lib/waiver-shortlist";
import { BASIS_LABEL } from "@/lib/types";
import { SavePlayer, ShortlistProvider } from "../waivers/Shortlist";
import { SearchForm } from "./PlayerSearch";
import { hasInjuryDesignation, ownerBadge } from "./helpers";
import styles from "./players.module.css";
import type { Detail } from "./types";

const SKILL = ["QB", "RB", "WR", "TE"];
const TABS_ID = "player-tabs";

/**
 * A player profile: one header (who, what state, the key numbers, compare and save) above URL-addressable tabs.
 * Only the selected tab's panel is rendered, so a tab's evidence is fetched and drawn when it is opened.
 * `leagueId` is the scoped league (null on the classic site), for the device-local waiver shortlist.
 */
export function PlayerProfile({ d, state, profile = ESPN_PROFILE, leagueId = null, children }: {
  d: Detail;
  state: ProfileState;
  /** The league's provider and whether it publishes projections (a Sleeper league: no ESPN numbers, no ownership). */
  profile?: LeagueProfile;
  leagueId?: string | null;
  /** The selected tab's content. */
  children: ReactNode;
}) {
  const p = d.player;
  const hasUsage = d.weekly.length > 0 || d.score !== null;
  const body = (
    <div className="stack">
      <div className={styles.hero}>
        <ProfileHeader d={d} q={state.q} profile={profile} leagueId={leagueId} />
        <div className={styles.heroStrip}><KeyNumbers d={d} hasUsage={hasUsage} profile={profile} /></div>
      </div>

      {p.match_method === "unmatched" && (
        <Notice tone="caution" title="No usage data for this player">
          There is no nflverse ID match for this {profile.provider === "espn" ? "ESPN player" : "player"}, so the usage numbers and charts are empty. {p.mapping_notes}
        </Notice>
      )}

      <div className={styles.tabs}>
        <TabList label={`${p.full_name} profile`} idBase={TABS_ID} current={state.tab} items={profileTabItems(state)} />
        <TabPanel idBase={TABS_ID} current={state.tab} className={`stack ${styles.panel}`}>{children}</TabPanel>
      </div>
    </div>
  );
  // Save is the waiver shortlist's action (device-local, per league), so it is offered for a scoped league only.
  return leagueId ? <ShortlistProvider key={leagueId} leagueId={leagueId}>{body}</ShortlistProvider> : body;
}

function ProfileHeader({ d, q, profile, leagueId }: { d: Detail; q: string; profile: LeagueProfile; leagueId: string | null }) {
  const provider = providerName(profile.provider);
  const p = d.player;
  const f = d.forward_projection;
  const owner = ownerBadge(p.fantasy_team, p.availability_status);
  const byes = f?.summary.bye_weeks ?? [];
  const latestInj = d.injuries[0];
  const report = latestInj?.report_status;
  return (
    <PageHeader
      eyebrow="Players · Profile"
      title={<span className={styles.titleLine}><PlayerPhoto id={p.espn_player_id} name={p.full_name} size={64} />{p.full_name}</span>}
      meta={
        <span className={styles.metaLine}>
          <Pos value={p.position} />
          <span>{p.pro_team ?? "No NFL team"}</span>
          {byes.length > 0 && <span>· Bye week {byes.join(", ")}</span>}
          <Badge value={owner.text} tone={owner.tone} />
          {hasInjuryDesignation(p.injury_status) && <Badge value={p.injury_status} title={`${provider} injury designation`} />}
        </span>
      }
      provenance={<ProvenanceBadge signals={signalsOf(d.provenance)} />}
      actions={<>
        <CompareLink a={p.espn_player_id} aName={p.full_name} />
        {/* The shortlist holds waiver candidates: only a free agent or waiver player, never a rostered or unknown one. */}
        {leagueId && isShortlistCandidate(p.availability_status, p.fantasy_team) && <SavePlayer id={p.espn_player_id} name={p.full_name} />}
        <RankingsLink position={p.position} />
        <SearchForm q={q} compact />
      </>}
    >
      <ul className={styles.facts}>
        {ownershipAvailable(profile.provider)
          ? <li>{num(p.percent_owned, 1)}% owned ESPN-wide ({signed(p.percent_change)} this week)</li>
          : <li className="faint">{OWNERSHIP_UNAVAILABLE}</li>}
        <li>Season {num(p.season_points)} league pts ({num(p.season_avg_points)}/g)</li>
        {d.depth_chart && <li><span className={styles.factLabel}>Depth chart</span> {d.depth_chart.pos_abb}{d.depth_chart.pos_rank} ({d.depth_chart.team})</li>}
        {hasInjuryDesignation(p.injury_status) && (
          <li><span className={styles.factLabel}>{provider}</span> <Badge value={p.injury_status} /></li>
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
          <span className={styles.factLabel} title={`How this ${profile.provider === "espn" ? "ESPN player" : "player"} was matched to nflverse data; coverage is on the Data quality page`}>ID map</span> <span className="mono">{p.match_method ?? "none"}</span>
          {p.match_confidence && !["high", "n/a"].includes(p.match_confidence) && (
            <Badge value={`${label(p.match_confidence)} confidence`} tone="warn" title="Matched by name rather than by an ID; verify before relying on the usage numbers" />
          )}
        </li>
      </ul>
    </PageHeader>
  );
}

function KeyNumbers({ d, hasUsage, profile }: { d: Detail; hasUsage: boolean; profile: LeagueProfile }) {
  const p = d.player;
  const f = d.forward_projection;
  if (!hasUsage && !profile.providerProjections) {
    // A Sleeper league: no ESPN projections and no ownership; the forward numbers are Fantasy Intel's own.
    const ours = f ? oursBlock(f) : null;
    const week = ours?.available && f ? oursByWeek(ours).get(f.summary.as_of_week) : undefined;
    return (
      <StatStrip>
        <Stat label="League pts / game" value={num(p.season_avg_points, 1, "No games yet")} note={<>{num(p.season_points)} season pts</>} />
        <Stat label="This week (Ours)" value={num(week && isOursNumber(week) ? week.value : null, 1, "No Fantasy Intel projection for this week")} note={f ? `week ${f.summary.as_of_week}` : undefined} />
        <Stat label="Regular season left (Ours)" value={num(ours?.available ? ours.ros_projected_points : null, 1, "Not every remaining week has a Fantasy Intel projection")} note={ours?.available ? `${ours.ros_weeks_available}/${ours.ros_weeks_expected} wk` : undefined} />
        <Stat label="Owned" value={num(null, 1, OWNERSHIP_UNAVAILABLE)} note="Not available for Sleeper leagues" />
      </StatStrip>
    );
  }
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
  const valueRating = ratingFor(p, "value");
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
      <Stat label="Value rating" value={<Rating rating={valueRating} kind="value" variant="text" why="Not computed" />} note={valueRating?.scale === 10 ? "legacy 0–10 value; 0–100 not computed yet" : "relative rating, 0–100"} />
      <Stat label="Market signal" value={p.market_signal ? label(p.market_signal) : num(null, 1, "Not computed")} note={p.opportunity_tier ? `${label(p.opportunity_tier)} role` : undefined} />
      <Stat
        label="Usage trend"
        value={p.trend_label ? <><TrendArrow label={p.trend_label} /> {label(p.trend_label)}</> : num(null, 1, "Not enough games for a trend")}
        note={p.usage_trend_score !== null ? <>{signed(p.usage_trend_score, 2)} score</> : undefined}
      />
    </StatStrip>
  );
}

/** The signals behind the profile's labels: role, trend, production against the role, and the ratings. */
export function UnderlyingValue({ d }: { d: Detail }) {
  const p = d.player;
  const score = d.score!;
  return (
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
        <span className="k">Opportunity rating</span>
        <span><Rating rating={ratingFor(p, "opportunity")} kind="opportunity" /> <span className="faint">basis: {BASIS_LABEL[p.opportunity_basis ?? "unavailable"] ?? p.opportunity_basis}</span></span>
        <span className="k">Production rating</span><Rating rating={ratingFor(p, "production")} kind="production" />
        <span className="k">Usage trend score</span><span>{signed(p.usage_trend_score, 2)} <span className="faint">(−1…+1)</span></span>
        <span className="k">PPR / game</span><span>{num(p.fp_per_game)}</span>
        <span className="k">Expected PPR / game</span><span>{num(p.xfp_per_game)} <span className="faint">(nflverse xFP)</span></span>
        <span className="k">Actual − expected</span><span>{signed(p.fp_minus_xfp)}</span>
        <span className="k">Value rating</span><Rating rating={ratingFor(p, "value")} kind="value" />
      </div>
      <ul className={styles.reasons}>{score.reasons.map((r, i) => <li key={i}>{tidyReason(r)}</li>)}</ul>
      <p className={styles.note}>
        Opportunity basis: {BASIS_LABEL[score.components.opportunity_basis] ?? score.components.opportunity_basis}, weeks {score.components.recent_weeks.join(", ")}. {score.components.value_formula}.
      </p>
      <p className={styles.noteTight}>{RATING_CAVEAT} Opportunity ratings with different bases are ranked in separate pools.</p>
    </Section>
  );
}

/** The recent-vs-baseline table that produced the usage trend label. */
export function UsageTrendTable({ d }: { d: Detail }) {
  const score = d.score!;
  return (
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
  );
}

/** Next step from a profile: the player's position in the Rankings (or the kicker / D/ST outlook), where the peers are. */
function RankingsLink({ position }: { position: string | null }) {
  const href = position && SKILL.includes(position) ? `/rankings?pos=${position}` : position === "K" ? "/kickers" : position === "D/ST" ? "/defenses" : null;
  if (!href) return null;
  const text = position === "K" ? "Kicker outlook" : position === "D/ST" ? "D/ST outlook" : `${position} rankings`;
  return <Link className={styles.nextLink} href={href}>{text}</Link>;
}
