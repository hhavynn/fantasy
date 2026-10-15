import Link from "next/link";
import { BackendProblem } from "@/components/BackendProblem";
import { MovesTable } from "@/components/MovesTable";
import { PlayerLink } from "@/components/PlayerLink";
import {
  Badge, EmptyState, Method, Notice, PageHeader, Pos, Score, Section, SortableTh, Stat, StatStrip, TrendArrow,
} from "@/components/ui";
import { api } from "@/lib/api";
import { buildActionItems, type StartSitInput, type WaiverInput } from "@/lib/action-items";
import { num } from "@/lib/format";
import { ordinal } from "@/lib/matchup";
import type { Matchup } from "@/lib/matchup-types";
import { parseSort, sortRows, type SortState } from "@/lib/sort";
import type { League, Move, Need, RosterPlayer, SyncInfo, Team } from "@/lib/types";
import { ActionItems } from "./ActionItems";
import { MatchupCard } from "./MatchupCard";
import styles from "./home.module.css";

// The template does not apply to a page in the layout's own segment, so the full title is given.
export const metadata = { title: { absolute: "Overview · Fantasy Intel" } };

type Overview = {
  league: League; my_team: Team | null; standings: Team[]; roster: RosterPlayer[]; injuries: RosterPlayer[];
  needs: Need[]; recent_transactions: Move[]; sync: SyncInfo; nfl_latest_week: number | null;
};

const STANDINGS_SORTS = ["rank", "record", "pf", "pa"] as const;
const STANDINGS_DEFAULT: SortState = { key: "rank", dir: "asc" };

export default async function OverviewPage({ searchParams }: PageProps<"/">) {
  const sp = await searchParams;
  // Each section has its own source; one failing endpoint degrades only its own section.
  const [res, matchup, startSit, waivers] = await Promise.all([
    api<Overview>("/api/overview"),
    api<Matchup>("/api/matchup"),
    api<StartSitInput>("/api/start-sit"),
    api<{ players: WaiverInput[]; total: number }>("/api/waivers?view=BEST&limit=3"),
  ]);
  const actions = buildActionItems({
    roster: res.ok ? res.data.roster : null,
    matchup: matchup.ok ? matchup.data : null,
    startSit: startSit.ok ? startSit.data : null,
    waivers: waivers.ok ? waivers.data : null,
  });
  const week = matchup.ok ? matchup.data.league.current_week : res.ok ? res.data.league.current_week : null;

  if (!res.ok) {
    if (!matchup.ok) {
      return (
        <div className="stack">
          <PageHeader title="Overview" />
          <BackendProblem error={res.error} kind={res.kind} status={res.status} />
        </div>
      );
    }
    return (
      <div className="stack">
        <PageHeader title="Overview" />
        <Notice tone="caution" title="Team overview unavailable">{res.error} The roster, needs and standings below are missing; the rest of the page still loaded.</Notice>
        <div className={styles.band}>
          <MatchupCard result={matchup} />
          <ActionItems list={actions} week={week} />
        </div>
      </div>
    );
  }
  const o = res.data;
  const me = o.my_team;
  const rank = me ? o.standings.findIndex((t) => t.team_id === me.team_id) + 1 : null;
  const n = o.standings.length;
  const rankBy = (k: "points_for" | "points_against") =>
    me ? [...o.standings].sort((a, b) => (b[k] ?? 0) - (a[k] ?? 0)).findIndex((t) => t.team_id === me.team_id) + 1 : null;
  const pfRank = rankBy("points_for");
  const paRank = rankBy("points_against");
  const scoring = o.league.reception_points === 1 ? "PPR" : o.league.reception_points ? `${o.league.reception_points} PPR` : "Scoring ?";

  return (
    <div className="stack">
      <PageHeader
        title={me ? me.name : "Your team"}
        meta={`${o.league.name} · ${o.league.season} · week ${o.league.current_week} · ${o.league.size} teams · ${scoring}${o.league.is_faab ? ` · FAAB $${o.league.faab_budget}` : " · waiver priority"}`}
      />

      {!me && (
        <Notice tone="caution">
          Your team wasn&apos;t identified from SWID. Set <code>ESPN_MY_TEAM_ID</code> in <code>.env</code> and re-sync.
        </Notice>
      )}

      {me && (
        <StatStrip>
          <Stat label="Record" value={`${me.wins}–${me.losses}${me.ties ? `–${me.ties}` : ""}`} note={rank ? `${ordinal(rank)} of ${n}` : undefined} />
          <Stat label="Points for" value={num(me.points_for, 1)} note={pfRank ? `${ordinal(pfRank)} most in league` : undefined} />
          <Stat label="Points against" value={num(me.points_against, 1)} note={paRank ? `${ordinal(paRank)} most in league` : undefined} />
          <Stat
            label={o.league.is_faab ? "FAAB left" : "Waiver priority"}
            value={o.league.is_faab ? `$${(o.league.faab_budget ?? 0) - (me.faab_spent ?? 0)}` : `#${me.waiver_rank ?? "?"}`}
            note={o.league.is_faab ? `of $${o.league.faab_budget ?? "?"}` : `of ${n} teams`}
          />
          <Stat label="Injury designations" value={o.injuries.length} note="on your roster" />
        </StatStrip>
      )}

      <div className={styles.band}>
        <MatchupCard result={matchup} />
        <ActionItems list={actions} week={week} />
      </div>

      <div className="grid-main">
        <Section title="Current roster" flush>
          <RosterTable rows={o.roster} />
        </Section>

        <div className="stack">
          <Section title="Biggest roster needs" flush>
            <div className="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th>Pos</th><th>Need</th>
                    <th className="num" title="Availability-weighted bodies vs depth target">Avail</th>
                    <th className="num" title="Your starters' value at the position vs the league average starter">Value / avg</th>
                  </tr>
                </thead>
                <tbody>
                  {o.needs.filter((x) => x.starters_required > 0).map((x) => (
                    <tr key={x.position}>
                      <td><Pos value={x.position} /></td>
                      <td><Score value={x.need_score * 10} /></td>
                      <td className="num" title={`${x.starters_required} start${x.starters_required === 1 ? "" : "s"}${x.details.flex_slots_filled ? " incl. FLEX" : ""} · ${x.unavailable} out`}>
                        {avail(x.effective_available ?? x.healthy)} / {x.details.depth_target}
                      </td>
                      <td className="num">{num(x.starter_value)} <span className="faint">/ {num(x.details.league_avg_starter_value)}</span></td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </Section>
          <Method title="How need is calculated">
            <p>
              Need = 0.55·shortage of availability-weighted bodies + 0.45·gap between your starters&apos; value at the position (fixed and FLEX-assigned) and the league
              average. FLEX is one real slot filled by the best eligible player. Weights: available 1, questionable 0.75, doubtful 0.15, out/IR 0.
            </p>
          </Method>

          {o.injuries.length > 0 && (
            <Section title="Injuries on my roster" flush>
              <div className="table-wrap">
                <table>
                  <thead><tr><th>Player</th><th>ESPN</th><th>NFL report</th></tr></thead>
                  <tbody>
                    {o.injuries.map((p) => (
                      <tr key={p.espn_player_id}>
                        <td><PlayerLink id={p.espn_player_id} name={p.full_name} /> <Pos value={p.position} /></td>
                        <td><Badge value={p.injury_status} /></td>
                        <td>
                          {p.nfl_injury
                            ? <><Badge value={p.nfl_injury.report_status ?? "none"} /> <span className="faint">wk {p.nfl_injury.week}{p.nfl_injury.report_primary_injury ? ` · ${p.nfl_injury.report_primary_injury}` : ""}</span></>
                            : <span className="missing" title="No NFL injury report row">—</span>}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </Section>
          )}
        </div>
      </div>

      <div className={styles.pair}>
        <StandingsSection standings={o.standings} sp={sp} />
        <Section title="Recent league transactions" flush={o.recent_transactions.length > 0}>
          {o.recent_transactions.length === 0
            ? <EmptyState title="No transactions recorded">Adds, drops and trades appear here after the next ESPN sync.</EmptyState>
            : <MovesTable moves={o.recent_transactions} />}
        </Section>
      </div>
    </div>
  );
}

const gamesOf = (t: Team) => t.wins + t.losses + t.ties;
/** Win share for sorting only (a tie counts half); teams with no games yet sort last. */
const winShare = (t: Team) => (gamesOf(t) === 0 ? null : (t.wins + t.ties / 2) / gamesOf(t));

function StandingsSection({ standings, sp }: { standings: Team[]; sp: Record<string, string | string[] | undefined> }) {
  const sort = parseSort(sp, STANDINGS_SORTS, STANDINGS_DEFAULT);
  const ranked = standings.map((team, i) => ({ team, rank: i + 1 }));
  const rows = sortRows(ranked, ({ team, rank }) => {
    switch (sort.key) {
      case "record": return winShare(team);
      case "pf": return team.points_for;
      case "pa": return team.points_against;
      default: return rank;
    }
  }, sort.dir);
  return (
    <Section title="Standings" flush>
      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              <SortableTh sortKey="rank" label="#" align="right" defaultDir="asc" current={sort} searchParams={sp} title="League standing" />
              <th>Team</th>
              <SortableTh sortKey="record" label="W–L" defaultDir="desc" current={sort} searchParams={sp} title="Sorted by win share (a tie counts half)" />
              <SortableTh sortKey="pf" label="PF" align="right" current={sort} searchParams={sp} title="Points for" />
              <SortableTh sortKey="pa" label="PA" align="right" current={sort} searchParams={sp} title="Points against" />
            </tr>
          </thead>
          <tbody>
            {rows.map(({ team: t, rank }) => (
              <tr key={t.team_id} className={t.is_mine ? "mine" : ""}>
                <td className="num faint">{rank}</td>
                <td><Link href={`/managers/${t.team_id}`}>{t.name}</Link></td>
                <td>{t.wins}–{t.losses}{t.ties ? `–${t.ties}` : ""}</td>
                <td className="num">{num(t.points_for, 1)}</td>
                <td className="num">{num(t.points_against, 1)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </Section>
  );
}

function avail(v: number | null | undefined) {
  if (v === null || v === undefined) return <span className="missing" title="Not computed">—</span>;
  return Number.isInteger(v) ? String(v) : v.toFixed(1);
}

function RosterTable({ rows }: { rows: RosterPlayer[] }) {
  const groups: [string, RosterPlayer[]][] = [
    ["Starters", rows.filter((p) => p.is_starter)],
    ["Bench / IR", rows.filter((p) => !p.is_starter)],
  ];
  return (
    <div className="table-wrap">
      <table>
        <thead>
          <tr>
            <th>Slot</th><th>Player</th><th>NFL</th><th>Status</th><th title="Season PPR/game (nflverse)" className="num">PPR/g</th>
            <th title="Expected PPR/game from role (nflverse xFP)" className="num">xFP/g</th><th>Trend</th><th title="Market signal (BUY needs a meaningful absolute role)">Signal</th>
            <th title="0-10 rest-of-season value proxy">Value</th>
          </tr>
        </thead>
        {groups.map(([title, ps]) => (
          <tbody key={title}>
            <tr className="group"><td colSpan={9}>{title}</td></tr>
            {ps.map((p) => (
              <tr key={p.espn_player_id}>
                <td className="slot">{p.lineup_slot}</td>
                <td><PlayerLink id={p.espn_player_id} name={p.full_name} /> <Pos value={p.position} /></td>
                <td>{p.pro_team ?? "FA"}{p.on_bye ? <> <Badge value="BYE" tone="warn" /></> : null}</td>
                <td>{p.availability && p.availability.status !== "AVAILABLE"
                  ? <Badge value={p.availability.status} title={`${p.availability.sources.join(", ")} · availability weight ${p.availability.weight}`} /> : ""}</td>
                <td className="num">{num(p.fp_per_game, 1, p.gsis_id ? "No games" : "No NFL id match")}</td>
                <td className="num">{num(p.xfp_per_game, 1, p.gsis_id ? "No games" : "No NFL id match")}</td>
                <td><TrendArrow label={p.trend_label} /></td>
                <td><Badge value={p.market_signal} /></td>
                <td><Score value={p.value_score} /></td>
              </tr>
            ))}
          </tbody>
        ))}
      </table>
    </div>
  );
}
