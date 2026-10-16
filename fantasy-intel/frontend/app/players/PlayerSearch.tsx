import { PlayerIdentity } from "@/components/PlayerIdentity";
import { Badge, Delta, EmptyState, Rating, Section, TrendArrow } from "@/components/ui";
import { cx } from "@/lib/cx";
import { int, num } from "@/lib/format";
import { RATING_CAVEAT, RATING_DESCRIPTION, ratingFor } from "@/lib/ratings";
import { hasInjuryDesignation, ownerBadge } from "./helpers";
import styles from "./players.module.css";
import type { PlayerListRow } from "./types";

/** GET form that keeps the query in the URL (`/players?q=`). */
export function SearchForm({ q, compact = false, autoFocus = false }: { q: string; compact?: boolean; autoFocus?: boolean }) {
  return (
    <form className={cx(styles.search, compact ? styles.searchCompact : styles.searchBar)} action="/players" role="search">
      <input
        className={styles.input}
        type="search"
        name="q"
        defaultValue={q}
        placeholder={compact ? "Search players" : "Search by name (e.g. Lloyd)"}
        aria-label="Search players by name"
        autoFocus={autoFocus}
      />
      <button className={styles.submit} type="submit">Search</button>
    </form>
  );
}

const OPP_TITLE = `Opportunity rating, 0–100: ${RATING_DESCRIPTION.opportunity}. ${RATING_CAVEAT}`;

export function ResultsTable({ rows, q, limit }: { rows: PlayerListRow[]; q: string; limit: number }) {
  if (rows.length === 0) {
    return (
      <Section title={`Results for “${q}”`}>
        <EmptyState title={`No players match “${q}”`}>
          <p>Check the spelling or try just a last name. Accents are ignored, and kickers and team defenses are searchable too.</p>
        </EmptyState>
      </Section>
    );
  }
  return (
    <Section
      title={q ? `Results for “${q}”` : "Most-owned players"}
      actions={<span className="faint">{rows.length} {rows.length === 1 ? "player" : "players"}</span>}
      footnote={`${q
        ? (rows.length >= limit ? `Showing the first ${limit} matches, most-owned first. Add more of the name to narrow them.` : "Most-owned first.")
        : `The ${limit} most-owned players across ESPN. Search above to find anyone else.`} Opportunity is a 0–100 relative rank within position, not a projection.`}
      flush
    >
      <div className="table-wrap">
        <table className={styles.results}>
          <thead>
            <tr>
              <th>Player</th><th>Fantasy team</th>
              <th className="num" title="Percent of ESPN leagues that roster the player">% own</th>
              <th className="num" title="NFL games with a usage record this season">G</th>
              <th className="num" title="PPR points per game (nflverse)">PPR/g</th>
              <th className="num" title="Expected PPR points per game from role (nflverse xFP): what an average player would score with this workload">xFP/g</th>
              <th className="num" title="PPR per game minus expected PPR per game. Above zero: producing more than the role implies">+/− xFP</th>
              <th title="Usage trend">Trend</th><th title="Market signal">Signal</th>
              <th className="num" title={OPP_TITLE}>Opportunity</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((p) => {
              const owner = ownerBadge(p.fantasy_team, p.availability_status);
              return (
                <tr key={p.espn_player_id}>
                  <td>
                    <PlayerIdentity id={p.espn_player_id} name={p.full_name} position={p.position} team={p.pro_team}
                                    meta={hasInjuryDesignation(p.injury_status) ? <Badge value={p.injury_status} /> : null} />
                  </td>
                  <td><Badge value={owner.text} tone={owner.tone} /></td>
                  <td className="num dim">{num(p.percent_owned, 1)}</td>
                  <td className="num dim">{int(p.games, "No games / no NFL id")}</td>
                  <td className="num key">{num(p.fp_per_game, 1, "No games / no NFL id")}</td>
                  <td className="num">{num(p.xfp_per_game, 1, "No games / no NFL id")}</td>
                  <td className="num"><Delta value={p.fp_minus_xfp} /></td>
                  <td><TrendArrow label={p.trend_label} /></td>
                  <td><Badge value={p.market_signal} /></td>
                  <td className="num"><Rating rating={ratingFor(p, "opportunity")} kind="opportunity" variant="bar" why="No usage data" /></td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </Section>
  );
}
