import type { ReactNode } from "react";
import { PlayerLink } from "@/components/PlayerLink";
import { Badge, Section, VisuallyHidden } from "@/components/ui";
import {
  hasDesignation, hasStarted, leader, lineupRows, lineupTotal, pairSlots, type LineupTotal, type LineupView, type PlayerRow, type SlotRow,
} from "@/lib/matchup";
import type { Matchup, TeamSide } from "@/lib/matchup-types";
import { num } from "@/lib/format";
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

/** One side of a phone pair row: the name on top, position and team under it, the points on the inner edge. */
function PairSide({ row, side, lead }: { row: PlayerRow | null; side: "left" | "right"; lead: boolean }) {
  return (
    <div className={`${styles.pairSide} ${side === "right" ? styles.pairRight : ""}`}>
      <div className={styles.pairWho}>
        {row === null ? <span className={styles.none} title="This team has no such slot">—</span>
          : row.id === null ? <span className={styles.empty} title="No player is projected for this slot">Empty</span>
          : (
            <>
              <PlayerLink id={row.id} name={row.name} />
              <span className={styles.pairMeta}>
                {row.position ? <span>{row.position}</span> : null}
                {row.pro_team ? <span>{row.pro_team}</span> : null}
                {row.bye ? <Badge value="BYE" tone="warn" title="His NFL team has no game this week" /> : null}
                {hasDesignation(row.injury) ? <Badge value={row.injury} /> : null}
              </span>
            </>
          )}
      </div>
      <span className={`${styles.pairPts} ${lead ? styles.pairLead : ""}`}>
        <Points value={row ? row.value : null} why={row ? "No ESPN projection" : "No such slot"} />
        {lead ? <VisuallyHidden> (higher)</VisuallyHidden> : null}
      </span>
    </div>
  );
}

/** Phone layout: the two lineups paired slot by slot, my player on the left and theirs on the right. */
function PairList({ m, opponent, pairs, heading, meTotal, oppTotal }: {
  m: Matchup; opponent: TeamSide; pairs: SlotRow<PlayerRow>[]; heading: string; meTotal: LineupTotal; oppTotal: LineupTotal;
}) {
  return (
    <ol className={styles.pairs} aria-label={`${m.me.name} against ${opponent.name}, slot by slot`}>
      <li className={`${styles.pair} ${styles.pairHeadRow}`} aria-hidden="true">
        <span className={styles.pairTeam}>{m.me.name}</span>
        <span className={styles.pairSlot}>{heading}</span>
        <span className={`${styles.pairTeam} ${styles.pairTeamRight}`}>{opponent.name}</span>
      </li>
      {pairs.map((p, i) => {
        const lead = leader(p.left?.value ?? null, p.right?.value ?? null);
        return (
          <li key={`${p.slot}-${i}`} className={styles.pair}>
            <PairSide row={p.left} side="left" lead={lead === "left"} />
            <span className={styles.pairSlot}>{p.slot}</span>
            <PairSide row={p.right} side="right" lead={lead === "right"} />
          </li>
        );
      })}
      <li className={`${styles.pair} ${styles.pairTotal}`}>
        <span className={styles.pairTotalValue}><TotalCell total={meTotal} why="No total available" /></span>
        <span className={styles.pairSlot}>Total</span>
        <span className={`${styles.pairTotalValue} ${styles.pairTotalRight}`}><TotalCell total={oppTotal} why="No total available" /></span>
      </li>
    </ol>
  );
}

/** The head-to-head: my player, points, slot, points, their player, aligned slot by slot. */
export function Lineups({ m, opponent, view, actions }: { m: Matchup; opponent: TeamSide; view: LineupView; actions?: ReactNode }) {
  const meRows = lineupRows(m.me, view, m.status);
  const oppRows = lineupRows(opponent, view, m.status);
  const pairs = pairSlots(meRows ?? [], oppRows ?? []);
  const heading = valueHeading(view, m.status);
  const meTotal = lineupTotal(m.me, view, meRows);
  const oppTotal = lineupTotal(opponent, view, oppRows);
  return (
    <Section title={view === "set" ? "Lineups set in ESPN" : "Best projected lineups"} actions={actions} flush footnote={FOOTNOTE[view]}>
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
                      <Points value={p.left ? p.left.value : null} why={p.left ? "No ESPN projection" : "No such slot"} />
                      {lead === "left" ? <VisuallyHidden> (higher)</VisuallyHidden> : null}
                    </td>
                    <td className={styles.slotCol}>{p.slot}</td>
                    <td className={`${styles.pts} ${lead === "right" ? styles.lead : ""}`}>
                      <Points value={p.right ? p.right.value : null} why={p.right ? "No ESPN projection" : "No such slot"} />
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
        <PairList m={m} opponent={opponent} pairs={pairs} heading={heading} meTotal={meTotal} oppTotal={oppTotal} />
      </div>
    </Section>
  );
}

/** Your own projected lineup when there is no opponent (bye week, pairings not set). */
export function SoloLineup({ m }: { m: Matchup }) {
  const rows = lineupRows(m.me, "projected", m.status);
  if (rows === null) return null;
  const total = lineupTotal(m.me, "projected", rows);
  return (
    <Section title="Your best projected lineup" flush footnote={FOOTNOTE.projected}>
      <div className="table-wrap">
        <table className={styles.h2h}>
          <thead><tr><th className={styles.slotCol}>Slot</th><th>Player</th><th className={styles.pts}>Proj</th></tr></thead>
          <tbody>
            {rows.map((r, i) => (
              <tr key={`${r.slot}-${i}`}>
                <td className={styles.slotCol}>{r.slot}</td>
                <td><PlayerCell row={r} /></td>
                <td className={styles.pts}><Points value={r.value} /></td>
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
