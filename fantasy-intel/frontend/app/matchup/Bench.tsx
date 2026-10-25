import { Section } from "@/components/ui";
import { benchRows, hasStarted, type LineupView, type PlayerRow } from "@/lib/matchup";
import type { Matchup, TeamSide } from "@/lib/matchup-types";
import { EMPTY_RANGES, type RangeMap } from "@/lib/projection-ranges";
import { PlayerCell, Points } from "./PlayerCell";
import styles from "./matchup.module.css";

function BenchTable({ name, rows, heading, ranges, projBy }: { name: string; rows: PlayerRow[] | null; heading: string; ranges: RangeMap; projBy: string }) {
  return (
    <div>
      <h3 className={styles.benchTitle}>{name}</h3>
      {rows === null ? <p className={styles.noData}>No bench data.</p> : rows.length === 0 ? <p className={styles.noData}>Nobody on the bench.</p> : (
        <div className="table-wrap">
          <table className={styles.bench}>
            <thead><tr><th>Player</th><th className={styles.pts}>{heading}</th></tr></thead>
            <tbody>
              {rows.map((r, i) => (
                <tr key={`${r.id}-${i}`}>
                  <td>
                    <PlayerCell row={r} />
                    {r.slot === "IR" ? <> <span className={styles.team3} title="In a fantasy IR slot">IR</span></> : null}
                  </td>
                  <td className={styles.pts}><Points value={r.value} why={`No ${projBy} projection`} row={r} ranges={ranges} /></td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

/** Both benches. On phones they sit behind a toggle; browsers that can show closed details content open it on wide screens. */
export function Bench({ m, opponent, view, ranges = EMPTY_RANGES, provider = "ESPN", projBy = "ESPN" }: {
  m: Matchup; opponent: TeamSide | null; view: LineupView; ranges?: RangeMap;
  /** Where lineups are set, and whose projections the page uses ("ESPN", or "Fantasy Intel"). */
  provider?: string; projBy?: string;
}) {
  const mine = benchRows(m.me, view, m.status);
  const theirs = opponent ? benchRows(opponent, view, m.status) : null;
  if (mine === null && theirs === null) return null;
  const heading = view === "set" && hasStarted(m.status) ? "Actual" : "Proj";
  const count = (mine?.length ?? 0) + (theirs?.length ?? 0);
  return (
    <Section title="Bench" footnote={view === "projected" ? `Rostered players outside the best projected lineup. A dash means ${projBy} has no projection for the player: unknown, not zero.` : `Bench and IR slots as set in ${provider}.`}>
      <details className={styles.benchDetails}>
        <summary>Show bench ({count} players)</summary>
        <div className={styles.benchGrid}>
          <BenchTable name={m.me.name} rows={mine} heading={heading} ranges={ranges} projBy={projBy} />
          {opponent ? <BenchTable name={opponent.name} rows={theirs} heading={heading} ranges={ranges} projBy={projBy} /> : null}
        </div>
      </details>
    </Section>
  );
}
