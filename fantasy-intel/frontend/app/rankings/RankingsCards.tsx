import { PlayerIdentity } from "@/components/PlayerIdentity";
import { TrendArrow } from "@/components/ui";
import { Missing } from "@/lib/format";
import { availabilityWarning, opponentLabel, rankLabel, type RankingRow } from "@/lib/rankings";
import { AvailabilityBadge, Points, RatingText, RosterBadge } from "./RankingParts";
import styles from "./rankings.module.css";

const TREND_WORD: Record<string, string> = { UP: "Rising", FLAT: "Flat", DOWN: "Falling" };

/**
 * The phone layout: a dense ranked list, one compact row per player (rank, identity, the ranked projection on the
 * right, then opponent, rating, trend and roster on a second line). It keeps players comparable down the screen
 * instead of one tall card each. Shown instead of the table at 720px and below (CSS only).
 */
export function RankingsCards({ rows, showOurs, basis, espnColumn = true }: {
  rows: RankingRow[]; showOurs: boolean; basis: "espn" | "ours";
  /** False for a league whose provider publishes no projections (Sleeper): only Ours is shown. */
  espnColumn?: boolean;
}) {
  const onOurs = basis === "ours" || !espnColumn;
  const ptsLabel = onOurs ? "Ours" : "ESPN";
  return (
    <ol className={styles.cardView} aria-label="Player rankings">
      {rows.map((r) => {
        const ranked = r.rank.overall !== null;
        const warning = availabilityWarning(r);
        const why = r.why[0];
        return (
          <li key={r.espn_player_id} className={r.roster.is_mine ? `${styles.card} ${styles.mine}` : styles.card}>
            <span className={styles.cardRank}>
              <strong className={ranked ? undefined : styles.unranked}>{rankLabel(r)}</strong>
              {ranked && <span className={styles.overall} title="Overall rank among all QB, RB, WR and TE">#{r.rank.overall}</span>}
            </span>
            <PlayerIdentity id={r.espn_player_id} name={r.full_name} position={r.position} team={r.pro_team}
              meta={<AvailabilityBadge row={r} />} className={styles.cardWho} />
            <span className={styles.cardPts}>
              <span className={styles.keyFact}>
                <Points value={onOurs ? r.projection.ours : r.projection.espn} status={r.projection.status} />
              </span>
              <span className={styles.cardPtsLabel}>{ptsLabel} pts</span>
              {showOurs && espnColumn && (
                <span className={styles.cardPtsLabel}>ESPN <Points value={r.projection.espn} status={r.projection.status} /></span>
              )}
            </span>
            <span className={styles.cardMeta}>
              <span>{opponentLabel(r.game)}</span>
              <span title="Overall rating, 0–100, within the position">Rating <b><RatingText value={r.rating.overall} /></b></span>
              <span>Trend {r.role_trend && TREND_WORD[r.role_trend] ? <><TrendArrow label={r.role_trend} /> {TREND_WORD[r.role_trend]}</> : <Missing why="Too few games for a role trend" />}</span>
              <RosterBadge roster={r.roster} />
            </span>
            {warning && <p className={styles.warning}>{warning}</p>}
            {why && <p className={styles.why}>{why}</p>}
          </li>
        );
      })}
    </ol>
  );
}
