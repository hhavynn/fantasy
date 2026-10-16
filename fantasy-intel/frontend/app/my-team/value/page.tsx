import Link from "next/link";
import { BackendProblem } from "@/components/BackendProblem";
import { Missing, num } from "@/lib/format";
import { Notice, PageHeader, Stat, StatStrip, Tabs } from "@/components/ui";
import { api } from "@/lib/api";
import { parseSort, type SortState } from "@/lib/sort";
import { PHASE_TEXT, parsePhase, phaseState, recordText, withParams } from "@/lib/team-value-helpers";
import type { TeamValue, TvPhase, TvPhaseSplit } from "@/lib/types";
import {
  BenchSection, MARGINAL_SORT_KEYS, MarginalTable, RaceContext, SlotGaps, SlotStrength, Summary, TopContributors,
  TradePreview, ValueMethod, WaiverMoves, WeeklyChart,
} from "./sections";
import styles from "./value.module.css";

export const metadata = { title: "Team value" };

// My Team Value: roster strength vs the league, who really moves the starting lineup, unused bench
// projection, upgrades and the playoff-race context. Every number comes from the canonical engine
// (ESPN weekly projections + the exact weekly lineup optimizer); nothing here is a probability.

const PATH = "/my-team/value";
const MARGINAL_DEFAULT: SortState = { key: "marginal", dir: "desc" };
const PHASES: TvPhase[] = ["all", "regular", "playoff"];
const TAB_TEXT: Record<TvPhase, string> = { all: "Rest of season", regular: "Regular", playoff: "Playoffs" };

const one = (v: string | string[] | undefined): string | undefined => (Array.isArray(v) ? v[0] : v);

/** A phase's projected total; its known partial sum marked with an asterisk; or a dash when no weeks are left. */
function total(s: TvPhaseSplit) {
  switch (phaseState(s)) {
    case "empty": return <Missing why="No weeks left in this phase" />;
    case "incomplete": return <>{num(s.known_points, 1)}*</>;
    default: return num(s.projected_points, 1);
  }
}

const NO_WEEKS = "No weeks left in this phase";

export default async function TeamValuePage({ searchParams }: PageProps<"/my-team/value">) {
  const sp = await searchParams;
  const team = one(sp.team);
  const phase = parsePhase(one(sp.phase));
  const trades = one(sp.trades) !== "off";
  const sort = parseSort(sp, MARGINAL_SORT_KEYS, MARGINAL_DEFAULT);

  const q = new URLSearchParams();
  if (team) q.set("team_id", team);
  if (!trades) q.set("trades", "false");
  const res = await api<TeamValue>(`/api/my-team/value?${q.toString()}`);
  if (!res.ok) {
    return (
      <div className="stack">
        <PageHeader title="My team value" />
        <BackendProblem error={res.error} kind={res.kind} status={res.status} />
      </div>
    );
  }
  const d = res.data;
  const weeks = d.overview?.phases?.[phase]?.weeks ?? d.window.weeks;

  const tabs = PHASES.map((p) => ({ href: withParams(PATH, sp, { phase: p === "all" ? null : p }), label: TAB_TEXT[p] }));
  const currentTab = withParams(PATH, sp, { phase: phase === "all" ? null : phase });

  return (
    <div className="stack">
      <PageHeader
        eyebrow="My team"
        title="My team value"
        meta={`${d.team.name}${d.team.is_mine ? " (you)" : ""} · ${PHASE_TEXT[phase]}${weeks.length ? ` · weeks ${weeks[0]}–${weeks[weeks.length - 1]}` : ""}`}
        actions={
          <form action={PATH} className={styles.picker}>
            {phase !== "all" && <input type="hidden" name="phase" value={phase} />}
            {!trades && <input type="hidden" name="trades" value="off" />}
            <label>
              <span>Team</span>
              <select name="team" defaultValue={d.team.team_id}>
                {d.teams.map((t) => <option key={t.team_id} value={t.team_id}>{t.name}{t.is_mine ? " (you)" : ""}</option>)}
              </select>
            </label>
            <button type="submit">Show</button>
          </form>
        }
      >
        <div className={styles.subhead}>
          <Tabs items={tabs} current={currentTab} label="Projection window" />
          <nav className={styles.related} aria-label="Related pages">
            <Link href="/trades/discover">Trade discovery</Link>
            <Link href="/waivers">Waivers</Link>
            <Link href="/playoff-race">Playoff race</Link>
            <Link href="/trades">Trade evaluator</Link>
          </nav>
        </div>
      </PageHeader>

      {d.window.uncertain && d.window.warning && <Notice tone="caution">{d.window.warning}</Notice>}

      {d.empty ? (
        <Notice tone="info">{d.message}</Notice>
      ) : (
        <Dashboard d={d} phase={phase} trades={trades} sort={sort} sp={sp} />
      )}
    </div>
  );
}

function Dashboard({ d, phase, trades, sort, sp }: {
  d: TeamValue;
  phase: TvPhase;
  trades: boolean;
  sort: SortState;
  sp: Record<string, string | string[] | undefined>;
}) {
  const ov = d.overview!;
  const split = ov.phases[phase];
  const ranks = ov.league_ranks.phases[phase];
  const unused = [...d.bench!.players].sort((a, b) => b.unused_projected_points - a.unused_projected_points)[0];
  const partial = [ov.phases.regular, ov.phases.playoff].some((s) => phaseState(s) === "incomplete");
  return (
    <>
      <StatStrip>
        <Stat
          label="Record (ESPN)"
          value={recordText(d.standing.record)}
          note={<>Seed {d.standing.espn_seed ?? "—"} · PF {num(d.standing.points_for, 1)} · PA {num(d.standing.points_against, 1)}</>}
        />
        <Stat
          label={`Projected starting points`}
          value={total(split)}
          note={
            phaseState(split) === "empty" ? NO_WEEKS
              : <>{split.weeks.length} weeks · {split.average_weekly !== null ? <>{num(split.average_weekly, 1)} per week</> : `incomplete: week ${split.incomplete_weeks.join(", ")}`}</>
          }
        />
        <Stat
          label="Roster strength rank"
          value={split.league_rank ? <>#{split.league_rank.rank}</> : <Missing why="No comparable weeks" />}
          note={ranks.weeks.length > 0 ? `of ${ranks.teams.length} teams · ${ranks.weeks.length} of ${ranks.phase_weeks.length} weeks comparable` : "No comparable weeks"}
        />
        <Stat
          label="Regular season"
          value={total(ov.phases.regular)}
          note={<>
            {phaseState(ov.phases.regular) === "empty" ? `${NO_WEEKS} · ` : null}
            Fantasy playoffs {phaseState(ov.phases.playoff) === "empty" ? "none left" : total(ov.phases.playoff)}
            {partial ? " · *known total, some weeks incomplete" : null}
          </>}
        />
        <Stat
          label="Largest unused bench projection"
          value={unused ? num(unused.unused_projected_points, 1) : <Missing why="Nobody is on the bench" />}
          note={unused ? `${unused.name}: per player, never summed` : "No bench players"}
        />
      </StatStrip>

      {ov.missing_players.length > 0 && (
        <Notice tone="caution" title={`${ov.summary.ros_weeks_complete} of ${ov.summary.ros_weeks_expected} weeks fully projected`}>
          Missing projections: {ov.missing_players.join(", ")}. Totals that depend on them are labelled as partial.
        </Notice>
      )}

      <Summary d={d} phase={phase} />

      <div className={styles.pair}>
        <WeeklyChart d={d} />
        <TopContributors d={d} phase={phase} />
      </div>

      <SlotStrength d={d} phase={phase} />
      <MarginalTable d={d} phase={phase} sort={sort} sp={sp} />
      <BenchSection d={d} />
      <SlotGaps d={d} />
      <WaiverMoves d={d} />
      <TradePreview d={d} trades={trades} sp={sp} />
      <RaceContext d={d} />
      <ValueMethod d={d} />
    </>
  );
}
