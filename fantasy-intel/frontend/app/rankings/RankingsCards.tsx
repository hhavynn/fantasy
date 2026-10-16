import { PlayerIdentity } from "@/components/PlayerIdentity";
import { TrendArrow } from "@/components/ui";
import { availabilityWarning, opponentLabel, rankLabel, type RankingRow } from "@/lib/rankings";
import { AvailabilityBadge, Points, RatingBar, RosterBadge } from "./RankingParts";
import styles from "./rankings.module.css";

const TREND_WORD: Record<string, string> = { UP: "Rising", FLAT: "Flat", DOWN: "Falling" };

/** The phone layout: one card per player. Shown instead of the table at 720px and below (CSS only). */
export function RankingsCards({ rows, showOurs, basis }: { rows: RankingRow[]; showOurs: boolean; basis: "espn" | "ours" }) {
  return (
    <ol className={styles.cardView} aria-label="Player rankings">
      {rows.map((r) => {
        const ranked = r.rank.overall !== null;
        const warning = availabilityWarning(r);
        const why = r.why[0];
        return (
          <li key={r.espn_player_id} className={r.roster.is_mine ? `${styles.card} ${styles.mine}` : styles.card}>
            <div className={styles.cardHead}>
              <span className={styles.cardRank}>
                <strong className={ranked ? undefined : styles.unranked}>{rankLabel(r)}</strong>
                {ranked && <span className={styles.overall}>#{r.rank.overall} overall</span>}
              </span>
              <RosterBadge roster={r.roster} />
            </div>
            <PlayerIdentity id={r.espn_player_id} name={r.full_name} position={r.position} team={r.pro_team}
              meta={<AvailabilityBadge row={r} />} size="lg" />
            <dl className={styles.facts}>
              <div>
                <dt>{basis === "ours" ? "Ours pts" : "ESPN pts"}</dt>
                <dd className={styles.keyFact}>
                  <Points value={basis === "ours" ? r.projection.ours : r.projection.espn} status={r.projection.status} />
                </dd>
              </div>
              {showOurs && (
                <div>
                  <dt>ESPN pts</dt>
                  <dd><Points value={r.projection.espn} status={r.projection.status} /></dd>
                </div>
              )}
              <div className={styles.ratingFact}>
                <dt>Rating</dt>
                <dd><RatingBar value={r.rating.overall} label="Overall rating" /></dd>
              </div>
              <div>
                <dt>Opp</dt>
                <dd>{opponentLabel(r.game)}</dd>
              </div>
              <div>
                <dt>Trend</dt>
                <dd><TrendArrow label={r.role_trend} /> {r.role_trend && TREND_WORD[r.role_trend] ? <span className={styles.trendWord}>{TREND_WORD[r.role_trend]}</span> : null}</dd>
              </div>
            </dl>
            {warning && <p className={styles.warning}>{warning}</p>}
            {why && <p className={styles.why}>{why}</p>}
          </li>
        );
      })}
    </ol>
  );
}
