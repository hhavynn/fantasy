import { Suspense } from "react";
import { BackendProblem } from "@/components/BackendProblem";
import { Delta, Notice, Section, SkeletonTable } from "@/components/ui";
import type { ApiResult } from "@/lib/api";
import { Missing, num, signed } from "@/lib/format";
import {
  MAX_ASSETS, nameOf, pathText, questionText, usesStream, type RosterPool, type RosterUtility, type UtilityChange,
} from "@/lib/roster-utility";
import type { TvPlayer } from "@/lib/types";
import styles from "./value.module.css";

// Roster utility (#216): what removing one to three of this roster's players changes in its exact optimal starting
// lineups, week by week, with the replacement the chosen pool allows. Several players are evaluated together, never
// added up. A plain GET form of this page; the answer streams in under it.

type Params = Record<string, string | string[] | undefined>;
const KEEP = ["team", "phase", "trades", "sort", "dir"];      // the page's own state survives a submit

const why = (c: UtilityChange) => `Incomplete: a projection is missing in week ${c.weeks_incomplete.join(", ")}`;

function Total({ c }: { c: UtilityChange }) {
  return c.ros_delta !== null ? <Delta value={c.ros_delta} />
    : <span title={why(c)}>{signed(c.known_ros_delta, 1)}* <span className={styles.faintText}>(known weeks only)</span></span>;
}

export function RosterUtilitySection({ players, sp, action, chosen, pool, result, notices = [] }: {
  players: TvPlayer[]; sp: Params; action: string; chosen: number[]; pool: RosterPool;
  result: Promise<ApiResult<RosterUtility>> | null;
  /** What the page changed about the question asked (lib/roster-utility.adjustments): said, never silent. */
  notices?: string[];
}) {
  const roster = [...players].sort((a, b) => (a.name ?? "").localeCompare(b.name ?? "") || a.espn_player_id - b.espn_player_id);
  return (
    <Section id="roster-utility" title="Roster utility"
      subtitle="How this roster's projected starting points change without one to three of its players, and who fills in"
      footnote="Several players are removed together and evaluated as one lineup, never added up: one can be the other's backfill.">
      <form action={`${action}#roster-utility`} className={styles.utilityForm} aria-label="Roster utility question">
        {KEEP.map((k) => {
          const v = sp[k];
          return typeof v === "string" ? <input key={k} type="hidden" name={k} value={v} /> : null;
        })}
        <fieldset className={styles.utilityPlayers}>
          <legend>Players to remove (up to {MAX_ASSETS})</legend>
          {roster.map((p) => (
            <label key={p.espn_player_id} className={styles.utilityPlayer}>
              <input type="checkbox" name="mu" value={p.espn_player_id} defaultChecked={chosen.includes(p.espn_player_id)} />
              <span>{p.name ?? `Player ${p.espn_player_id}`} <span className={styles.faintText}>{p.position ?? "—"}</span></span>
            </label>
          ))}
        </fieldset>
        <fieldset className={styles.utilityPool}>
          <legend>Who fills in</legend>
          <label><input type="radio" name="pool" value="bench" defaultChecked={pool === "bench"} /> The rest of your roster</label>
          <label><input type="radio" name="pool" value="waiver" defaultChecked={pool === "waiver"} /> Plus the best available free agent (one player only)</label>
        </fieldset>
        <button type="submit">Show the lineup change</button>
      </form>
      {result && notices.map((n) => <Notice key={n} tone="info">{n}</Notice>)}
      {result && (
        <Suspense fallback={<div role="status" aria-busy="true" aria-label="Evaluating the lineups"><SkeletonTable rows={4} cols={3} /></div>}>
          <Answer result={result} />
        </Suspense>
      )}
    </Section>
  );
}

async function Answer({ result }: { result: Promise<ApiResult<RosterUtility>> }) {
  const r = await result;
  if (!r.ok) {
    return r.status === 422 ? <Notice tone="caution" title="That question cannot be evaluated">{r.error}</Notice>
      : <BackendProblem error={r.error} kind={r.kind} status={r.status} />;
  }
  const d = r.data;
  const c = d.change;
  const path = c ?? d.removal;                 // an unknown pool still shows the bench-alone path
  const streamed = d.streaming.enabled && usesStream(path);
  const base = d.baseline;
  const noCandidates = d.pool === "waiver" && !d.replacement && (d.waiver_candidates?.scored ?? 0) === 0;
  return (
    <div className={styles.utilityAnswer}>
      <p className={styles.strong}>{questionText(d, streamed)}</p>
      {d.reserved_note && <Notice tone="info">{d.reserved_note}</Notice>}
      {d.status !== "evaluated" && d.reason && <Notice tone="caution">{d.reason}</Notice>}
      {c ? (
        <dl className={styles.utilityFigures}>
          <div><dt>Projected now</dt><dd>{base.ros_projected_starting_points !== null ? num(base.ros_projected_starting_points, 1)
            : <span title="A projection is missing in some week">{num(base.ros_known_starting_points, 1)}*</span>}</dd></div>
          <div><dt>Rest of season</dt><dd><Total c={c} /></dd></div>
          <div><dt>Regular season</dt><dd>{c.regular_season_delta !== null ? <Delta value={c.regular_season_delta} /> : <Missing why="Unknown, or no regular-season weeks left" />}</dd></div>
          <div><dt>Playoffs</dt><dd>{c.playoff_delta !== null ? <Delta value={c.playoff_delta} /> : <Missing why="Unknown, or no playoff weeks left" />}</dd></div>
          {d.pool === "waiver" && (
            <div><dt>Free agent added</dt><dd>{d.replacement ? nameOf(d, d.replacement.espn_player_id)
              : noCandidates ? "No free agent at this position in the pool" : "None that helps"}</dd></div>
          )}
        </dl>
      ) : (
        <p className={styles.faintText}>Without a known free-agent pool no replacement is assumed. From the bench alone: <Total c={d.removal} />.</p>
      )}
      {d.asset_ids.length > 1 && d.single_deltas && (
        <p className={styles.faintText}>
          One at a time: {d.asset_ids.map((id) => {
            const v = d.single_deltas?.[String(id)];
            return <span key={id}>{nameOf(d, id)} {v == null ? "unknown" : signed(v, 1)}{id !== d.asset_ids[d.asset_ids.length - 1] ? " · " : ""}</span>;
          })}
          {d.nonadditive ? ". Together the loss differs from their sum, because one backs the other up." : "."}
        </p>
      )}
      <div className="table-wrap">
        <table>
          <caption className={styles.faintText}>Week by week: the change and who moves in or out of the starting lineup</caption>
          <thead><tr><th scope="col">Week</th><th scope="col" className="num">Change</th><th scope="col">Lineup</th></tr></thead>
          <tbody>
            {path.weeks.map((w) => (
              <tr key={w.week}>
                <th scope="row">{w.week}</th>
                <td className="num">{w.delta !== null ? <Delta value={w.delta} /> : <Missing why="A projection is missing this week" />}</td>
                <td className={styles.utilityPath}>{pathText(d, w)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <p className={styles.caption}>
        Unit: {d.unit}. Projections: {d.projection_source}. {d.horizon.note}
        {streamed && d.streaming.note ? ` ${d.streaming.note}` : ""}
        {d.waiver_availability && d.waiver_availability.excluded_unknown_availability > 0
          ? ` ${d.waiver_availability.excluded_unknown_availability} free agents with unknown availability were not considered.` : ""}
        {d.waiver_availability && d.waiver_availability.excluded_position_limit > 0
          ? ` ${d.waiver_availability.excluded_position_limit} free agents who could fill in would exceed the league's position limit.` : ""}
      </p>
    </div>
  );
}
