import Link from "next/link";
import { BackendProblem } from "@/components/BackendProblem";
import { CompareLink } from "@/components/CompareLink";
import { TeamPerspective, UnknownTeam } from "@/components/TeamPerspective";
import { isUnknownTeam, readTeamSelection, withTeam, withTeamId } from "@/lib/team-perspective";
import { PlayerIdentity } from "@/components/PlayerIdentity";
import { PlayerLink } from "@/components/PlayerLink";
import { OriginTag, ProjectionNotice } from "@/components/ProjectionNotice";
import {
  Badge, Delta, Glossary, Method, Notice, PageHeader, Pos, Section, SortableTh, Verdict, VerdictFigure,
} from "@/components/ui";
import { api } from "@/lib/api";
import { Missing, num, when } from "@/lib/format";
import { showingOurs, withSource } from "@/lib/projection-source";
import { getProjectionSource } from "@/lib/projection-source.server";
import { parseSort, sortRows, type SortState } from "@/lib/sort";
import {
  actionabilityBadge,
  emptyStartingSlots,
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

/** Position filters the waivers page accepts via `?pos=`. */
const WAIVER_POSITIONS = ["QB", "RB", "WR", "TE", "K", "D/ST"];

const HEALTHY = ["ACTIVE", "NORMAL"];

type SP = Record<string, string | string[] | undefined>;

const one = (v: string | string[] | undefined): string | undefined => (Array.isArray(v) ? v[0] : v);

export default async function StartSitPage({ searchParams }: PageProps<"/start-sit">) {
  const sp: SP = await searchParams;
  const qs = new URLSearchParams();
  const sel = readTeamSelection(sp);
  const week = one(sp.week);
  if (week) qs.set("week", week);
  if (sel.kind === "invalid") {
    return (
      <div className="stack">
        <PageHeader title="Start/Sit" actions={<TeamPerspective path="/start-sit" sp={sp} current={null} />} />
        <UnknownTeam path="/start-sit" sel={sel} />
      </div>
    );
  }

  const res = await api<StartSitResponse>(withTeamId(withSource(`/api/start-sit?${qs.toString()}`, await getProjectionSource()), sel));
  if (isUnknownTeam(res, sel)) {
    return (
      <div className="stack">
        <PageHeader title="Start/Sit" actions={<TeamPerspective path="/start-sit" sp={sp} current={null} />} />
        <UnknownTeam path="/start-sit" sel={sel} detail={res.error} />
      </div>
    );
  }
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
  const ours = showingOurs(d.projection_source_detail);
  const projLabel = ours ? "Fantasy Intel (research) projected points for the week" : "ESPN projected points for the week";
  const moves = decisions.filter((x) => x.action !== "NO_CHANGE");
  const actionable = moves.filter((x) => x.actionability === "ACTIONABLE").length;
  const hasLineup = summary.starters_count > 0;
  const totalPlayers = summary.starters_count + summary.bench_count + summary.ir_count;
  const lockedRows = recommended_lineup.filter((r) => r.lock_status === "LOCKED" && r.player_id !== null);

  const emptySlots = emptyStartingSlots(recommended_lineup);
  const verdict = startSitVerdict({
    status: summary.status,
    startersCount: summary.starters_count,
    projectedGain: summary.projected_gain,
    missingProjections: summary.missing_projections.length,
    scoringPeriod: d.scoring_period,
    moves: moves.length,
    actionableMoves: actionable,
    emptySlots,
  });
  const actual = lineupTotal(hasLineup, summary.projection_complete, summary.actual_projected_total, summary.known_actual_total);
  const recommended = lineupTotal(true, summary.projection_complete, summary.recommended_projected_total, summary.known_recommended_total);
  const gainKnown = summary.projection_complete && summary.projected_gain !== null;

  return (
    <div className="stack">
      <PageHeader
        eyebrow={team.name}
        title={`Start/Sit · Week ${d.scoring_period}`}
        meta={`${team.name} (${team.abbrev}) · ${d.projection_source} projections · ${summary.locked_players_count} of ${totalPlayers} players kicked off`}
        actions={
          <TeamPerspective path="/start-sit" sp={sp} current={team.id}>
            <StartSitControls
              team={sel.kind === "team" ? String(sel.id) : null}
              currentWeek={league.current_week}
              selectedWeek={d.scoring_period}
              finalWeek={league.final_scoring_period}
            />
          </TeamPerspective>
        }
      />

      <ProjectionNotice detail={d.projection_source_detail} />

      {/* The answer first: the verdict, the totals behind it, then each move. */}
      <Verdict
        tone={verdict.tone}
        eyebrow={`Week ${d.scoring_period} · ${team.name}`}
        headline={verdict.headline}
        figures={
          <>
            <VerdictFigure
              label="Submitted (proj)"
              value={actual.value === null ? <Missing why="No submitted lineup for this week" /> : <>{num(actual.value, 1)}{actual.partial ? "*" : null}</>}
              note={!hasLineup ? "No submitted lineup" : actual.partial ? "*Partial: some starters lack a projection" : "Current ESPN starters, projected pts"}
            />
            <VerdictFigure
              label="Recommended (proj)"
              value={<>{num(recommended.value, 1)}{recommended.partial ? "*" : null}</>}
              note={recommended.partial ? "*Partial: some players lack a projection" : "Best lineup under kickoff locks, projected pts"}
            />
            <VerdictFigure
              label="Projected gain"
              emphasis
              tone={gainKnown && (summary.projected_gain ?? 0) > 0 ? "positive" : undefined}
              value={gainKnown ? <Delta value={summary.projected_gain} suffix=" pts" /> : <Missing why={hasLineup ? "Cannot compute: missing projections" : "No submitted lineup to compare"} />}
              note={
                !hasLineup ? "Needs a submitted lineup"
                  : !summary.projection_complete ? "Needs every starter projected"
                  : (summary.projected_gain ?? 0) > 0 ? "Over the submitted lineup"
                  : "Submitted lineup is already the best"
              }
            />
            <VerdictFigure
              label="Kickoff locks"
              value={lockedRows.length}
              note={lockedRows.length > 0 ? `Locked: ${lockedRows.map((r) => r.slot).join(", ")}` : "No starting slot locked"}
            />
          </>
        }
      >
        {verdict.detail}
        {emptySlots.map((slot) => (
          <span key={slot}> <Link href={withTeam(WAIVER_POSITIONS.includes(slot) ? `/waivers?pos=${encodeURIComponent(slot)}` : "/waivers", sel.kind === "team" ? String(sel.id) : null)}>Find a {slot} on waivers</Link>.</span>
        ))}
      </Verdict>

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
          subtitle={`${actionable} of ${moves.length} can be made now · gains are not additive`}
          footnote="Gains are measured against the submitted lineup as a whole and are never summed across moves: FLEX dependencies share opportunity cost."
          flush
        >
          <ul className={styles.moves}>
            {moves.map((m) => <Move key={m.id} move={m} ours={ours} />)}
          </ul>
        </Section>
      )}

      <Section
        title="Lineup: submitted vs recommended"
        footnote={`${summary.starters_count} starters · ${summary.bench_count} bench · ${summary.ir_count} IR. Kickoff locks use stored NFL schedule data and are conservative.`}
        flush
      >
        <LineupTable recommended={recommended_lineup} actual={actual_lineup} ours={ours} projLabel={projLabel} />
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

      <BenchSection bench={current_bench} recommended={recommended_lineup} sp={sp} ours={ours} projLabel={projLabel} />

      <Benchmark
        summary={summary}
        recommended={recommended_lineup}
        unconstrained={unconstrained_lineup}
      />

      <Glossary terms={["Projected points", "Projected gain", "Kickoff lock", "FLEX", "IR", "Ours", "—"]} />
      <Method title="How this is calculated">
        <p className={styles.prose}>
          Three lineups are compared for the week: the submitted ESPN lineup, the best lineup under kickoff locks (the one recommended here),
          and an unconstrained optimum used only as a benchmark. {ours
            ? "Projections are Fantasy Intel's independent expected points (switch in the header); players marked ESPN keep ESPN's number."
            : "Projections are unmodified ESPN weekly projections."} Players whose games
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

function MovePlayer({ p, ours, dir }: { p: DecisionPlayer; ours: boolean; dir: "in" | "out" }) {
  const lock = p.lock_status !== "UNLOCKED" ? lockStatusBadge(p.lock_status, null) : null;
  return (
    <li className={styles.swapRow}>
      <span className={dir === "in" ? styles.inGlyph : styles.outGlyph} aria-hidden="true">{dir === "in" ? "▲" : "▼"}</span>
      <PlayerIdentity id={p.id} name={p.name} position={p.position} team={p.pro_team || null}
                      meta={lock ? <Badge value={lock.label} tone={lock.tone === "warn" ? "warn" : ""} /> : null} />
      <span className={styles.swapProj}><OriginTag origin={p.projection_origin} ours={ours} /> {num(p.projection, 1)}</span>
    </li>
  );
}

function Move({ move, ours }: { move: StartSitDecision; ours: boolean }) {
  const badge = actionabilityBadge(move.actionability);
  // A plain 1-for-1 swap has an obvious pair to compare; a multi-player cascade does not.
  const pair = move.start_players.length === 1 && move.bench_players.length === 1 ? [move.start_players[0], move.bench_players[0]] : null;
  return (
    <li className={styles.move}>
      <div className={styles.swap}>
        <div className={styles.swapSide}>
          <p className={styles.swapLabel}><span className={styles.verb}>Start</span></p>
          <ul className={styles.swapList}>{move.start_players.map((p) => <MovePlayer key={p.id} p={p} ours={ours} dir="in" />)}</ul>
        </div>
        <div className={styles.swapSide}>
          <p className={styles.swapLabel}><span className={styles.over}>Bench</span></p>
          <ul className={styles.swapList}>{move.bench_players.map((p) => <MovePlayer key={p.id} p={p} ours={ours} dir="out" />)}</ul>
        </div>
      </div>
      <div className={styles.moveMeta}>
        <span className={styles.gain}><Delta value={move.projected_net_gain} suffix=" pts" /></span>
        <span className={styles.tags}>
          <Badge value={badge.label} tone={badge.tone} />
          <Badge value={move.action === "COORDINATED_SWAP" ? "FLEX cascade" : "1-for-1 swap"} tone="" />
        </span>
        {move.target_slot && <span className={styles.slotNote}>Slot: {move.target_slot}</span>}
        {pair && <CompareLink a={pair[0].id} b={pair[1].id} aName={pair[0].name} bName={pair[1].name} />}
      </div>
      {move.warnings.length > 0 && (
        <ul className={styles.cautions}>
          {move.warnings.map((w, i) => <li key={i}>{w}</li>)}
        </ul>
      )}
      <details className={styles.why}>
        <summary>Why this move</summary>
        <p className={styles.reason}>{move.explanation}</p>
      </details>
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

function LineupTable({ recommended, actual, ours, projLabel }: { recommended: LineupPlayer[]; actual: LineupPlayer[]; ours: boolean; projLabel: string }) {
  const kept = new Set(recommended.map((r) => r.player_id).filter((id): id is number => id !== null));
  const leaving = actual.filter((a) => a.player_id !== null && !kept.has(a.player_id));
  // Who takes a leaving player's place is only unambiguous with one swap; otherwise link the leaving player alone.
  const promoted = recommended.filter((r) => r.player_id !== null && !r.is_currently_starting);
  const partner = leaving.length === 1 && promoted.length === 1 ? promoted[0] : null;
  return (
    <div className="table-wrap">
      <table>
        <thead>
          <tr>
            <th>Player</th>
            <th>Opponent</th>
            <th>Versus submitted</th>
            <th className="num" title={projLabel}>Proj</th>
          </tr>
        </thead>
        <tbody>
          {recommended.map((r) => {
            const change = slotChange({ slot: r.slot, playerId: r.player_id, isCurrentlyStarting: r.is_currently_starting, currentSlot: r.current_slot });
            return (
              <tr key={r.slot_id}>
                <td>
                  {r.player_id !== null ? (
                    <PlayerIdentity id={r.player_id} name={r.name} position={r.position} team={r.pro_team}
                                    lead={<span title="Lineup slot">{r.slot}</span>}
                                    meta={r.injury_status && !HEALTHY.includes(r.injury_status) ? <Badge value={r.injury_status} /> : null} />
                  ) : (
                    <span className={styles.lineCell}><span className={styles.slotName} title="Lineup slot">{r.slot}</span><span className={styles.empty}>Empty</span></span>
                  )}
                </td>
                <td>{r.player_id !== null ? <OppCell r={r} /> : <Missing why="No player in this slot" />}</td>
                <td className={styles.change}>
                  {change.kind === "promote" ? <span className={styles.promote}>{change.text}</span>
                    : change.kind === "empty" ? <span className={styles.empty} title="No eligible player is on the roster for this slot">{change.text}</span>
                    : change.text}
                </td>
                <td className="num"><OriginTag origin={r.projection_origin} ours={ours} /> <b>{num(r.projected_points, 1)}</b></td>
              </tr>
            );
          })}
        </tbody>
        {leaving.length > 0 && (
          <tbody>
            <tr className="group"><td colSpan={4}>Moving out of the lineup</td></tr>
            {leaving.map((a) => (
              <tr key={a.slot_id} className={styles.out}>
                <td>
                  <PlayerIdentity id={a.player_id as number} name={a.name} position={a.position} team={a.pro_team}
                                  lead={<span title="Current slot">{a.current_slot ?? a.slot}</span>}
                                  meta={a.injury_status && !HEALTHY.includes(a.injury_status) ? <Badge value={a.injury_status} /> : null} />
                </td>
                <td><OppCell r={a} /></td>
                <td>
                  To bench{" "}
                  <CompareLink a={partner?.player_id ?? (a.player_id as number)} b={partner ? (a.player_id as number) : undefined} aName={partner?.name ?? a.name} bName={partner ? a.name : undefined} />
                </td>
                <td className="num"><OriginTag origin={a.projection_origin} ours={ours} /> {num(a.projected_points, 1)}</td>
              </tr>
            ))}
          </tbody>
        )}
      </table>
    </div>
  );
}

// ------------------------------------------------------------------- bench --

function BenchSection({ bench, recommended, sp, ours, projLabel }: { bench: BenchPlayer[]; recommended: LineupPlayer[]; sp: SP; ours: boolean; projLabel: string }) {
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
              <SortableTh sortKey="proj" label="Proj" align="right" current={sort} searchParams={sp} title={projLabel} />
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
                <td className="num"><OriginTag origin={b.projection_origin} ours={ours} /> {num(b.projected_points, 1)}</td>
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
