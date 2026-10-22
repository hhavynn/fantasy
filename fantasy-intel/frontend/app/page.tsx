import Link from "next/link";
import { Suspense } from "react";
import { BackendProblem } from "@/components/BackendProblem";
import { MovesTable } from "@/components/MovesTable";
import { TeamPerspective, UnknownTeam } from "@/components/TeamPerspective";
import { PlayerIdentity } from "@/components/PlayerIdentity";
import { PlayerLink } from "@/components/PlayerLink";
import {
  Badge, EmptyState, Glossary, Method, Notice, PageHeader, Pos, ProvenanceBadge, Rating, Score, Section, SortableTh, Stat, StatStrip, TrendArrow,
} from "@/components/ui";
import { api } from "@/lib/api";
import type { StartSitInput, WaiverInput } from "@/lib/action-items";
import { Missing, num } from "@/lib/format";
import { providerName } from "@/lib/labels";
import { getLeagueProfile } from "@/lib/league-profile.server";
import { RATING_CAVEAT, RATING_DESCRIPTION, ratingFor } from "@/lib/ratings";
import { ordinal } from "@/lib/matchup";
import { withSource } from "@/lib/projection-source";
import { getProjectionSource } from "@/lib/projection-source.server";
import type { Matchup } from "@/lib/matchup-types";
import { signalsOf, type WithProvenance } from "@/lib/signal-provenance";
import { parseSort, sortRows, type SortState } from "@/lib/sort";
import { isUnknownTeam, readTeamSelection, withTeamId, type Perspective } from "@/lib/team-perspective";
import type { League, Move, Need, RosterPlayer, SyncInfo, Team } from "@/lib/types";
import { WeekBand, WeekBandSkeleton } from "./WeekBand";

// The template does not apply to a page in the layout's own segment, so the full title is given.
export const metadata = { title: { absolute: "Overview · Fantasy Intel" } };

type Overview = {
  league: League; my_team: Team | null; team: Team | null; perspective: Perspective | null; standings: Team[]; roster: RosterPlayer[]; injuries: RosterPlayer[];
  needs: Need[]; recent_transactions: Move[]; sync: SyncInfo; nfl_latest_week: number | null;
} & WithProvenance;

const STANDINGS_SORTS = ["rank", "record", "pf", "pa"] as const;
const STANDINGS_DEFAULT: SortState = { key: "rank", dir: "asc" };

export default async function OverviewPage({ searchParams }: PageProps<"/">) {
  const sp = await searchParams;
  const sel = readTeamSelection(sp);
  if (sel.kind === "invalid") {
    return (
      <div className="stack">
        <PageHeader title="Overview" actions={<TeamPerspective path="/" sp={sp} current={null} />} />
        <UnknownTeam path="/" sel={sel} />
      </div>
    );
  }
  // Each section has its own source; one failing endpoint degrades only its own section. The decision hub's three
  // sources start now but are not awaited here: the hub streams in behind the header, stat strip and roster.
  const [projection, profile] = await Promise.all([getProjectionSource(), getLeagueProfile()]);
  const provider = providerName(profile.provider);
  // Every section is computed for the selected team (`?team=`); without one, the owner's team as before.
  const matchup = api<Matchup>(withTeamId(withSource("/api/matchup", projection), sel));
  const startSit = api<StartSitInput & WithProvenance>(withTeamId(withSource("/api/start-sit", projection), sel));
  const waivers = api<{ players: WaiverInput[]; total: number } & WithProvenance>(withTeamId("/api/waivers?view=BEST&limit=3", sel));
  const res = await api<Overview>(withTeamId("/api/overview", sel));
  const band = (
    <Suspense fallback={<WeekBandSkeleton />}>
      <WeekBand matchup={matchup} startSit={startSit} waivers={waivers} projection={projection}
                overview={res.ok ? { roster: res.data.roster, league: res.data.league, provenance: res.data.provenance } : null}
                team={sel.kind === "team" ? String(sel.id) : null} />
    </Suspense>
  );

  if (isUnknownTeam(res, sel)) {
    return (
      <div className="stack">
        <PageHeader title="Overview" actions={<TeamPerspective path="/" sp={sp} current={null} />} />
        <UnknownTeam path="/" sel={sel} detail={res.error} />
      </div>
    );
  }
  if (!res.ok) {
    // Without the overview, the page is only worth showing if the matchup (the week band's anchor) loaded.
    if (!(await matchup).ok) {
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
        {band}
      </div>
    );
  }
  const o = res.data;
  const me = o.team;
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
        eyebrow={`Week ${o.league.current_week} · ${o.league.name}`}
        title={me ? me.name : "Team overview"}
        meta={`${me ? `${me.wins}–${me.losses}${me.ties ? `–${me.ties}` : ""}${rank ? ` · ${ordinal(rank)} of ${n}` : ""} · ` : ""}${o.league.season} season · ${o.league.size} teams · ${scoring}${o.league.is_faab ? ` · FAAB $${o.league.faab_budget}` : " · waiver priority"}`}
        // The header vouches for the roster and injuries; the usage signal backs only the hub's trade decisions.
        provenance={<ProvenanceBadge signals={signalsOf(o.provenance).filter((s) => s.signal !== "usage")} />}
        actions={<TeamPerspective path="/" sp={sp} current={me?.team_id ?? null} />}
      />

      {!me && (profile.provider === "espn" ? (
        <Notice tone="caution">
          No default team is set (it wasn&apos;t identified from SWID). Pick a team above to analyze it, or set <code>ESPN_MY_TEAM_ID</code> in <code>.env</code> and re-sync.
        </Notice>
      ) : (
        <Notice tone="caution">No default team is set. Pick a team above to analyze it.</Notice>
      ))}


      {projection === "ours" && profile.providerProjections && (
        <Notice tone="info" title="This week's matchup and lineup use Fantasy Intel projections">
          Switched in the header (K and D/ST from our kicker and D/ST models). Waiver and rest-of-season numbers stay on ESPN.
        </Notice>
      )}

      {band}

      {me && (
        <StatStrip compact>
          <Stat label="Record" value={`${me.wins}–${me.losses}${me.ties ? `–${me.ties}` : ""}`} note={rank ? `${ordinal(rank)} of ${n}` : undefined} />
          <Stat label="Pts for" value={num(me.points_for, 1)} note={pfRank ? `${ordinal(pfRank)} in league` : undefined} />
          <Stat label="Pts against" value={num(me.points_against, 1)} note={paRank ? `${ordinal(paRank)} most allowed` : undefined} />
          <Stat
            label={o.league.is_faab ? "FAAB left" : "Waiver priority"}
            value={o.league.is_faab
              ? (o.league.faab_budget != null && me.faab_spent != null ? `$${o.league.faab_budget - me.faab_spent}` : <Missing why="FAAB budget or spending not synced" />)
              : `#${me.waiver_rank ?? "?"}`}
            note={o.league.is_faab ? `of $${o.league.faab_budget ?? "?"}` : `of ${n} teams`}
          />
          <Stat label="Injuries" value={o.injuries.length} note="designations on the roster" />
        </StatStrip>
      )}

      <div className="grid-main">
        <Section title="Current roster" subtitle={`${o.roster.filter((p) => p.is_starter).length} starters · ${o.roster.filter((p) => !p.is_starter).length} bench and IR`} flush>
          <RosterTable rows={o.roster} />
        </Section>

        <div className="stack">
          {o.injuries.length > 0 && <InjuriesSection injuries={o.injuries} provider={provider} />}
          <Section title="Biggest roster needs" subtitle="Where the lineup is thinnest against the league" flush>
            <div className="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th>Pos</th><th title="Positional need, 0–10 scale (not a player rating)">Need</th>
                    <th className="num" title="Availability-weighted bodies vs depth target">Avail</th>
                    <th className="num" title="This team's starters' internal 0–10 value at the position vs the league average starter (not the 0–100 value rating)">Value / avg</th>
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
              Need = 0.55·shortage of availability-weighted bodies + 0.45·gap between the team&apos;s starters&apos; value at the position (fixed and FLEX-assigned) and the league
              average. FLEX is one real slot filled by the best eligible player. Weights: available 1, questionable 0.75, doubtful 0.15, out/IR 0.
            </p>
          </Method>

          <StandingsSection standings={o.standings} sp={sp} selected={me?.team_id ?? null} />
        </div>
      </div>

      <div className="stack">
        <Section title="Recent league transactions" flush={o.recent_transactions.length > 0} footnote={o.recent_transactions.length > 0 ? <Link href="/transactions">See all transactions</Link> : undefined}>
          {o.recent_transactions.length === 0
            ? <EmptyState title="No transactions recorded">Adds, drops and trades appear here after the next {provider} sync.</EmptyState>
            : <MovesTable moves={o.recent_transactions} />}
        </Section>
        <Glossary terms={["PPR/g", "xFP/g", "Value rating", "Projected points", "FAAB", "Q / D / O", "—"]} providerProjections={profile.providerProjections} />
      </div>
    </div>
  );
}

const gamesOf = (t: Team) => t.wins + t.losses + t.ties;
/** Win share for sorting only (a tie counts half); teams with no games yet sort last. */
const winShare = (t: Team) => (gamesOf(t) === 0 ? null : (t.wins + t.ties / 2) / gamesOf(t));

function StandingsSection({ standings, sp, selected }: { standings: Team[]; sp: Record<string, string | string[] | undefined>; selected: number | null }) {
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
              <tr key={t.team_id} className={t.team_id === selected ? "mine" : ""}>
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

function InjuriesSection({ injuries, provider }: { injuries: RosterPlayer[]; provider: string }) {
  return (
    <Section title="Injuries on the roster" subtitle={`${injuries.length} ${injuries.length === 1 ? "player" : "players"} with a designation`} flush>
      <div className="table-wrap">
        <table>
          <thead><tr><th>Player</th><th>{provider}</th><th>NFL report</th></tr></thead>
          <tbody>
            {injuries.map((p) => (
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
            <th>Player</th><th>Status</th><th title="Season PPR/game (nflverse)" className="num">PPR/g</th>
            <th title="Expected PPR/game from role (nflverse xFP)" className="num">xFP/g</th><th>Trend</th><th title="Market signal (BUY needs a meaningful absolute role)">Signal</th>
            <th title={`Value rating, 0–100: ${RATING_DESCRIPTION.value}. ${RATING_CAVEAT}`}>Value</th>
          </tr>
        </thead>
        {groups.map(([title, ps]) => (
          <tbody key={title}>
            <tr className="group"><td colSpan={7}>{title}</td></tr>
            {ps.map((p) => (
              <tr key={p.espn_player_id}>
                <td>
                  <PlayerIdentity id={p.espn_player_id} name={p.full_name} position={p.position} team={p.pro_team}
                                  lead={<span title="Lineup slot">{p.lineup_slot}</span>}
                                  meta={p.on_bye ? <Badge value="BYE" tone="warn" /> : null} />
                </td>
                <td>{p.availability && p.availability.status !== "AVAILABLE"
                  ? <Badge value={p.availability.status} title={`${p.availability.sources.join(", ")} · availability weight ${p.availability.weight}`} /> : ""}</td>
                <td className="num key">{num(p.fp_per_game, 1, p.gsis_id ? "No games" : "No NFL id match")}</td>
                <td className="num">{num(p.xfp_per_game, 1, p.gsis_id ? "No games" : "No NFL id match")}</td>
                <td><TrendArrow label={p.trend_label} /></td>
                <td><Badge value={p.market_signal} /></td>
                <td><Rating rating={ratingFor(p, "value")} kind="value" /></td>
              </tr>
            ))}
          </tbody>
        ))}
      </table>
    </div>
  );
}
