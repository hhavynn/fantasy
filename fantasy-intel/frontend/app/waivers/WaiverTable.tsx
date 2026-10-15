import Link from "next/link";
import { PlayerLink } from "@/components/PlayerLink";
import { Badge, EmptyState, Pos, Score, SortableTh } from "@/components/ui";
import type { SearchParams, SortState } from "@/lib/sort";
import { Evidence } from "./Evidence";
import { Competition, EvidenceBadge, GiveUp, OwnedDelta, RoleTrend, actionLabel } from "./WaiverParts";
import { hasInjuryDesignation } from "../players/helpers";
import { label } from "@/lib/format";
import styles from "./waivers.module.css";
import type { WaiverRow } from "./types";

const COLS = 7;

/**
 * One tbody per player: the decision row, then a quiet row holding the collapsible evidence.
 * `sp` is the search params without `offset`, so a sort link starts again from the first page.
 */
export function WaiverTable({ rows, sort, sp, empty }: {
  rows: WaiverRow[];
  sort: SortState;
  sp: SearchParams;
  empty: { title: string; body: string; href: string; linkText: string };
}) {
  if (rows.length === 0) {
    return (
      <EmptyState title={empty.title} action={<Link href={empty.href}>{empty.linkText}</Link>}>
        {empty.body}
      </EmptyState>
    );
  }
  return (
    <div className="table-wrap">
      <table className={styles.table}>
        <thead>
          <tr>
            <SortableTh sortKey="rank" label="Player" defaultDir="asc" current={sort} searchParams={sp} title="Decision rank: actionable moves first, then roster impact (the default order)" />
            <th scope="col">Move</th>
            <SortableTh sortKey="score" label="Score" align="right" current={sort} searchParams={sp} title="Waiver score, 0–10: a compatibility heuristic, not a probability" />
            <SortableTh sortKey="trend" label="Role trend" defaultDir="desc" current={sort} searchParams={sp} title="Composite usage trend: rising, flat, falling" />
            <SortableTh sortKey="owned" label="% owned" align="right" current={sort} searchParams={sp} title="ESPN % owned and its change this week; sorts by the change" />
            <SortableTh sortKey="competition" label="Competition" defaultDir="desc" current={sort} searchParams={sp} title="League interest from managers' historical tendencies and needs; not a forecast of bids" />
            <th scope="col">Give up</th>
          </tr>
        </thead>
        {rows.map((w) => {
          const d = w.components.decision;
          const summary = d ? d.streaming?.summary ?? d.utility.summary : null;
          return (
            <tbody key={w.espn_player_id} className={styles.player}>
              <tr className={styles.main}>
                <td className={styles.who}>
                  <PlayerLink id={w.espn_player_id} name={w.full_name} /> <Pos value={w.position} />
                  <span className={styles.sub}>
                    {w.pro_team ?? "Team unknown"} · {label(w.availability_status)}
                    {hasInjuryDesignation(w.injury_status) && <> <Badge value={w.injury_status} /></>}
                  </span>
                </td>
                <td className={styles.move}>
                  <div className={styles.moveHead}>
                    <Badge value={w.recommendation} />
                    <strong>{actionLabel(w) ?? "Refresh analytics"}</strong>
                    <EvidenceBadge row={w} />
                  </div>
                  {summary && <span className={styles.sub}>{summary}</span>}
                </td>
                <td className="num"><Score value={w.waiver_score} /></td>
                <td><RoleTrend trend={w.role_trend} /></td>
                <td className="num"><OwnedDelta row={w} /></td>
                <td><Competition row={w} /></td>
                <td className={styles.giveUp}><GiveUp row={w} /></td>
              </tr>
              <tr className={styles.more}>
                <td colSpan={COLS}>
                  <details className={styles.details}>
                    <summary>Reasons, alternatives and trade-offs</summary>
                    {d
                      ? <Evidence row={w} decision={d} />
                      : (
                        <div className={styles.evidence}>
                          <p>Refresh analytics to generate roster legality and acquisition advice.</p>
                          <p>Existing recommendation: {w.recommendation}. An absent drop does not establish an open roster spot.</p>
                          <ul>{w.reasons.map((r, i) => <li key={i}>{r}</li>)}</ul>
                        </div>
                      )}
                  </details>
                </td>
              </tr>
            </tbody>
          );
        })}
      </table>
    </div>
  );
}
