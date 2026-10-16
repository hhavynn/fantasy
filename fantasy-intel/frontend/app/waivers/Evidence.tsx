import Link from "next/link";
import { CompareLink } from "@/components/CompareLink";
import { PlayerLink } from "@/components/PlayerLink";
import { Pos } from "@/components/ui";
import { label, num, when } from "@/lib/format";
import type { Decision, WaiverRow } from "./types";
import styles from "./waivers.module.css";

/**
 * Everything behind one recommendation: the roster move, what it costs, the evidence limits and the
 * competition. All of the old per-player copy lives here, one click from the table row.
 */
export function Evidence({ row, decision: d }: { row: WaiverRow; decision: Decision }) {
  const stream = d.streaming;
  const drop = d.roster_move.drop;
  return (
    <div className={styles.evidence}>
      <p><b>Roster move.</b> {d.roster_move.summary}</p>
      {drop && <p>Give up <PlayerLink id={drop.espn_player_id} name={drop.name} /> <Pos value={drop.position} /></p>}
      <p>
        <CompareLink a={row.espn_player_id} b={drop?.espn_player_id} aName={row.full_name} bName={drop?.name}>
          {drop ? "Compare with the suggested drop" : "Compare"}
        </CompareLink>
      </p>
      {d.roster_move.status === "UNKNOWN" && <p>Missing drop information does not establish an open spot.</p>}
      {d.utility.stash && <p>Potential bench stash; the future role remains uncertain.</p>}
      <p><b>Cost.</b> {d.resources.summary}</p>
      {d.resources.alternative && (
        <p>Compare <PlayerLink id={d.resources.alternative.espn_player_id} name={d.resources.alternative.name} /> before using priority.</p>
      )}
      {d.risks.length > 0 && (
        <>
          <p><b>Evidence limits</b></p>
          <ul>{d.risks.map((r) => <li key={r}>{r}</li>)}</ul>
        </>
      )}

      <p>
        <b>Player quality.</b> {row.components.player.basis_description ?? row.components.player.basis}; opportunity {num(row.opportunity_score)}/10,
        historical value {num(row.components.player.value)}/10. These are heuristics, not claim-success probabilities.
      </p>
      <ul>{row.reasons.map((r, i) => <li key={i}>{r}</li>)}</ul>

      <p><b>Roster benefit.</b> {d.utility.summary}</p>
      <p>
        Future lineup gain: {num(d.utility.forward.total_gain)} ESPN points across {d.utility.forward.weeks.length} comparable weeks.
        {d.utility.forward.missing_weeks.length > 0 && ` ${d.utility.forward.missing_weeks.length} weeks lack complete comparisons.`}
      </p>
      {drop && <ul>{drop.sacrifice.map((s) => <li key={s}>{s}</li>)}</ul>}
      {d.roster_move.alternatives.length > 1 && (
        <p>
          Other defensible drops:{" "}
          {d.roster_move.alternatives.slice(1, 4).map((alt, i) => (
            <span key={alt.espn_player_id}>{i > 0 && ", "}<PlayerLink id={alt.espn_player_id} name={alt.name} /></span>
          ))}.
        </p>
      )}
      {d.roster_move.excluded.length > 0 && (
        <details>
          <summary>Why other drops were excluded</summary>
          <ul>
            {d.roster_move.excluded.map((x) => (
              <li key={x.espn_player_id}><PlayerLink id={x.espn_player_id} name={null} />: {x.reason}</li>
            ))}
          </ul>
        </details>
      )}

      {stream && (
        <>
          <p><b>{label(stream.category)}.</b> {stream.summary}</p>
          <p>
            Best other available projection: {num(stream.alternative_points)}. Opponent implied total: {num(stream.opponent_implied_total)}.
            Team implied total: {num(stream.team_implied_total)}. Lines provide context; they are not converted into invented fantasy points.
          </p>
          {stream.opponent_offense && (
            <p>
              Opponent offense: sack rate {stream.opponent_offense.sack_rate === null ? "unknown" : `${num(stream.opponent_offense.sack_rate * 100, 1)}%`} across {stream.opponent_offense.games} stored games.
            </p>
          )}
          {stream.defense_performance && (
            <p>
              Defense context across {stream.defense_performance.games} games: pass EPA allowed/play {num(stream.defense_performance.pass_epa_allowed, 2)},
              rush EPA allowed/play {num(stream.defense_performance.rush_epa_allowed, 2)}. Context only; not a fantasy-points adjustment.
            </p>
          )}
          <ul>
            {stream.weeks.map((w) => (
              <li key={w.week}>
                Week {w.week}: {w.bye ? "bye" : w.opponent ? `vs ${w.opponent}` : "opponent unknown"}; projected {num(w.points)}, your lineup {num(w.incumbent_points)}.
              </li>
            ))}
          </ul>
          <p>Projection snapshot: {when(stream.projection_synced_at)}.</p>
        </>
      )}

      <p><b>Competition: {row.league_interest.toLowerCase()} interest.</b> Historical tendencies and needs; no prediction of bids or claim success.</p>
      <ul>
        {row.competitors.map((c) => (
          <li key={c.team_id}>
            <Link href={`/managers/${c.team_id}`}>{c.name}</Link>{c.reasons.length ? `: ${c.reasons.join("; ")}` : ": limited behavioral evidence"}
          </li>
        ))}
      </ul>
      <p>
        <b>Known capacity:</b> {d.roster_move.active_count}/{d.roster_move.capacity ?? "unknown"} active players. IR is excluded from active capacity.
        Position limits: {Object.entries(d.roster_move.position_limits).map(([p, n]) => `${p} ${n}`).join(", ") || "none supplied"}.
      </p>
      <p>Each pickup is a separate scenario against your current roster. Refresh after making a move; these are not a combined claim queue.</p>
    </div>
  );
}
