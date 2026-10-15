import { BackendProblem } from "@/components/BackendProblem";
import { PlayerLink } from "@/components/PlayerLink";
import {
  Badge, Delta, Method, Notice, PageHeader, Pos, Section, SortableTh, Stat, StatStrip,
} from "@/components/ui";
import { api } from "@/lib/api";
import { Missing, num, when } from "@/lib/format";
import { parseSort, sortRows, type SortState } from "@/lib/sort";
import {
  actionabilityBadge,
  lineupTotal,
  lockStatusBadge,
  slotChange,
  startSitVerdict,
} from "@/lib/start-sit-helpers";
import { StartSitControls } from "./StartSitControls";
import type { BenchPlayer, DecisionPlayer, LineupPlayer, StartSitDecision, StartSitResponse } from "./types";
import styles from "./start-sit.module.css";

export const metadata = { title: "Start/Sit" };

const BENCH_SORTS = ["name", "proj"] as const;
const BENCH_DEFAULT: SortState = { key: "proj", dir: "desc" };

const HEALTHY = ["ACTIVE", "NORMAL"];

type SP = Record<string, string | string[] | undefined>;

const one = (v: string | string[] | undefined): string | undefined => (Array.isArray(v) ? v[0] : v);

export default async function StartSitPage({ searchParams }: PageProps<"/start-sit">) {
  const sp: SP = await searchParams;
  const qs = new URLSearchParams();
  const teamId = one(sp.team_id);
  const week = one(sp.week);
  if (teamId) qs.set("team_id", teamId);
  if (week) qs.set("week", week);

  const res = await api<StartSitResponse>(`/api/start-sit?${qs.toString()}`);
  if (!res.ok) {
    return (
      <div className="stack">
        <PageHeader title="Start/Sit" />
        <BackendProblem error={res.error} kind={res.kind} status={res.status} />
      </div>
    );
  }

  const d = res.data;
  const { league, team, summary, decisions, recommended_lineup, actual_lineup, unconstrained_lineup, current_bench } = d;
  const moves = decisions.filter((x) => x.action !== "NO_CHANGE");
  const actionable = moves.filter((x) => x.actionability === "ACTIONABLE").length;
  const hasLineup = summary.starters_count > 0;
  const totalPlayers = summary.starters_count + summary.bench_count + summary.ir_count;
  const lockedRows = recommended_lineup.filter((r) => r.lock_status === "LOCKED" && r.player_id !== null);

  const verdict = startSitVerdict({
    status: summary.status,
    startersCount: summary.starters_count,
    projectedGain: summary.projected_gain,
    missingProjections: summary.missing_projections.length,
    scoringPeriod: d.scoring_period,
    moves: moves.length,
    actionableMoves: actionable,
  });
  const actual = lineupTotal(hasLineup, summary.projection_complete, summary.actual_projected_total, summary.known_actual_total);
  const recommended = lineupTotal(true, summary.projection_complete, summary.recommended_projected_total, summary.known_recommended_total);
  const gainKnown = summary.projection_complete && summary.projected_gain !== null;

  return (
    <div className="stack">
      <PageHeader
        title={`Start/Sit · Week ${d.scoring_period}`}
        meta={`${team.name} (${team.abbrev}) · ${d.projection_source} projections · ${summary.locked_players_count} of ${totalPlayers} players kicked off`}
        actions={
          <StartSitControls
            currentTeamId={team.id}
            teams={d.teams && d.teams.length > 0 ? d.teams : [team]}
            currentWeek={league.current_week}
            selectedWeek={d.scoring_period}
            finalWeek={league.final_scoring_period}
          />
        }
      />

      {/* Verdict band: the answer first, then the numbers behind it. */}
      <div className={styles.verdict}>
        <Notice tone={verdict.tone} title={verdict.headline}>{verdict.detail}</Notice>
        <StatStrip>
          <Stat
            label="Submitted lineup"
            value={actual.value === null ? <Missing why="No submitted lineup for this week" /> : `${num(actual.value, 1)}${actual.partial ? "*" : ""}`}
            note={!hasLineup ? "No submitted lineup" : actual.partial ? "*Partial: some starters lack a projection" : "ESPN starters, projected pts"}
          />
          <Stat
            label="Recommended lineup"
            value={`${num(recommended.value, 1)}${recommended.partial ? "*" : ""}`}
            note={recommended.partial ? "*Partial: some players lack a projection" : "Projected pts, respects kickoff locks"}
          />
          <Stat
            label="Projected gain"
            tone={gainKnown && (summary.projected_gain ?? 0) > 0 ? "positive" : undefined}
            value={gainKnown ? <Delta value={summary.projected_gain} suffix=" pts" /> : <Missing why={hasLineup ? "Cannot compute: missing projections" : "No submitted lineup to compare"} />}
            note={
              !hasLineup ? "Needs a submitted lineup"
                : !summary.projection_complete ? "Needs every starter projected"
                : (summary.projected_gain ?? 0) > 0 ? "Over the submitted lineup"
                : "Submitted lineup is already the best"
            }
          />
          <Stat label="Moves" value={moves.length} note={moves.length > 0 ? `${actionable} actionable now` : "No swaps recommended"} />
          <Stat
            label="Kickoff locks"
            value={lockedRows.length}
            note={lockedRows.length > 0 ? `Locked: ${lockedRows.map((r) => r.slot).join(", ")}` : "No starting slot locked"}
          />
        </StatStrip>
      </div>

      {d.is_future_preview && (
        <Notice tone="info" title={`Future week preview (week ${d.scoring_period})`}>
          Lineup assignments are an informational projection based on current roster assets. ESPN does not accept lineup submissions for a future scoring period until the current week concludes.
        </Notice>
      )}

      {summary.missing_projections.length > 0 && (
        <Notice tone="caution" title="Missing projections">
          {summary.missing_projections.map((p) => `${p.name} (${p.position})`).join(", ")}. Their points are unknown, not zero.
        </Notice>
      )}

      {moves.length > 0 && (
        <Section
          title="Recommended moves"
          actions={<span className="faint">{moves.length} {moves.length === 1 ? "move" : "moves"} · not additive</span>}
          footnote="Gains are measured against the submitted lineup as a whole and are never summed across moves: FLEX dependencies share opportunity cost."
          flush
        >
          <ul className={styles.moves}>
            {moves.map((m) => <Move key={m.id} move={m} />)}
          </ul>
        </Section>
      )}

      <Section
        title="Lineup: submitted vs recommended"
        footnote={`${summary.starters_count} starters · ${summary.bench_count} bench · ${summary.ir_count} IR. Kickoff locks use stored NFL schedule data and are conservative.`}
        flush
      >
        <LineupTable recommended={recommended_lineup} actual={actual_lineup} />
      </Section>

      {d.injury_warnings.length > 0 || d.schedule_warnings.length > 0 ? (
        <Section title="Availability alerts" flush={d.injury_warnings.length > 0}>
          {d.injury_warnings.length > 0 && (
            <div className="table-wrap">
              <table>
                <thead><tr><th>Player</th><th>Status</th><th>Detail</th></tr></thead>
                <tbody>
                  {d.injury_warnings.map((w) => (
                    <tr key={w.player_id}>
                      <td><PlayerLink id={w.player_id} name={w.name} /> <Pos value={w.position} /></td>
                      <td>
                        <span className={styles.person2}>
                          {w.injury_status && !HEALTHY.includes(w.injury_status) && <Badge value={w.injury_status} />}
                          {w.on_ir && <Badge value="IR slot" tone="bad" />}
                          {w.nfl_ineligible && <Badge value="NFL out" tone="bad" />}
                        </span>
                      </td>
                      <td className="wrap">{w.warnings.length > 0 ? w.warnings.join("; ") : <span className="faint">Designation only</span>}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
          {d.schedule_warnings.length > 0 && (
            <ul className="reasons">
              {d.schedule_warnings.map((w, i) => <li key={i}><b>Schedule:</b> {w}</li>)}
            </ul>
          )}
        </Section>
      ) : null}

      <BenchSection bench={current_bench} recommended={recommended_lineup} sp={sp} />

      <Benchmark
        summary={summary}
        recommended={recommended_lineup}
        unconstrained={unconstrained_lineup}
      />

      <Method title="How this is calculated">
        <p className={styles.prose}>
          Three lineups are compared for the week: your submitted ESPN lineup, the best lineup under kickoff locks (the one recommended here),
          and an unconstrained optimum used only as a benchmark. Projections are unmodified ESPN weekly projections. Players whose games
          have kicked off keep their slots, and fantasy IR or NFL-ineligible players are never promoted.
        </p>
        <p className={styles.prose}>
          Projected gain is the recommended total minus the submitted total. When any starter lacks a projection no gain is claimed.
          A move that shuffles several slots (a FLEX cascade) is shown once, as a single coordinated move.
        </p>
        <ul className={`reasons ${styles.limits}`}>
          {d.limitations.map((l, i) => <li key={i}>{l}</li>)}
        </ul>
      </Method>

      <p className={styles.fresh}>
        Lineup synced {when(d.freshness.lineup_synced_at)} · ESPN projections synced {when(d.freshness.projection_synced_at)} · NFL schedule synced {when(d.freshness.nfl_schedule_synced_at)}
      </p>
    </div>
  );
}

// ------------------------------------------------------------------- moves --

function MovePlayer({ p }: { p: DecisionPlayer }) {
  const lock = p.lock_status !== "UNLOCKED" ? lockStatusBadge(p.lock_status, null) : null;
  return (
    <span className={styles.person}>
      <PlayerLink id={p.id} name={p.name} /> <Pos value={p.position} /> <span className={styles.proj}>({num(p.projection, 1)})</span>
      {lock && <> <Badge value={lock.label} tone={lock.tone === "warn" ? "warn" : ""} /></>}
    </span>
  );
}

function PlayerList({ players }: { players: DecisionPlayer[] }) {
  return (
    <>
      {players.map((p, i) => (
        <span key={p.id}>{i > 0 ? ", " : ""}<MovePlayer p={p} /></span>
      ))}
    </>
  );
}

function Move({ move }: { move: StartSitDecision }) {
  const badge = actionabilityBadge(move.actionability);
  return (
    <li className={styles.move}>
      <div>
        <p className={styles.sentence}>
          <span className={styles.verb}>Start</span> <PlayerList players={move.start_players} />{" "}
          <span className={styles.over}>over</span> <PlayerList players={move.bench_players} />
        </p>
        <p className={styles.reason}>{move.explanation}</p>
        {move.warnings.length > 0 && (
          <ul className={styles.cautions}>
            {move.warnings.map((w, i) => <li key={i}>{w}</li>)}
          </ul>
        )}
      </div>
      <div className={styles.moveMeta}>
        <span className={styles.gain}><Delta value={move.projected_net_gain} suffix=" pts" /></span>
        <span className={styles.tags}>
          <Badge value={badge.label} tone={badge.tone} />
          <Badge value={move.action === "COORDINATED_SWAP" ? "FLEX cascade" : "1-for-1 swap"} tone="" />
        </span>
        {move.target_slot && <span className={styles.slotNote}>Slot: {move.target_slot}</span>}
      </div>
    </li>
  );
}

// ------------------------------------------------------------------ lineup --

function OppCell({ r }: { r: Pick<LineupPlayer, "opponent" | "is_home" | "lock_status" | "kickoff_utc" | "has_final_score" | "home_score" | "away_score"> }) {
  const lock = lockStatusBadge(r.lock_status, r.kickoff_utc, r.has_final_score, r.home_score, r.away_score);
  return (
    <span className={styles.person2}>
      {r.opponent
        ? <span>{r.is_home ? "vs" : "@"} {r.opponent}</span>
        : <span className="faint">{r.lock_status === "BYE" ? "Bye" : "No game"}</span>}
      <Badge value={lock.label} tone={lock.tone === "warn" ? "warn" : lock.tone === "info" ? "info" : ""} />
    </span>
  );
}

function LineupTable({ recommended, actual }: { recommended: LineupPlayer[]; actual: LineupPlayer[] }) {
  const kept = new Set(recommended.map((r) => r.player_id).filter((id): id is number => id !== null));
  const leaving = actual.filter((a) => a.player_id !== null && !kept.has(a.player_id));
  return (
    <div className="table-wrap">
      <table>
        <thead>
          <tr>
            <th>Slot</th>
            <th>Player</th>
            <th>Opponent</th>
            <th>Versus submitted</th>
            <th className="num" title="ESPN projected points for the week">Proj</th>
          </tr>
        </thead>
        <tbody>
          {recommended.map((r) => {
            const change = slotChange({ slot: r.slot, playerId: r.player_id, isCurrentlyStarting: r.is_currently_starting, currentSlot: r.current_slot });
            return (
              <tr key={r.slot_id}>
                <td className={styles.slotName}>{r.slot}</td>
                <td>
                  {r.player_id !== null ? (
                    <span className={styles.person2}>
                      <PlayerLink id={r.player_id} name={r.name} />
                      <Pos value={r.position} />
                      <span className="faint">{r.pro_team ?? "FA"}</span>
                      {r.injury_status && !HEALTHY.includes(r.injury_status) && <Badge value={r.injury_status} />}
                    </span>
                  ) : <span className={styles.empty}>Empty</span>}
                </td>
                <td>{r.player_id !== null ? <OppCell r={r} /> : <Missing why="No player in this slot" />}</td>
                <td className={styles.change}>
                  {change.kind === "promote" ? <span className={styles.promote}>{change.text}</span>
                    : change.kind === "empty" ? <span className={styles.empty} title="No eligible player is on the roster for this slot">{change.text}</span>
                    : change.text}
                </td>
                <td className="num"><b>{num(r.projected_points, 1)}</b></td>
              </tr>
            );
          })}
        </tbody>
        {leaving.length > 0 && (
          <tbody>
            <tr className="group"><td colSpan={5}>Moving out of the lineup</td></tr>
            {leaving.map((a) => (
              <tr key={a.slot_id} className={styles.out}>
                <td>{a.current_slot ?? a.slot}</td>
                <td>
                  <span className={styles.person2}>
                    <PlayerLink id={a.player_id as number} name={a.name} />
                    <Pos value={a.position} />
                    <span className="faint">{a.pro_team ?? "FA"}</span>
                    {a.injury_status && !HEALTHY.includes(a.injury_status) && <Badge value={a.injury_status} />}
                  </span>
                </td>
                <td><OppCell r={a} /></td>
                <td>To bench</td>
                <td className="num">{num(a.projected_points, 1)}</td>
              </tr>
            ))}
          </tbody>
        )}
      </table>
    </div>
  );
}

// ------------------------------------------------------------------- bench --

function BenchSection({ bench, recommended, sp }: { bench: BenchPlayer[]; recommended: LineupPlayer[]; sp: SP }) {
  if (bench.length === 0) return null;
  const sort = parseSort(sp, BENCH_SORTS, BENCH_DEFAULT);
  const promoted = new Set(recommended.map((r) => r.player_id));
  const rows = sortRows(bench, (b) => (sort.key === "name" ? b.name : b.projected_points), sort.dir);
  return (
    <Section title="Bench and reserves" actions={<span className="faint">{bench.length} players</span>} flush>
      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              <SortableTh sortKey="name" label="Player" current={sort} searchParams={sp} defaultDir="asc" />
              <th>Opponent</th>
              <th>Role</th>
              <SortableTh sortKey="proj" label="Proj" align="right" current={sort} searchParams={sp} title="ESPN projected points for the week" />
            </tr>
          </thead>
          <tbody>
            {rows.map((b) => (
              <tr key={b.player_id}>
                <td>
                  <span className={styles.person2}>
                    <PlayerLink id={b.player_id} name={b.name} />
                    <Pos value={b.position} />
                    <span className="faint">{b.pro_team ?? "FA"}</span>
                    {b.on_ir && <Badge value="IR slot" tone="bad" />}
                    {b.nfl_ineligible && <Badge value="NFL out" tone="bad" />}
                    {b.injury_status && !HEALTHY.includes(b.injury_status) && <Badge value={b.injury_status} />}
                  </span>
                </td>
                <td><OppCell r={{ ...b, has_final_score: false, home_score: null, away_score: null }} /></td>
                <td>{promoted.has(b.player_id) ? <span className={styles.promote}>Recommended starter</span> : "Bench reserve"}</td>
                <td className="num">{num(b.projected_points, 1)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </Section>
  );
}

// --------------------------------------------------------------- benchmark --

function Benchmark({ summary, recommended, unconstrained }: {
  summary: StartSitResponse["summary"];
  recommended: LineupPlayer[];
  unconstrained: LineupPlayer[];
}) {
  const total = summary.unconstrained_total;
  const rec = summary.recommended_projected_total;
  const drag = total !== null && rec !== null ? Math.max(0, total - rec) : null;
  const inRec = new Set(recommended.map((r) => r.player_id));
  const extra = unconstrained.filter((u) => u.player_id !== null && !inRec.has(u.player_id));
  return (
    <Section title="Unconstrained benchmark" footnote="A theoretical ceiling that ignores kickoff locks. It is not a recommendation.">
      <dl className={styles.figures}>
        <div>
          <dt>Unconstrained optimum</dt>
          <dd>{total !== null ? num(total, 1) : <>{num(summary.known_unconstrained_total, 1)}*</>}</dd>
        </div>
        <div>
          <dt title="Unconstrained optimum minus the lock-constrained recommendation">Lock drag</dt>
          <dd>{drag !== null ? num(drag, 1) : <Missing why="Needs complete projections" />}</dd>
        </div>
      </dl>
      {total === null && <p className={styles.prose}>*Partial total: some players lack a projection.</p>}
      {drag !== null && drag > 0 ? (
        <p className={styles.prose}>
          Kickoff locks cost {num(drag, 1)} projected points. Without locks the lineup would also start{" "}
          {extra.map((u, i) => <span key={u.slot_id}>{i > 0 ? ", " : ""}<PlayerLink id={u.player_id as number} name={u.name} /></span>)}.
        </p>
      ) : drag === 0 ? (
        <p className={styles.prose}>Kickoff locks are not costing any projected points.</p>
      ) : null}
    </Section>
  );
}
