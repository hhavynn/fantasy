import Link from "@/components/LeagueLink";
import { useId, type ReactNode } from "react";
import { TeamLink } from "@/components/TeamLink";
import { Badge, EmptyState, Glossary, ProvenanceBadge, Section, SkeletonStatStrip, SkeletonTable } from "@/components/ui";
import { Missing, label, num, pct } from "@/lib/format";
import { isOursNumber, oursBlock, oursByWeek } from "@/lib/forward-view";
import { providerName } from "@/lib/labels";
import type { LeagueProfile } from "@/lib/league-profile";
import {
  WEEK_WINDOWS, profileHref, weekSpan, weekWindowLabel, windowRange, type ProfileState, type WeekRange, type WeekWindow,
} from "@/lib/player-tabs";
import { weekAxis } from "@/lib/player-usage";
import { plural } from "@/lib/plural";
import type { PlayerRange } from "@/lib/projection-ranges";
import { tidyReason } from "@/lib/reason-text";
import { knownWaiverScore, waiverScoreGap } from "@/lib/waivers";
import { signalsOf, type SignalKey } from "@/lib/signal-provenance";
import type { ForwardProjection } from "@/lib/types";
import { ForwardView } from "./ForwardView";
import { GameLog } from "./GameLog";
import { ContextSection, RouteSection, ScheduleLink } from "./PlayerContext";
import { UnderlyingValue, UsageTrendTable } from "./PlayerProfile";
import { LeaguePointsChart, UsageCharts, lastCompletedWeek } from "./UsageCharts";
import styles from "./players.module.css";
import type { Detail } from "./types";

// The panels of the player profile's tabs (#187). Each opens with the same summary block: one headline sentence,
// then the scoring, source and time window behind the tab, and the tab's own signal-provenance.v1 badge. Every
// number comes from the profile payload as the backend sent it; a missing one is said to be missing, never 0.

const SKILL = ["QB", "RB", "WR", "TE"];

type TabProps = { d: Detail; state: ProfileState; profile: LeagueProfile };

/** The provenance signals a tab's data comes from (the header's badge covers all of them). */
function signalsFor(d: Detail, keys: readonly SignalKey[]) {
  return signalsOf(d.provenance).filter((s) => (keys as readonly string[]).includes(s.signal));
}

const hasUsage = (d: Detail) => d.weekly.length > 0 || d.score !== null;

/** The weeks a usage chart or game log can show: NFL usage rows, else the weeks the provider scored. */
function recordedWeeks(d: Detail): number[] {
  return d.weekly.length > 0
    ? d.weekly.map((w) => w.week)
    : d.league_points.filter((r) => r.actual_points !== null).map((r) => r.scoring_period);
}

function TabSummary({ headline, scoring, source, window, signals }: {
  headline: ReactNode;
  scoring: ReactNode;
  source: ReactNode;
  window: ReactNode;
  /** The tab's provenance signals; omitted where the header's badge already covers exactly these. */
  signals?: ReturnType<typeof signalsFor>;
}) {
  const id = useId();
  return (
    <section className={styles.summary} aria-labelledby={id}>
      <h2 id={id} className={styles.summaryHeadline}>{headline}</h2>
      <dl className={styles.summaryFacts}>
        <div><dt>Scoring</dt><dd>{scoring}</dd></div>
        <div><dt>Source</dt><dd>{source}</dd></div>
        <div><dt>Window</dt><dd>{window}</dd></div>
      </dl>
      {signals !== undefined && (signals.length > 0
        ? <div className={styles.summaryBadge}><ProvenanceBadge signals={signals} /></div>
        : <p className={styles.noteTight}>The age of this tab&apos;s data is unknown: no source was reported for it.</p>)}
    </section>
  );
}

/** Week window chips for the Usage and Game log tabs. Plain links, so the window is in the URL and survives sharing. */
function WeekFilter({ state }: { state: ProfileState }) {
  const chip = (w: WeekWindow) => (w === null ? "Season" : `Last ${w}`);
  return (
    <div className="filters" role="group" aria-label="Weeks shown">
      <span className="filter-label" aria-hidden="true">Weeks</span>
      {WEEK_WINDOWS.map((w) => (
        <Link
          key={String(w)}
          className="chip"
          href={profileHref(state, { weeks: w })}
          scroll={false}
          aria-current={w === state.weeks ? "true" : undefined}
          aria-label={weekWindowLabel(w)}
        >
          {chip(w)}
        </Link>
      ))}
    </div>
  );
}

/** "Last 5 weeks: wk 4–8" or "Season: wk 1–8"; says so when no week is recorded yet. */
function windowText(d: Detail, state: ProfileState, range: WeekRange | null): ReactNode {
  if (range) return <>{weekWindowLabel(state.weeks)}: wk {range.from}–{range.to}</>;
  const axis = weekAxis(recordedWeeks(d), lastCompletedWeek(d));
  return axis.length ? <>Season: wk 1–{axis[axis.length - 1]}</> : "Season (no weeks recorded yet)";
}

// ---------- Overview ----------

function overviewHeadline(d: Detail, provider: string): ReactNode {
  const p = d.player;
  const parts: string[] = [];
  if (p.opportunity_tier) parts.push(`${label(p.opportunity_tier)} role`);
  if (p.trend_label) parts.push(`usage ${label(p.trend_label).toLowerCase()}`);
  if (p.market_signal) parts.push(`market signal ${label(p.market_signal).toLowerCase()}`);
  if (parts.length > 0) {
    const text = parts.join(", ");
    return text.charAt(0).toUpperCase() + text.slice(1);
  }
  if (p.season_avg_points !== null && p.season_avg_points !== undefined) {
    return <>{num(p.season_avg_points, 1)} {provider} league points per game this season</>;
  }
  return "No usage evaluation or league points for this player yet";
}

export function OverviewTab({ d, profile }: TabProps) {
  const provider = providerName(profile.provider);
  const usage = hasUsage(d);
  const recent = d.score?.components.recent_weeks ?? [];
  return (
    <>
      <TabSummary
        headline={overviewHeadline(d, provider)}
        scoring={usage
          ? <>League points in your league&apos;s {provider} scoring; PPR and xFP in full PPR (nflverse)</>
          : <>League points in your league&apos;s {provider} scoring</>}
        source={d.score ? `${provider} league data, nflverse usage, Fantasy Intel analysis` : usage ? `${provider} league data, nflverse usage` : `${provider} league data`}
        window={recent.length > 0 ? <>Season to date; the usage trend compares {weekSpan(recent)} with earlier games</> : "Season to date"}
      />
      {usage && (
        <p className={styles.explainer}>
          <b>PPR / game</b> is what the player scored. <b>Expected PPR</b> (nflverse xFP) is what an average player would score with the same targets,
          carries and field position, so the gap between them separates role from efficiency.
        </p>
      )}

      {d.waiver && (
        <Section title="Waiver view" actions={<TeamLink href="/waivers">All waivers</TeamLink>}>
          <div className={styles.badgeRow}>
            <Badge value={d.waiver.recommendation} />
            <span>Waiver score {knownWaiverScore(d.waiver) === null ? <Missing why={waiverScoreGap(d.waiver)} /> : d.waiver.waiver_score.toFixed(2)}</span>
          </div>
          <ul className={styles.reasons}>{d.waiver.reasons.map((r, i) => <li key={i}>{tidyReason(r)}</li>)}</ul>
        </Section>
      )}

      <LeaguePointsChart d={d} asOf={d.forward_projection?.summary.as_of_week ?? null} profile={profile} trend />
      {d.score && <UnderlyingValue d={d} />}
      <Glossary terms={["PPR/g", "xFP", "FPOE", "Value rating", "Waiver score", "—"]} providerProjections={profile.providerProjections} />
    </>
  );
}

// ---------- Usage ----------

function usageHeadline(d: Detail, range: WeekRange | null): ReactNode {
  const snap = d.score?.components.trends.find((t) => t.metric === "offense_pct");
  if (!range && snap && snap.recent_avg !== null) {
    return (
      <>Snap share {pct(snap.recent_avg)} in {weekSpan(snap.recent_weeks) ?? "recent games"}
        {snap.baseline_avg !== null ? <>, against {pct(snap.baseline_avg)} in {weekSpan(snap.baseline_weeks) ?? "earlier games"}</> : null}</>
    );
  }
  const rows = d.weekly.filter((w) => !range || (w.week >= range.from && w.week <= range.to));
  const latest = [...rows].reverse().find((w) => w.offense_pct !== null);
  if (latest) return <>Snap share {pct(latest.offense_pct)} in week {latest.week}, the latest game in this window</>;
  if (d.weekly.length > 0) return "No snap share recorded in these weeks";
  return "No NFL usage data for this player";
}

export function UsageTab({ d, state, profile }: TabProps) {
  const provider = providerName(profile.provider);
  const range = windowRange(recordedWeeks(d), lastCompletedWeek(d), state.weeks);
  const usage = d.weekly.length > 0;
  return (
    <>
      <TabSummary
        headline={usageHeadline(d, range)}
        scoring={usage
          ? "Shares of the team's snaps, carries and targets; points in full PPR (nflverse), not your league's scoring"
          : <>League points in your league&apos;s {provider} scoring</>}
        source={usage
          ? <>nflverse weekly usage{d.route_data ? <>; routes from {d.route_data.providers.join(", ")}</> : null}</>
          : `${provider} weekly points (no NFL usage is stored for this player)`}
        window={windowText(d, state, range)}
        signals={signalsFor(d, usage ? ["usage"] : ["usage", "league"])}
      />
      <WeekFilter state={state} />
      <UsageCharts d={d} profile={profile} range={range} />
      {d.score && <UsageTrendTable d={d} />}
      {d.route_data && <RouteSection r={d.route_data} />}
      <Glossary terms={["Snap share", "Target share", "Backfield share", "Air-yard share", "aDOT", "xFP", "—"]} providerProjections={profile.providerProjections} />
    </>
  );
}

// ---------- Game log ----------

function gameLogHeadline(d: Detail, range: WeekRange | null, provider: string): string {
  const all = recordedWeeks(d);
  const shown = range ? all.filter((w) => w >= range.from && w <= range.to) : all;
  const noun = d.weekly.length > 0 ? ["game", "games"] as const : ["week with league points", "weeks with league points"] as const;
  if (all.length === 0) return d.weekly.length > 0 ? "No games recorded this season" : `No ${provider} weekly points recorded this season`;
  const total = `${all.length} ${plural(all.length, noun[0], noun[1])} recorded this season`;
  return range ? `${total}; ${shown.length} in weeks ${range.from}–${range.to}` : total;
}

export function GameLogTab({ d, state, profile }: TabProps) {
  const provider = providerName(profile.provider);
  const range = windowRange(recordedWeeks(d), lastCompletedWeek(d), state.weeks);
  const usage = d.weekly.length > 0;
  return (
    <>
      <TabSummary
        headline={gameLogHeadline(d, range, provider)}
        scoring={usage
          ? <>League pts in your league&apos;s {provider} scoring; PPR and xFP in full PPR (nflverse)</>
          : <>League points in your league&apos;s {provider} scoring</>}
        source={usage ? `nflverse weekly stats and ${provider} weekly points` : `${provider} weekly points`}
        window={windowText(d, state, range)}
        signals={signalsFor(d, ["usage", "league"])}
      />
      <WeekFilter state={state} />
      <GameLog
        d={d}
        all={state.cols === "all"}
        colsHref={{ core: profileHref(state, { cols: "core" }), all: profileHref(state, { cols: "all" }) }}
        range={range}
        profile={profile}
      />
    </>
  );
}

// ---------- Matchups ----------

function matchupHeadline(d: Detail): ReactNode {
  const f = d.forward_projection;
  const next = f?.weeks.find((w) => w.week === f.summary.as_of_week);
  const ctx = d.context?.labels;
  if (next?.on_bye) return `On bye in week ${next.week}`;
  if (next?.opponent) {
    // The matchup label describes the team context's next opponent; it is only quoted when that is this opponent.
    const labelled = ctx && ctx.matchup && ctx.matchup !== "BYE" && d.team_context?.next_opponent === next.opponent;
    return <>Week {next.week}: {next.is_home ? "vs" : "@"} {next.opponent}{labelled ? <>, a {label(ctx.matchup).toLowerCase()} matchup by what this defense allows</> : null}</>;
  }
  if (d.team_context?.next_opponent) return `Next opponent: ${d.team_context.next_opponent}`;
  return "No upcoming opponent is known";
}

/** Who the player faces in each remaining fantasy week; byes come from the NFL schedule. */
function RemainingSchedule({ f }: { f: ForwardProjection | null }) {
  if (!f || f.weeks.length === 0) {
    return (
      <Section title="Remaining schedule">
        <EmptyState title="Remaining schedule unavailable">The league&apos;s remaining weeks or this player&apos;s NFL schedule are not known.</EmptyState>
      </Section>
    );
  }
  return (
    <Section title="Remaining schedule" subtitle={`Weeks ${f.summary.as_of_week}–${f.summary.final_week}`} flush>
      <div className="table-wrap">
        <table>
          <thead><tr><th scope="col">Week</th><th scope="col">Opponent</th><th scope="col">Phase</th></tr></thead>
          <tbody>
            {f.weeks.map((w) => (
              <tr key={w.week}>
                <td>{w.week}</td>
                <td>
                  {w.on_bye ? <Badge value="BYE" tone="warn" />
                    : w.opponent ? `${w.is_home ? "vs" : "@"} ${w.opponent}`
                      : <span className="missing" title={w.on_bye === null ? "Schedule unknown" : "NFL schedule unavailable"}>—</span>}
                </td>
                <td className={styles.phase}>{w.phase === "playoff" ? "Fantasy playoffs" : w.phase === "regular" ? "Regular season" : <span className="missing" title="Fantasy playoff split unknown">—</span>}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </Section>
  );
}

export function MatchupsTab({ d, profile }: TabProps) {
  const e = d.context?.evidence;
  const pos = d.player.position;
  return (
    <>
      <TabSummary
        headline={matchupHeadline(d)}
        scoring="Descriptive, no points: what each defense has allowed (EPA, success rate, target shares)"
        source="nflverse play-by-play and the NFL schedule"
        window={e ? <>Player: last {e.games_recent} {plural(e.games_recent, "game")} ({weekSpan(e.recent_weeks) ?? "no weeks"}); defenses: season to date</> : "Season to date"}
        signals={signalsFor(d, ["usage", "league"])}
      />
      {d.context
        ? <ContextSection d={d} />
        : (
          <Section title="Player context">
            <EmptyState title="No role or matchup context">
              No role, environment or matchup context is computed for this player. It needs NFL usage data and a skill position.
            </EmptyState>
          </Section>
        )}
      <RemainingSchedule f={d.forward_projection} />
      {SKILL.includes(pos ?? "") && <ScheduleLink id={d.player.espn_player_id} />}
      <Glossary terms={["EPA", "Snap share", "Target share", "Bye", "—"]} providerProjections={profile.providerProjections} />
    </>
  );
}

// ---------- Outlook ----------

export type OutlookData = { f: ForwardProjection | null; range: PlayerRange | null };

function outlookHeadline(f: ForwardProjection, espnProjections: boolean): ReactNode {
  const wk = f.summary.as_of_week;
  if (f.weeks.find((w) => w.week === wk)?.on_bye === true) return `On bye in week ${wk}`;
  const ours = oursBlock(f);
  const oursWeek = ours?.available ? oursByWeek(ours).get(wk) : undefined;
  if (oursWeek && isOursNumber(oursWeek)) return <>{num(oursWeek.value, 1)} projected points in week {wk} (Ours)</>;
  if (!espnProjections) return `No Fantasy Intel projection for week ${wk} yet`;
  return f.summary.current_week_projection !== null
    ? <>{num(f.summary.current_week_projection, 1)} projected points in week {wk} (ESPN)</>
    : `No ESPN projection for week ${wk}`;
}

/** The Outlook tab. `load` resolves the forward view (with Ours when the switch asks for it); it streams in. */
export async function OutlookTab({ d, profile, load }: Omit<TabProps, "state"> & { load: Promise<OutlookData> }) {
  const { f, range } = await load;
  const espn = profile.providerProjections;
  const oursOn = f ? oursBlock(f)?.available === true : false;
  const signals = signalsFor(d, ["projections", "injuries"]);
  if (!f) {
    return (
      <>
        <TabSummary
          headline="No forward projection for this player"
          scoring="Projected points in your league's scoring"
          source={espn ? "ESPN projections" : "Fantasy Intel (Ours)"}
          window="Remaining weeks unknown"
          signals={signals}
        />
        <Section title="Forward view">
          <EmptyState title="No forward projection">The league&apos;s remaining weeks are unknown, or no projection is stored for this player. Nothing is shown as 0.</EmptyState>
        </Section>
      </>
    );
  }
  return (
    <>
      <TabSummary
        headline={outlookHeadline(f, espn)}
        scoring="Projected points in your league's scoring"
        source={!espn ? "Fantasy Intel (Ours); this league's provider publishes no projections" : oursOn ? "ESPN, shown unmodified, beside Fantasy Intel (Ours)" : "ESPN, shown unmodified"}
        window={<>Weeks {f.summary.as_of_week}–{f.summary.final_week}</>}
        signals={signals}
      />
      <ForwardView f={f} range={range} espnProjections={espn} />
      <Glossary terms={["Projected points", "ROS", "Ours", "Bye", "—"]} providerProjections={espn} />
    </>
  );
}

/** Same footprint as the Outlook's summary and forward table while it streams in. */
export function OutlookSkeleton() {
  return (
    <>
      <SkeletonStatStrip />
      <SkeletonTable rows={6} cols={5} />
    </>
  );
}
