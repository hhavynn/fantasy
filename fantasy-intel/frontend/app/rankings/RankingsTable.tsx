import { PlayerIdentity } from "@/components/PlayerIdentity";
import { SortableTh, TrendArrow } from "@/components/ui";
import { Missing } from "@/lib/format";
import { formatPoints, opponentLabel, rankLabel, type RankingRow } from "@/lib/rankings";
import type { SearchParams, SortState } from "@/lib/sort";
import { AvailabilityBadge, Points, RatingBar, RatingText, RosterBadge } from "./RankingParts";
import styles from "./rankings.module.css";

const RATING_TITLE = "Overall rating, 0–100.0: a percentile within the player's own position, from season and recent PPR production and opportunity. It is not a trade price and is not comparable to projected points. Ratings use full PPR, not your league's scoring.";

/**
 * The desktop table. `sp` is the search params without `offset`, so a sort link starts again at page one.
 * `showOurs` adds the Fantasy Intel projection column (only when the page is ranked by Ours).
 */
export function RankingsTable({ rows, sort, sp, showOurs, windowLabel, basis }: {
  rows: RankingRow[];
  sort: SortState;
  sp: SearchParams;
  showOurs: boolean;
  windowLabel: string;
  basis: "espn" | "ours";
}) {
  const projTitle = (who: string) => `${who} projected fantasy points for ${windowLabel}, in your league's scoring.`;
  return (
    <div className={`table-wrap ${styles.tableView}`}>
      <table className={styles.table} aria-label="Player rankings">
        <thead>
          <tr>
            <SortableTh sortKey="rank" label="Rank" current={sort} searchParams={sp} defaultDir="asc" className={styles.rankCol}
              title="Rank by projected points: position rank (WR3) with the overall rank beneath. T- means tied. Unranked players are listed last with the reason." />
            <SortableTh sortKey="name" label="Player" current={sort} searchParams={sp} defaultDir="asc" className={styles.playerCol} title="Player name, A to Z" />
            <th scope="col" title="Opponent in the ranked week">Opp</th>
            <SortableTh sortKey="espn" label="ESPN pts" align="right" current={sort} searchParams={sp} title={projTitle("ESPN's")} />
            {showOurs && <SortableTh sortKey="ours" label="Ours pts" align="right" current={sort} searchParams={sp} title={`${projTitle("Fantasy Intel's")} Opt-in and experimental.`} />}
            <SortableTh sortKey="rating" label="Rating" align="right" current={sort} searchParams={sp} title={RATING_TITLE} />
            <SortableTh sortKey="opportunity" label="Opportunity" align="right" current={sort} searchParams={sp}
              title="Opportunity rating, 0–100.0: percentile of recent expected points (xFP) within the position, full PPR. Position-relative, not a price." />
            <SortableTh sortKey="production" label="Production" align="right" current={sort} searchParams={sp}
              title="Production rating, 0–100.0: percentile of recent PPR points per game within the position, full PPR. Position-relative, not a price." />
            <SortableTh sortKey="xfp" label="xFP/g" align="right" current={sort} searchParams={sp}
              title="Expected PPR points per game from role (targets, carries, field position): what an average player would score with this workload" />
            <SortableTh sortKey="ppr" label="PPR/g" align="right" current={sort} searchParams={sp} title="PPR fantasy points scored per game this season (nflverse)" />
            <SortableTh sortKey="consistency" label="SD" align="right" current={sort} searchParams={sp} defaultDir="asc"
              title="Standard deviation of weekly PPR points over games played. Lower is steadier; raw points, not a rating. Sorts steadiest first." />
            <SortableTh sortKey="trend" label="Trend" current={sort} searchParams={sp} defaultDir="desc" title="Role trend: usage rising, flat or falling" />
            <th scope="col" title="Who has the player in your league">Roster</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => {
            const ranked = r.rank.overall !== null;
            const why = r.why[0];
            return (
              <tr key={r.espn_player_id} className={r.roster.is_mine ? "mine" : undefined}>
                <td className={`${styles.rankCol} ${styles.rankCell}`}>
                  <strong className={ranked ? undefined : styles.unranked}>{rankLabel(r)}</strong>
                  {ranked && <span className={styles.overall} title="Overall rank among all QB, RB, WR and TE">#{r.rank.overall}</span>}
                </td>
                <td className={styles.playerCol}>
                  <PlayerIdentity id={r.espn_player_id} name={r.full_name} position={r.position} team={r.pro_team}
                    meta={<AvailabilityBadge row={r} />} />
                  {why && <span className={styles.why}>{why}</span>}
                </td>
                <td className="dim">{opponentLabel(r.game)}</td>
                <td className={`num ${basis === "espn" ? "key" : "dim"}`}><Points value={r.projection.espn} status={r.projection.status} /></td>
                {showOurs && <td className={`num ${basis === "ours" ? "key" : "dim"}`}><Points value={r.projection.ours} status={r.projection.status} /></td>}
                <td className="num"><RatingBar value={r.rating.overall} label="Overall rating" /></td>
                <td className="num"><RatingText value={r.rating.opportunity} /></td>
                <td className="num"><RatingText value={r.rating.production} /></td>
                <td className="num">{r.opportunity.xfp_per_game === null ? <Missing why="Too few games" /> : formatPoints(r.opportunity.xfp_per_game)}</td>
                <td className="num">{r.production.ppr_per_game === null ? <Missing why="Too few games" /> : formatPoints(r.production.ppr_per_game)}</td>
                <td className="num" title={r.consistency.games ? `Over ${r.consistency.games} games played` : undefined}>
                  {r.consistency.ppr_sd === null ? <Missing why="Needs at least two games played" /> : formatPoints(r.consistency.ppr_sd)}
                </td>
                <td><TrendArrow label={r.role_trend} /></td>
                <td><RosterBadge roster={r.roster} /></td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
