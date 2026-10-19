import type { ReactNode } from "react";
import { Section, VisuallyHidden } from "@/components/ui";
import {
  hasStarted, leader, lineupRows, lineupTotal, pairSlots, type LineupTotal, type LineupView, type PlayerRow,
} from "@/lib/matchup";
import type { Matchup, TeamSide } from "@/lib/matchup-types";
import { num } from "@/lib/format";
import { EMPTY_RANGES, RANGE_FOOTNOTE, type RangeMap } from "@/lib/projection-ranges";
import { PlayerCell, Points } from "./PlayerCell";
import styles from "./matchup.module.css";

const FOOTNOTE: Record<LineupView, string> = {
  projected: "Each team's best lineup from ESPN's weekly projections. It is not necessarily the lineup the manager has set.",
  set: "The lineups held in ESPN as of the last sync.",
};

function valueHeading(view: LineupView, status: Matchup["status"]): string {
  return view === "set" && hasStarted(status) ? "Actual" : "Proj";
}

function TotalCell({ total, why }: { total: LineupTotal; why: string }) {
  return (
    <>
      {num(total.value, 1, why)}
      {total.incomplete && total.value !== null ? <span className={styles.none} title="Not every player has a projection"> *</span> : null}
    </>
  );
}

/** Phone layout: one list per team, slot by slot. */
function SideList({ name, rows, heading, total, ranges }: { name: string; rows: PlayerRow[] | null; heading: string; total: LineupTotal; ranges: RangeMap }) {
  return (
    <div>
      <h3 className={styles.listHead}>{name}</h3>
      {rows === null ? <p className={styles.noData}>No lineup data.</p> : (
        <ul className={styles.list}>
          {rows.map((r, i) => (
            <li key={`${r.slot}-${i}`} className={styles.row}>
              <span className={styles.slotTag}>{r.slot}</span>
              <PlayerCell row={r} />
              <span className={styles.rowPts}><Points value={r.value} row={r} ranges={ranges} /></span>
            </li>
          ))}
          <li className={`${styles.row} ${styles.listTotal}`}>
            <span className={styles.slotTag}>{heading}</span>
            <span>Total</span>
            <span className={styles.rowPts}><TotalCell total={total} why="No total available" /></span>
          </li>
        </ul>
      )}
    </div>
  );
}

/** The head-to-head: my player, points, slot, points, their player, aligned slot by slot. */
export function Lineups({ m, opponent, view, actions, ranges = EMPTY_RANGES }: { m: Matchup; opponent: TeamSide; view: LineupView; actions?: ReactNode; ranges?: RangeMap }) {
  const meRows = lineupRows(m.me, view, m.status);
  const oppRows = lineupRows(opponent, view, m.status);
  const pairs = pairSlots(meRows ?? [], oppRows ?? []);
  const heading = valueHeading(view, m.status);
  const meTotal = lineupTotal(m.me, view, meRows);
  const oppTotal = lineupTotal(opponent, view, oppRows);
  return (
    <Section title={view === "set" ? "Lineups set in ESPN" : "Best projected lineups"} actions={actions} flush footnote={ranges.size > 0 ? `${FOOTNOTE[view]} ${RANGE_FOOTNOTE}` : FOOTNOTE[view]}>
      <div className={styles.h2hWrap}>
        <div className="table-wrap">
          <table className={styles.h2h}>
            <thead>
              <tr>
                <th>{m.me.name}</th>
                <th className={styles.pts}>{heading}</th>
                <th className={styles.slotCol}>Slot</th>
                <th className={styles.pts}>{heading}</th>
                <th className={styles.rightCol}>{opponent.name}</th>
              </tr>
            </thead>
            <tbody>
              {pairs.map((p, i) => {
                const lead = leader(p.left?.value ?? null, p.right?.value ?? null);
                return (
                  <tr key={`${p.slot}-${i}`}>
                    <td><PlayerCell row={p.left} /></td>
                    <td className={`${styles.pts} ${lead === "left" ? styles.lead : ""}`}>
                      <Points value={p.left ? p.left.value : null} why={p.left ? "No ESPN projection" : "No such slot"} row={p.left} ranges={ranges} />
                      {lead === "left" ? <VisuallyHidden> (higher)</VisuallyHidden> : null}
                    </td>
                    <td className={styles.slotCol}>{p.slot}</td>
                    <td className={`${styles.pts} ${lead === "right" ? styles.lead : ""}`}>
                      <Points value={p.right ? p.right.value : null} why={p.right ? "No ESPN projection" : "No such slot"} row={p.right} ranges={ranges} />
                      {lead === "right" ? <VisuallyHidden> (higher)</VisuallyHidden> : null}
                    </td>
                    <td className={styles.rightCol}><PlayerCell row={p.right} align="right" /></td>
                  </tr>
                );
              })}
            </tbody>
            <tfoot>
              <tr>
                <td />
                <td className={styles.pts}><TotalCell total={meTotal} why="No total available" /></td>
                <td className={styles.slotCol}>Total</td>
                <td className={styles.pts}><TotalCell total={oppTotal} why="No total available" /></td>
                <td />
              </tr>
            </tfoot>
          </table>
        </div>
      </div>
      <div className={styles.lists}>
        <SideList name={m.me.name} rows={meRows} heading={heading} total={meTotal} ranges={ranges} />
        <SideList name={opponent.name} rows={oppRows} heading={heading} total={oppTotal} ranges={ranges} />
      </div>
    </Section>
  );
}

/** Your own projected lineup when there is no opponent (bye week, pairings not set). */
export function SoloLineup({ m, ranges = EMPTY_RANGES }: { m: Matchup; ranges?: RangeMap }) {
  const rows = lineupRows(m.me, "projected", m.status);
  if (rows === null) return null;
  const total = lineupTotal(m.me, "projected", rows);
  return (
    <Section title="Your best projected lineup" flush footnote={ranges.size > 0 ? `${FOOTNOTE.projected} ${RANGE_FOOTNOTE}` : FOOTNOTE.projected}>
      <div className="table-wrap">
        <table className={styles.h2h}>
          <thead><tr><th className={styles.slotCol}>Slot</th><th>Player</th><th className={styles.pts}>Proj</th></tr></thead>
          <tbody>
            {rows.map((r, i) => (
              <tr key={`${r.slot}-${i}`}>
                <td className={styles.slotCol}>{r.slot}</td>
                <td><PlayerCell row={r} /></td>
                <td className={styles.pts}><Points value={r.value} row={r} ranges={ranges} /></td>
              </tr>
            ))}
          </tbody>
          <tfoot>
            <tr>
              <td className={styles.slotCol}>Total</td>
              <td />
              <td className={styles.pts}><TotalCell total={total} why="No total available" /></td>
            </tr>
          </tfoot>
        </table>
      </div>
    </Section>
  );
}
