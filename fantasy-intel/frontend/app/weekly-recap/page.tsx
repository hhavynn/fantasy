import Link from "@/components/LeagueLink";
import { BackendProblem } from "@/components/BackendProblem";
import { PlayerLink } from "@/components/PlayerLink";
import { BarChart } from "@/components/charts";
import { Badge, Delta, EmptyState, Method, Notice, PageHeader, Pos, Section, Stat, StatStrip, Tabs } from "@/components/ui";
import { api } from "@/lib/api";
import { cx } from "@/lib/cx";
import { date, num, pct } from "@/lib/format";
import { providerName } from "@/lib/labels";
import { getLeagueProfile } from "@/lib/league-profile.server";
import { plural } from "@/lib/plural";
import {
  backendPath, joinNames, MOVE_TYPE_LABEL, parseRecapQuery, pointsText, recapHref, recordText, scoreBars, sideResult,
  STATUS_LABEL, tradeLines, weekLabel,
  type RecapQuery, type WeeklyRecap, type WrAward, type WrGame, type WrMatchup, type WrMove, type WrPlayer,
  type WrTeamScore,
} from "@/lib/weekly-recap";
import styles from "./weekly-recap.module.css";

export const metadata = { title: "Weekly recap" };

// Weekly League Recap: one completed matchup period's awards, scoreboard, standings after the week,
// standout player scores and league activity. Completed matchups only (ESPN winner home, away or tie);
// no projections, no win probabilities. Every number comes from the backend.

const RESULT_TONE = { W: "good", L: "bad", T: "warn" } as const;
const RESULT_WORD = { W: "Won", L: "Lost", T: "Tied" } as const;
const STATUS_TONE: Record<string, string> = { STARTER: "info", BENCH: "warn", IR: "", UNROSTERED: "", MIXED: "" };

export default async function WeeklyRecapPage({ searchParams }: PageProps<"/weekly-recap">) {
  const q = parseRecapQuery(await searchParams);
  const [res, profile] = await Promise.all([api<WeeklyRecap>(backendPath(q)), getLeagueProfile()]);
  const labels: L = { prov: providerName(profile.provider), espn: profile.providerProjections };
  if (!res.ok) {
    return (
      <div className="stack">
        <PageHeader title="Weekly recap" />
        <BackendProblem error={res.error} kind={res.kind} status={res.status} />
        {res.status === 404 && <p><Link href={recapHref({ ...q, week: null })}>Show the latest completed week</Link></p>}
      </div>
    );
  }
  return <Body d={res.data} q={q} L={labels} />;
}

/** The league's provider ("ESPN" or "Sleeper") and whether it publishes projections. */
type L = { prov: string; espn: boolean };

function Body({ d, q, L }: { d: WeeklyRecap; q: RecapQuery; L: L }) {
  if (d.selected_week === null || !d.awards) {
    return (
      <div className="stack">
        <PageHeader title="Weekly recap" meta={<>{d.league_name} {d.season}</>} />
        <Section>
          <EmptyState title="No completed week yet">
            The recap starts once {L.prov} marks a matchup final (a winner, or a tie). Live scores from a week in
            progress never count.
          </EmptyState>
        </Section>
      </div>
    );
  }
  const week = d.selected_week;
  const label = weekLabel(d.scoring_periods, week);
  const view = { ...q, week };
  const pending = d.notes.pending_matchups ?? 0;
  return (
    <div className="stack">
      <PageHeader
        eyebrow="League"
        title={`${label} recap`}
        meta={<>
          {d.league_name} {d.season} · <b>{d.matchups.length}</b> final {plural(d.matchups.length, "matchup")}
          {pending > 0 && <> · {pending} not final yet</>}
          {" "}· results only, no projections
        </>}
      >
        <Tabs
          label="Week"
          current={recapHref(view)}
          items={d.available_periods.map((p) => ({ href: recapHref(view, { week: p.matchup_period }), label: weekLabel(p.weeks, p.matchup_period) }))}
        />
      </PageHeader>

      {pending > 0 && (
        <Notice tone="caution" title="This week is not complete">
          {pending} {plural(pending, "matchup")} {pending === 1 ? "has" : "have"} no {L.prov} winner yet. Awards and standings
          use the {d.matchups.length} final {plural(d.matchups.length, "matchup")} only.
        </Notice>
      )}

      <Awards d={d} L={L} />

      <div className={cx("grid grid-2", styles.pair)}>
        <Scoreboard matchups={d.matchups} />
        <ScoreChart d={d} />
      </div>

      <Standings d={d} />

      <div className={cx("grid grid-2", styles.pair)}>
        <Players d={d} q={view} L={L} />
        <Activity d={d} q={view} L={L} />
      </div>

      <MethodNotes d={d} L={L} />
    </div>
  );
}

// ---------------------------------------------------------------------- awards --

function names(winners: { name: string }[]): string {
  return joinNames(winners.map((w) => w.name));
}

function TeamAward({ award }: { award: WrAward<WrTeamScore> }) {
  const w = award.winners;
  return (
    <Stat
      label={award.award_name}
      value={w.length ? pointsText(w[0].points) : "—"}
      note={w.length ? <>{names(w)}{award.is_tied && " (shared)"}</> : "No final scores"}
    />
  );
}

function GameAward({ award }: { award: WrAward<WrGame> }) {
  const w = award.winners;
  const first = w[0];
  return (
    <Stat
      label={award.award_name}
      value={first ? (first.is_tie && w.length === 1 ? "Tie" : `${pointsText(first.margin)}`) : "—"}
      note={first ? w.map((g, i) => (
        <span key={g.matchup_id} className={styles.awardLine}>
          {i > 0 && "; "}{g.winner_name} {g.is_tie ? "tied" : "beat"} {g.loser_name}, {g.score_summary}
        </span>
      )) : "No decided matchups"}
    />
  );
}

function Awards({ d, L }: { d: WeeklyRecap; L: L }) {
  const a = d.awards!;
  return (
    <div>
      <StatStrip>
        <TeamAward award={a.highest_score} />
        <GameAward award={a.biggest_blowout} />
        <GameAward award={a.closest_matchup} />
        <TeamAward award={a.highest_scoring_loser} />
      </StatStrip>
      <p className={styles.footnote}>
        Margins and scores are matchup totals in points. No biggest-upset award: {L.espn
          ? "ESPN projections in this database were synced after the games, so there are no genuine pregame expectations for past weeks."
          : "no pregame projections are stored for past weeks, so there are no genuine pregame expectations."}
      </p>
    </div>
  );
}

// ------------------------------------------------------------------ scoreboard --

function Side({ m, side }: { m: WrMatchup; side: "home" | "away" }) {
  const t = side === "home" ? m.home_team : m.away_team;
  const r = sideResult(m, side);
  return (
    <div className={cx(styles.side, r === "W" && styles.won)}>
      <Badge value={r} tone={RESULT_TONE[r]} title={RESULT_WORD[r]} />
      <span className={styles.team} title={t.name}>{t.name}</span>
      <span className={styles.points}>{pointsText(t.points)}</span>
    </div>
  );
}

function Scoreboard({ matchups }: { matchups: WrMatchup[] }) {
  return (
    <Section title="Scoreboard">
      <ul className={styles.board}>
        {matchups.map((m) => (
          <li key={m.matchup_id} className={styles.game}>
            <Side m={m} side="home" />
            <Side m={m} side="away" />
            <p className={styles.gameMeta}>
              {m.is_tie ? "Tie" : m.margin === null ? <span className="missing" title="A score is missing">Margin unknown</span> : <>Margin {pointsText(m.margin)}</>}
              {m.playoff_tier && <> · <Badge value={m.playoff_tier} /></>}
            </p>
          </li>
        ))}
      </ul>
    </Section>
  );
}

function ScoreChart({ d }: { d: WeeklyRecap }) {
  const top = new Set(d.awards!.highest_score.winners.map((w) => w.team_id));
  return (
    <Section title="Team scores" footnote="Highlighted: the week's top score. Bars are matchup-period totals.">
      <BarChart
        orientation="horizontal"
        data={scoreBars(d.matchups, top)}
        valueFormat={(v) => v.toFixed(2)}
        ariaLabel={`Team scores, ${weekLabel(d.scoring_periods, d.selected_week!)}`}
      />
    </Section>
  );
}

// ------------------------------------------------------------------- standings --

function periodName(d: WeeklyRecap, period: number): string {
  return weekLabel(d.available_periods.find((p) => p.matchup_period === period)?.weeks, period);
}

function Standings({ d }: { d: WeeklyRecap }) {
  if (d.standings_through === null || d.standings.length === 0) {
    return (
      <Section title="Standings">
        <EmptyState title="No regular-season results yet">Standings appear after the first final regular-season week.</EmptyState>
      </Section>
    );
  }
  const playoff = d.standings_through !== d.selected_week;
  return (
    <Section
      title={`Standings after ${periodName(d, d.standings_through).toLowerCase()}`}
      flush
      footnote={playoff ? "Playoff games do not change regular-season standings; this is the final regular-season table." : undefined}
    >
      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              <th>Team</th><th className="num">Rank</th><th className="num" title="Change in rank since the previous week">Move</th>
              <th>Record</th><th className="num">Win %</th><th>This week</th><th className="num">Points for</th><th className="num">Points against</th>
            </tr>
          </thead>
          <tbody>
            {d.standings.map((s) => (
              <tr key={s.team_id}>
                <td><span className={styles.teamCell} title={s.team_name}>{s.team_name}</span></td>
                <td className="num">{s.rank}</td>
                <td className="num">
                  {s.rank_change === null ? <span className="missing" title="No earlier week to compare">—</span> : <Delta value={s.rank_change} digits={0} />}
                </td>
                <td>{recordText(s)}</td>
                <td className="num">{pct(s.win_pct, 0, "No games yet")}</td>
                <td>{s.weekly_result
                  ? <Badge value={s.weekly_result} tone={RESULT_TONE[s.weekly_result]} title={RESULT_WORD[s.weekly_result]} />
                  : <span className="missing" title="No final matchup this week">—</span>}</td>
                <td className="num">{num(s.points_for, 2, "A counted matchup has no score")}</td>
                <td className="num">{num(s.points_against, 2, "A counted matchup has no score")}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </Section>
  );
}

// --------------------------------------------------------------------- players --

function PlayerTable({ rows, empty }: { rows: WrPlayer[]; empty: string }) {
  if (rows.length === 0) return <EmptyState title={empty} />;
  return (
    <div className="table-wrap">
      <table>
        <thead><tr><th>Player</th><th>Fantasy team</th><th>Lineup</th><th className="num">Points</th></tr></thead>
        <tbody>
          {rows.map((p) => (
            <tr key={p.espn_player_id}>
              <td>
                <PlayerLink id={p.espn_player_id} name={p.full_name} /> <Pos value={p.position} />
                {p.pro_team && <span className="faint"> {p.pro_team}</span>}
              </td>
              <td>{p.fantasy_team_name
                ? <span className={styles.teamCell} title={p.fantasy_team_name}>{p.fantasy_team_name}</span>
                : <span className="faint">{p.lineup_status === "MIXED" ? "More than one" : "None"}</span>}</td>
              <td><Badge value={STATUS_LABEL[p.lineup_status]} tone={STATUS_TONE[p.lineup_status]} /></td>
              <td className="num">{pointsText(p.points)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function Players({ d, q, L }: { d: WeeklyRecap; q: RecapQuery; L: L }) {
  const views = {
    top: { label: "Top scorers", rows: d.standout_players, empty: "No player points recorded for this week" },
    bench: { label: "Best on a bench", rows: d.bench_standouts, empty: "No benched player scored" },
    unrostered: { label: "Unrostered", rows: d.unrostered_standouts, empty: "No unrostered player scored" },
  } as const;
  const v = views[q.players];
  const missing = d.notes.rostered_without_points ?? 0;
  const partial = d.notes.partial_period_players ?? 0;
  return (
    <Section
      title="Standout players"
      flush
      footnote={<>
        League-scored {L.prov} points.
        {missing > 0 && <> {missing} rostered {plural(missing, "player has", "players have")} no recorded points for this week and {missing === 1 ? "is" : "are"} left out, not counted as zero.</>}
        {partial > 0 && <> {partial} {plural(partial, "player")} with points for only part of the period {partial === 1 ? "is" : "are"} left out.</>}
      </>}
    >
      <Tabs
        className={styles.sectionTabs}
        label="Player list"
        current={recapHref(q)}
        items={(Object.keys(views) as (keyof typeof views)[]).map((k) => ({ href: recapHref(q, { players: k }), label: views[k].label }))}
      />
      <PlayerTable rows={v.rows} empty={v.empty} />
    </Section>
  );
}

// -------------------------------------------------------------------- activity --

function MoveTable({ rows, showBid, prov }: { rows: WrMove[]; showBid: boolean; prov: string }) {
  if (rows.length === 0) return <EmptyState title="Nothing recorded this week" />;
  return (
    <div className="table-wrap">
      <table>
        <thead>
          <tr><th>Player</th><th>Team</th><th>How</th><th>Date</th>{showBid && <th className="num" title="Waiver bid (FAAB)">Bid</th>}</tr>
        </thead>
        <tbody>
          {rows.map((m, i) => (
            <tr key={`${m.player_id}-${m.team_id}-${i}`}>
              <td><PlayerLink id={m.player_id} name={m.player_name} /> <Pos value={m.position} /></td>
              <td>{m.team_name ? <span className={styles.teamCell} title={m.team_name}>{m.team_name}</span> : <span className="missing" title="Team not recorded">—</span>}</td>
              <td>{MOVE_TYPE_LABEL[m.type] ?? m.type}</td>
              <td>{date(m.processed_at, prov)}</td>
              {showBid && <td className="num">{m.bid_amount === null || m.bid_amount === undefined ? "" : `$${m.bid_amount.toFixed(0)}`}</td>}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function Activity({ d, q, L }: { d: WeeklyRecap; q: RecapQuery; L: L }) {
  const a = d.league_activity;
  if (!a) {
    return (
      <Section title="League activity">
        <EmptyState title="Activity unavailable">The scoring weeks of this matchup period are unknown, so moves cannot be matched to it.</EmptyState>
      </Section>
    );
  }
  const items = [
    { href: recapHref(q, { moves: "adds" }), label: "Adds", count: a.adds.length },
    { href: recapHref(q, { moves: "drops" }), label: "Drops", count: a.drops.length },
    { href: recapHref(q, { moves: "trades" }), label: "Trades", count: a.trades.length },
  ];
  return (
    <Section
      title="League activity"
      flush
      actions={<span className="faint">{a.total_moves} {plural(a.total_moves, "move")}</span>}
      footnote={<>
        Executed moves only; failed and cancelled claims are left out.
        {a.unplaced_trades > 0 && <> {a.unplaced_trades} {plural(a.unplaced_trades, "trade")} from the activity feed could not be placed in a week and {a.unplaced_trades === 1 ? "is" : "are"} not shown.</>}
      </>}
    >
      <Tabs className={styles.sectionTabs} label="Activity type" current={recapHref(q)} items={items} />
      {q.moves === "adds" && <MoveTable rows={a.adds} showBid prov={L.prov} />}
      {q.moves === "drops" && <MoveTable rows={a.drops} showBid={false} prov={L.prov} />}
      {q.moves === "trades" && (a.trades.length === 0 ? <EmptyState title="No trades this week" /> : (
        <ul className={styles.trades}>
          {a.trades.map((t) => (
            <li key={t.trade_id}>
              <p className={styles.tradeHead}>
                {date(t.processed_at, L.prov)}
                {t.placed_by === "activity_date" && <span className="faint" title="Seen only in the league activity feed; placed in this week by its date"> · from the activity feed</span>}
              </p>
              <ul className={styles.tradeSides}>
                {tradeLines(t).map((line, i) => <li key={i}>{line}</li>)}
              </ul>
            </li>
          ))}
        </ul>
      ))}
    </Section>
  );
}

// ---------------------------------------------------------------------- method --

function MethodNotes({ d, L }: { d: WeeklyRecap; L: L }) {
  const check = d.notes.espn_record_check;
  const snaps = d.notes.lineup_snapshot_weeks ?? [];
  const missingLineups = d.scoring_periods.filter((w) => !snaps.includes(w));
  return (
    <Method title="How the recap is built">
      <ul className="reasons">
        <li>Only matchups {L.prov} marks final (a winner, or a tie) count. A week in progress shows only its final games, and live points never count.</li>
        <li>{d.rules.awards}</li>
        <li>{d.rules.standings}</li>
        <li>{d.rules.players}</li>
        <li>{d.rules.trades}</li>
        <li>Free-agent pickups and releases carry the time {L.prov} recorded the request (they execute immediately); waiver claims and trades carry the processing time.</li>
        {missingLineups.length > 0 && <li>No stored roster snapshot for week {missingLineups.join(", ")}: players from {missingLineups.length === 1 ? "that week" : "those weeks"} show as unrostered.</li>}
        {(d.notes.byes ?? 0) > 0 && <li>{d.notes.byes} one-team {plural(d.notes.byes ?? 0, "row")} (a bye) {d.notes.byes === 1 ? "is" : "are"} left out of the scoreboard.</li>}
        {check?.compared && (check.mismatched_team_ids.length === 0
          ? <li>Check: the rebuilt records through {periodName(d, d.standings_through!).toLowerCase()} match {L.prov}&apos;s current team records.</li>
          : <li>Check: the rebuilt records differ from {L.prov}&apos;s current records for {check.mismatched_team_ids.length} {plural(check.mismatched_team_ids.length, "team")}; {L.prov} may count a result this database does not have.</li>)}
      </ul>
    </Method>
  );
}
